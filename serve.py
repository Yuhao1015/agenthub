"""Agent Hub local server: dashboard + goal dispatch + manager chat + settings.

  python3 serve.py [--port N] [--data-dir DIR] [--project NAME]
               # http://localhost:8787/dashboard.html

GET  /health            {"version","commit","data_dir","project","busy","port"}
GET  /events.jsonl      the CURRENT run's event stream (frontends poll this;
                        response header X-AgentHub-Run carries the run id so
                        they can reset their byte offset when a new run starts)
GET  /file?task=&path=  one file that task actually produced (whitelisted via
                        the owning run's blackboard, newest run first)
GET  /file/diff?task=&path=  live `git diff` (worktree vs HEAD) for that
                        recorded deliverable; argv-only subprocess, no shell
GET  /export?run=       zip: this run's real deliverables (grouped by
                        task id) + a templated SUMMARY.md, no LLM call
GET  /settings          read-only snapshot for the settings page
GET  /settings/tools    effective allowed_tools per claude_code agent
                        (POST = tighten-only edit via matrix_patch)
GET  /settings/search?q=   full-text search over all past runs
                        (goal / task titles / notes / deliverable
                         file names), linear scan, no index
GET  /settings/agents   existence probe of local agent CLIs (zero LLM calls)
POST /settings/pick-workdir   macOS native folder chooser (osascript) ->
                        candidate path; takes effect only after the save
POST /settings/workdir  choose an existing folder as the work area
                        (config.yaml `workdir`; "" resets to default)
POST /settings/reveal-workdir   open the configured workdir in the OS
                        file manager
POST /run     {"goal"|"plan", "mock"?}
POST /rerun   rerun the latest run's STALE tasks (reuses that run's files)
POST /chat    manager clarification dialogue
POST /task/{id}/message   deep-collaboration follow-up (queued when running)
POST /settings/preflight   one-time REAL probe of every configured agent
                           (a few tokens each) -> persisted advice
POST /settings/config      write user overrides (budget / matrix patch)
POST /settings/credentials write local credentials (0600, synced to env)
GET  /schedules            scheduled goals, best-effort in-process firing
                            (POST /schedules, POST /schedules/delete; the
                            firing loop lives only in the real server, and
                            a missed window is recorded, never retried)
POST /shutdown             clean exit (409 while a run is in progress)

Data layout (desktop app, one user, no cloud):

  <data-dir>/<project>/runs/<run-id>/blackboard.jsonl
                                        events.jsonl
                        /runs/index.jsonl     one line per run
                        /workdir/             where agents write deliverables
                        /config.yaml          user overrides (budget + matrix patch)
                        /merged_matrix.yaml   derived: matrix + user patch, deep merge
                        /preflight.json       last real-preflight result
  <data-dir>/credentials.yaml                local secrets (0600, never served)

BYOK: credentials live on this machine only and are never returned by any
endpoint -- settings reports just "configured / not configured".

Legacy mode: when the module is imported without _configure() (tests, and
plain `python3 serve.py` before the desktop shell lands), DATA_DIR is empty
and everything behaves as before: paths stay module-level globals relative
to the cwd, no run directories, no monitor thread. Tests monkeypatch
PORT/MATRIX/BOARD_PATH/EVENTS_PATH and rely on those names staying module
globals -- do not replace them with a config object.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import contextlib
import datetime
import io
import collections
import json
import math
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import yaml

from hub import atomicio, conversation, manager, sessions
from hub.blackboard import Blackboard
from hub.executors import _codex_cmd, _dsh_cmd, _resolve_codebuddy_paths
from hub.models import EventBus, Task, TaskType
from hub.executors import _which as _cli_which
from hub.router import Router

VERSION = "0.2.0"
MAIN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
MATRIX_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "hub", "capability_matrix.yaml")

PORT = 8787
MATRIX = MATRIX_BASE
BOARD_PATH = "blackboard.jsonl"
EVENTS_PATH = "events.jsonl"
WORKDIR = "."

DATA_DIR = ""            # "" = legacy dev mode (no desktop data directory)
PROJECT = "default"
RUNTIME_DIR = ""
RUNS_DIR = ""
SESSIONS_DIR = ""
ACTIVE_SESSION_PATH = ""
INDEX_PATH = ""
CONFIG_PATH = ""
MERGED_MATRIX_PATH = ""
PREFLIGHT_PATH = ""
CREDENTIALS_PATH = ""
SCHEDULES_PATH = ""        # <RUNTIME_DIR>/schedules.json (desktop only)

SESSION_TTL_SEC = 30 * 24 * 60 * 60

# project names become one directory level under DATA_DIR; this is the
# FIRST time PROJECT accepts untrusted HTTP input (POST /settings/
# switch-project), so validate before anything touches the filesystem.
# ASCII-only + no dots is deliberately conservative (no `..`, no reserved
# names, no Unicode normalization questions) -- see the project-switcher
# design doc §3.3/§4.
_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Judgment call (no measured basis): cap for one git-diff response body,
# same "prevent runaway" family as MAX_PREVIEW_BYTES/MAX_EXPORT_BYTES.
MAX_DIFF_CHARS = 200_000
COMMIT = ""

_current: subprocess.Popen | None = None
_run_meta: dict = {}     # {"id","board","events","proc"} -- the latest run
_runs: list[dict] = []   # index.jsonl entries, in-memory mirror
SERVER: ThreadingHTTPServer | None = None
PROGRESS_MAX_STEPS = 40   # 一轮澄清撑死几十步；有界是因为经理 session 是
                           # 长期驻留的内存对象，无界缓冲就是慢性泄漏
_progress: dict[str, "collections.deque"] = {}


def _progress_sink(sid: str):
    """返回一个 on_event 回调，把这一步追加进该 session 的进度缓冲。

    这是用户在一次高推理时长的经理回合里唯一能看到的东西 —— `claude -p`
    的 thinking 块是空的（2026-07-23 实测，2026-08-31 复测仍然如此），
    没有推理文本可展示，只有推理的**形状**（想了几轮、读了什么、何时
    开始写回复）。**不要为了好看编造旁白。**

    写入发生在 /chat 的处理线程，读取发生在 /chat/progress 的另一个线程；
    deque 的 append 在 CPython 下是原子的，所以这里不额外上锁。
    """
    buf = _progress.setdefault(sid, collections.deque(maxlen=PROGRESS_MAX_STEPS))

    def sink(detail: str) -> None:
        text = str(detail or "").strip()
        if text:
            buf.append({"ts": round(time.time(), 3), "detail": text[:200]})
    return sink


def _progress_read(sid: str) -> list[dict]:
    return list(_progress.get(sid) or ())


def _progress_reset(sid: str) -> None:
    """每轮开头清空 —— 用户要看的是这一轮的进度，不是历史堆积。"""
    _progress.pop(sid, None)


_sessions: dict[str, dict] = {}   # sid -> history/questions/awaiting_confirmation/proposal/router
_active_sid = ""
_chat_lock = threading.RLock()
_chat_inflight: set[str] = set()





# ---------- 会话存储（实现在 hub/sessions.py，2026-09-05 抽出） ----------
# 校验/序列化/单文件读写搬去了 hub/sessions.py：那些是数据平面的事，且
# 全部不依赖本模块的任何全局，抽出去之后能脱离 31 个模块级路径做单元测试。
# 这里保留原名 —— serve.py 内部有 46 处 SessionDataError、5 处
# _SESSION_DISK_FIELDS，测试也直接引用 serve.SessionDataError。异常必须是
# 同一个类对象，否则 except 抓不住 hub/sessions.py 抛出来的那个。
#
# 缓存(_sessions)、锁(_chat_lock)、活跃指针、归档与对账**刻意留在这里**：
# 它们和经理对话状态机缠在一起，而且测试把 _sessions 当作 serve 自己的
# （test_serve_chat.py 直接重绑它）。要动那部分得先决定缓存归谁。
_SESSION_ID_RE = sessions.SESSION_ID_RE
_SESSION_FILE_RE = sessions.SESSION_FILE_RE
_MANAGER_RUN_ID_RE = sessions.MANAGER_RUN_ID_RE
_SESSION_DISK_FIELDS = sessions.DISK_FIELDS
SessionDataError = sessions.SessionDataError
SessionConflict = sessions.SessionConflict
_atomic_write = atomicio.atomic_write
_session_number = sessions.is_number
_validate_session_id = sessions.validate_session_id
_new_manager_session = sessions.new_manager_session
_session_disk_dict = sessions.disk_dict
_validate_session_questions = sessions.validate_questions
_validate_session_history = sessions.validate_history
_validate_session_data = sessions.validate_session_data
_session_stage = sessions.stage
_session_public = sessions.public


# 这四个要带上 SESSIONS_DIR。它是 _configure() 每次重设的模块级路径，
# import 期绑死就会让测试的 isolated_desktop() 全部失效，所以只能是函数。
def _session_path(sid: str) -> str:
    return sessions.session_path(SESSIONS_DIR, sid)


def _session_save(sess: dict) -> None:
    sessions.save(SESSIONS_DIR, sess)


def _session_write_snapshot(sess: dict) -> None:
    sessions.write_snapshot(SESSIONS_DIR, sess)


def _session_load_disk(sid: str) -> dict | None:
    return sessions.load_disk(SESSIONS_DIR, sid)


# Per-task_id locks for POST /task/{id}/message. ThreadingHTTPServer runs
# each request in its own OS thread within this ONE persistent process, so
# an in-process lock is enough to serialize concurrent follow-ups to the
# same task (double-click send, two browser tabs) without touching
# hub/conversation.py at all. Different task_ids never wait on each other.
# _task_locks grows by one entry per distinct task_id ever messaged and is
# never pruned -- fine for a single session's realistic task count, not
# meant to survive being a long-lived multi-tenant service.
_task_locks: dict[str, threading.Lock] = {}
_task_locks_guard = threading.Lock()
_gate_reply_lock = threading.Lock()


def _lock_for_task(task_id: str) -> threading.Lock:
    with _task_locks_guard:
        lock = _task_locks.get(task_id)
        if lock is None:
            lock = _task_locks[task_id] = threading.Lock()
        return lock


# The best-effort scheduler (see _schedule_loop) runs in its own daemon
# thread and can now spawn a run on its own, independent of any HTTP
# request -- two locks close the two races that introduces:
#   _spawn_lock:      serializes "is a run busy? -> spawn one" everywhere
#                      that does it (HTTP handlers AND _schedule_fire), so
#                      the scheduler and an HTTP request can't both pass
#                      their own busy check and both spawn an engine that
#                      shares one blackboard (only one becomes `_current`;
#                      the other is unkillable via POST /cancel and
#                      invisible to the watchdog).
#   _schedules_lock:   serializes read-modify-write of schedules.json
#                      between the scheduler tick and POST /schedules(/
#                      delete), so one side's write can't be silently
#                      clobbered by the other's (a lost last_fired_date
#                      update would let a daily goal fire twice).
_spawn_lock = threading.Lock()
_schedules_lock = threading.Lock()


def _spawn_main_if_idle(argv_tail: list[str]) -> int | None:
    """Atomic check-and-spawn. Returns None (never spawns) if a run is
    already busy at the moment the lock is held; propagates OSError from
    _spawn_main unchanged (callers already handle that)."""
    with _spawn_lock:
        if _run_busy():
            return None
        return _spawn_main(argv_tail)


def _spawn_manager_main_if_idle(
        argv_tail: list[str]) -> tuple[int | None, str]:
    """Spawn for manager confirmation and capture its run id atomically."""
    with _spawn_lock:
        if _run_busy():
            return None, ""
        pid = _spawn_main(argv_tail)
        return pid, str(_run_meta.get("id") or "")


# ---------- desktop configuration ----------

def _default_data_dir() -> str:
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/AgentHub")
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return os.path.join(base, "AgentHub")
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "AgentHub")


























def _run_manager_archive_path(run_id: str) -> str:
    """Resolve one archive path without following run-local symlinks."""
    if not isinstance(run_id, str) or not _MANAGER_RUN_ID_RE.fullmatch(run_id):
        raise SessionDataError("manager session has an invalid run id")
    if not RUNS_DIR:
        raise SessionDataError("manager session archive is unavailable")

    runs_root = os.path.realpath(RUNS_DIR)
    requested_run_dir = os.path.join(RUNS_DIR, run_id)
    if os.path.islink(requested_run_dir):
        raise SessionDataError("manager session run directory is a symlink")
    run_dir = os.path.realpath(requested_run_dir)
    try:
        if run_dir == runs_root or \
                os.path.commonpath((runs_root, run_dir)) != runs_root:
            raise SessionDataError(
                "manager session run directory is outside the runs directory")
    except ValueError as exc:
        raise SessionDataError(
            "manager session run directory is outside the runs directory") from exc
    if not os.path.isdir(run_dir):
        raise SessionDataError("manager session run directory does not exist")

    path = os.path.join(run_dir, "manager-session.json")
    if os.path.islink(path):
        raise SessionDataError("manager session archive is a symlink")
    resolved_path = os.path.realpath(path)
    try:
        if os.path.commonpath((run_dir, resolved_path)) != run_dir:
            raise SessionDataError(
                "manager session archive is outside its run directory")
    except ValueError as exc:
        raise SessionDataError(
            "manager session archive is outside its run directory") from exc
    return path


def _write_run_manager_archive(sess: dict, run_id: str) -> None:
    """Persist a started session while preserving conflicting evidence."""
    path = _run_manager_archive_path(run_id)
    payload = _validate_session_data(_session_disk_dict(sess))
    if payload.get("status") != "started" or payload.get("run_id") != run_id:
        raise SessionDataError("manager session archive does not match its run id")

    if os.path.lexists(path):
        try:
            with open(path, encoding="utf-8") as f:
                existing = _validate_session_data(json.load(f))
        except (OSError, UnicodeError, json.JSONDecodeError,
                SessionDataError) as exc:
            raise SessionDataError(
                f"existing manager session archive is unreadable: {exc}") from exc
        if existing != payload:
            raise SessionDataError(
                "existing manager session archive conflicts with this session")
        return

    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2,
                                   allow_nan=False), mode=0o600)


def _unlink_session_file(sid: str) -> None:
    path = _session_path(sid)
    if not path:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _archive_manager_session(sess: dict, run_id: str) -> str:
    """Move a successfully-started manager session into its run directory.

    The started source snapshot is written first. It remains authoritative
    until the run archive is durable, closing the spawn/archive crash window.
    """
    sid = _validate_session_id(sess.get("session_id", ""))
    run_id = str(run_id or "")
    if not run_id or not RUNS_DIR:
        _clear_active_session(sid)
        _sessions.pop(sid, None)
        _progress_reset(sid)
        return ""

    already_started = sess.get("status") == "started"
    if not already_started:
        sess["status"] = "started"
        sess["run_id"] = run_id
        sess["history"].append({
            "role": "manager", "text": "收到，马上安排开工。",
            "ts": round(time.time(), 3),
        })
    elif sess.get("run_id") != run_id:
        return "manager session archive failed: started session run id mismatch"

    active_error = ""
    try:
        if already_started:
            _session_write_snapshot(sess)
        else:
            _session_save(sess)
    except (OSError, SessionDataError, ValueError) as exc:
        active_error = str(exc)
    try:
        _write_run_manager_archive(sess, run_id)
    except (OSError, SessionDataError, ValueError) as exc:
        return (f"manager session archive failed: {exc}; "
                f"active save: {active_error or 'ok'}")

    try:
        _clear_active_session(sid)
        _unlink_session_file(sid)
    except OSError as exc:
        return f"manager session cleanup failed after archive: {exc}"
    _sessions.pop(sid, None)
    _progress_reset(sid)
    return ""


def _reconcile_manager_sessions(now: float | None = None) -> None:
    """Recover started fallbacks and expire old active drafts at startup."""
    if not SESSIONS_DIR or not os.path.isdir(SESSIONS_DIR):
        return
    current_time = time.time() if now is None else now
    try:
        names = os.listdir(SESSIONS_DIR)
    except OSError as exc:
        print(f"[manager-session] cleanup skipped: {exc}")
        return

    for name in names:
        match = _SESSION_FILE_RE.fullmatch(name)
        if not match:
            continue
        sid = match.group(1)
        path = _session_path(sid)
        try:
            if os.path.islink(path):
                raise SessionDataError(
                    "manager session file is a symlink; preserving evidence")
            sess = _session_load_disk(sid)
            if sess is None:
                continue
            if sess.get("status") == "started":
                run_id = str(sess.get("run_id") or "")
                if not _MANAGER_RUN_ID_RE.fullmatch(run_id):
                    raise SessionDataError(
                        "started manager session has an invalid run id")
                run_dir = os.path.join(RUNS_DIR, run_id) if RUNS_DIR else ""
                if run_dir and os.path.isdir(run_dir):
                    _write_run_manager_archive(sess, run_id)
                    _unlink_session_file(sid)
                    _clear_active_session(sid)
                    _sessions.pop(sid, None)
                    _progress_reset(sid)
            elif current_time - float(sess.get("updated_at") or 0) > \
                    SESSION_TTL_SEC:
                _unlink_session_file(sid)
                _clear_active_session(sid)
                _sessions.pop(sid, None)
                _progress_reset(sid)
        except (OSError, SessionDataError, TypeError, ValueError) as exc:
            print(f"[manager-session] skipped {name}: {exc}")

    try:
        active = _active_session_id()
        if active and not os.path.lexists(_session_path(active)):
            _clear_active_session(active)
    except (OSError, SessionDataError, ValueError) as exc:
        print(f"[manager-session] active pointer kept for diagnosis: {exc}")


def _session_router(sess: dict):
    """Lazily restore the runtime-only router after a process restart."""
    router = sess.get("router")
    if router is None:
        router = Router(MATRIX, workdir=WORKDIR)
        sess["router"] = router
    return router




def _session_for_chat(sid: str) -> tuple[dict, bool]:
    sid = _validate_session_id(sid)
    active = _active_session_id()
    if active and active != sid:
        raise SessionConflict(f"another manager session is active: {active}")
    sess = _sessions.get(sid)
    if sess is None:
        sess = _session_load_disk(sid)
    if active == sid and sess is None:
        raise SessionDataError("active manager session file is missing")
    created = sess is None
    if created:
        sess = _new_manager_session(sid)
        _session_save(sess)
    if sess.get("status") != "active":
        raise SessionConflict("manager session has already started")
    try:
        with _chat_lock:
            current_active = _active_session_id()
            if current_active and current_active != sid:
                raise SessionConflict(
                    f"another manager session is active: {current_active}")
            if not current_active:
                _set_active_session(sid)
    except (OSError, SessionConflict):
        if created:
            path = _session_path(sid)
            if path:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
        raise
    _sessions[sid] = sess
    return sess, created


def _choice_answers(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {key[:100]: answer[:2000]
            for key, answer in value.items()
            if isinstance(key, str) and isinstance(answer, str)}


def _active_session_id() -> str:
    global _active_sid
    with _chat_lock:
        if _active_sid:
            return _active_sid
        if not ACTIVE_SESSION_PATH or not os.path.exists(ACTIVE_SESSION_PATH):
            return ""
        try:
            with open(ACTIVE_SESSION_PATH, encoding="utf-8") as f:
                pointer = json.load(f)
            sid = pointer.get("session_id") if isinstance(pointer, dict) else ""
            _active_sid = _validate_session_id(sid)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise SessionDataError(
                f"active manager session pointer is unreadable: {exc}") from exc
        return _active_sid


def _set_active_session(sid: str) -> None:
    global _active_sid
    sid = _validate_session_id(sid)
    with _chat_lock:
        if ACTIVE_SESSION_PATH:
            _atomic_write(ACTIVE_SESSION_PATH,
                          json.dumps({"session_id": sid}), mode=0o600)
        _active_sid = sid


def _clear_active_session(sid: str) -> None:
    global _active_sid
    sid = _validate_session_id(sid)
    with _chat_lock:
        if _active_session_id() != sid:
            return
        if ACTIVE_SESSION_PATH:
            try:
                os.unlink(ACTIVE_SESSION_PATH)
            except FileNotFoundError:
                pass
        _active_sid = ""




def _run_manager_session() -> dict | None:
    """当前 run 归档的那份澄清对话，没有就是 None。

    找不到归档文件时回落到内存/磁盘上那个已 started 的会话 —— 归档是
    Task 4 才落地的，在那之前这条回落是唯一能让「已开工的对话」显示
    出来的路径。
    """
    run_id = str(_run_meta.get("id") or "")
    if not run_id or not RUNS_DIR:
        return None
    if re.fullmatch(r"[a-f0-9]{8}", run_id) is None:
        raise SessionDataError("run manager session has an invalid run id")
    runs_root = os.path.realpath(RUNS_DIR)
    run_dir = os.path.realpath(os.path.join(runs_root, run_id))
    try:
        if run_dir == runs_root or \
                os.path.commonpath((runs_root, run_dir)) != runs_root:
            raise SessionDataError(
                "run manager session is outside the configured runs directory")
    except ValueError as exc:
        raise SessionDataError(
            "run manager session is outside the configured runs directory") from exc
    path = os.path.realpath(os.path.join(run_dir, "manager-session.json"))
    try:
        if os.path.commonpath((run_dir, path)) != run_dir:
            raise SessionDataError(
                "run manager session archive is outside its run directory")
    except ValueError as exc:
        raise SessionDataError(
            "run manager session archive is outside its run directory") from exc
    if not os.path.exists(path):
        sid = _active_session_id()
        fallback = (_sessions.get(sid) or _session_load_disk(sid)) if sid else None
        if fallback and fallback.get("status") == "started" and \
                fallback.get("run_id") == run_id:
            return _session_public(fallback, False)
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SessionDataError(f"run manager session is unreadable: {exc}") from exc
    data = _validate_session_data(data)
    if data.get("run_id") != run_id or data.get("status") != "started":
        raise SessionDataError("run manager session has an invalid schema")
    return _session_public(data, False)


def _current_manager_sessions() -> dict:
    """前端刷新后要恢复的两份东西：还在澄清中的草稿（active），以及
    当前这次 run 开工前的那段对话（run）。两者必须分开 —— 混在一起会
    让「正在写的新目标」串进上一次运行的记录里。"""
    active = None
    sid = _active_session_id()
    if sid:
        sess = _sessions.get(sid) or _session_load_disk(sid)
        if sess is None:
            raise SessionDataError("active manager session file is missing")
        if sess.get("status") == "active":
            active = _session_public(sess, sid in _chat_inflight)
    run = _run_manager_session()
    if run:
        run = {"run_id": run.get("run_id"), "history": run.get("history", [])}
    return {"active": active, "run": run}


def _resolve_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, timeout=5,
                             cwd=os.path.dirname(os.path.abspath(__file__)))
        if out.returncode == 0:
            return out.stdout.decode().strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def _configure(data_dir: str | None, project: str | None, port: int) -> None:
    """Desktop-mode setup. Runs once from __main__; tests never call it,
    which keeps every module-level default intact for them."""
    global PORT, DATA_DIR, PROJECT, RUNTIME_DIR, RUNS_DIR, INDEX_PATH
    global CONFIG_PATH, MERGED_MATRIX_PATH, PREFLIGHT_PATH, CREDENTIALS_PATH
    global SCHEDULES_PATH, SESSIONS_DIR, ACTIVE_SESSION_PATH, _active_sid
    global MATRIX, BOARD_PATH, EVENTS_PATH, WORKDIR, COMMIT
    _active_sid = ""
    PORT = port
    DATA_DIR = data_dir or os.environ.get("AGENT_HUB_HOME") or _default_data_dir()
    PROJECT = project or _load_last_project(DATA_DIR) or "default"
    RUNTIME_DIR = os.path.join(DATA_DIR, PROJECT)
    RUNS_DIR = os.path.join(RUNTIME_DIR, "runs")
    SESSIONS_DIR = os.path.join(RUNTIME_DIR, "sessions")
    ACTIVE_SESSION_PATH = os.path.join(SESSIONS_DIR, "active.json")
    INDEX_PATH = os.path.join(RUNS_DIR, "index.jsonl")
    CONFIG_PATH = os.path.join(RUNTIME_DIR, "config.yaml")
    MERGED_MATRIX_PATH = os.path.join(RUNTIME_DIR, "merged_matrix.yaml")
    PREFLIGHT_PATH = os.path.join(RUNTIME_DIR, "preflight.json")
    CREDENTIALS_PATH = os.path.join(DATA_DIR, "credentials.yaml")
    SCHEDULES_PATH = os.path.join(RUNTIME_DIR, "schedules.json")
    WORKDIR = os.path.join(RUNTIME_DIR, "workdir")
    os.makedirs(RUNS_DIR, exist_ok=True)
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    os.makedirs(WORKDIR, exist_ok=True)
    # optional user override: an EXISTING folder outside the data dir can
    # become the work area (where agents write deliverables). Invalid or
    # vanished overrides silently fall back to the default -- the app must
    # still start, see the workdir-picker design doc §1.1.
    cfg = _load_config()
    override = cfg.get("workdir")
    if isinstance(override, str) and override.strip() and os.path.isdir(override):
        WORKDIR = os.path.realpath(override)
    _credentials_load()
    _runs_load()
    _write_merged_matrix()
    _adopt_latest_run()
    _reconcile_manager_sessions()
    COMMIT = _resolve_commit()


# ---------- run index (index.jsonl) ----------

def _runs_load() -> None:
    global _runs
    _runs = []
    if not INDEX_PATH or not os.path.exists(INDEX_PATH):
        return
    try:
        with open(INDEX_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    _runs.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass


def _runs_save() -> None:
    if not INDEX_PATH:
        return
    _atomic_write(INDEX_PATH,
                  "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in _runs))


def _runs_add(run_id: str, goal: str) -> None:
    _runs.append({"run_id": run_id, "ts": round(time.time(), 3), "goal": goal,
                  "status": "running", "cost_usd": 0.0})
    _runs_save()


def _runs_update(run_id: str, **fields) -> None:
    for e in _runs:
        if e.get("run_id") == run_id:
            e.update(fields)
            _runs_save()
            return


def _adopt_latest_run() -> None:
    """Point BOARD_PATH/EVENTS_PATH at the latest run's files, so /rerun,
    /task/{id}/message and /file keep working right after an app restart.

    When the project has NO runs yet, these must still be moved off their
    module-level defaults ("events.jsonl"/"blackboard.jsonl"), which are
    RELATIVE and therefore resolve against whatever cwd the launcher
    happened to start in. Left alone, a fresh project would serve the
    repo's own leftover dev-run files as if they were its own -- the
    dashboard replays a stale run from somewhere else, and /rerun's
    os.path.exists(BOARD_PATH) check passes and resumes against that
    foreign blackboard. Pointing at a path inside RUNS_DIR that cannot
    exist makes "no run yet" read as exactly that: a clean 404."""
    global BOARD_PATH, EVENTS_PATH, _run_meta
    if not RUNS_DIR:
        return
    # "_none" can never collide with a real run id (uuid4().hex[:8], so
    # lowercase hex only), and lives under RUNS_DIR so nothing outside the
    # project is reachable through it
    empty = os.path.join(RUNS_DIR, "_none")
    BOARD_PATH = os.path.join(empty, "blackboard.jsonl")
    EVENTS_PATH = os.path.join(empty, "events.jsonl")
    run_id = None
    if _runs:
        run_id = _runs[-1].get("run_id")
    else:
        # index may be missing while run dirs exist (e.g. hand-deleted index)
        try:
            dirs = [d for d in os.listdir(RUNS_DIR)
                    if os.path.isdir(os.path.join(RUNS_DIR, d))]
        except OSError:
            dirs = []
        if dirs:
            run_id = max(dirs, key=lambda d: os.path.getmtime(os.path.join(RUNS_DIR, d)))
    if not run_id:
        return
    run_dir = os.path.join(RUNS_DIR, run_id)
    BOARD_PATH = os.path.join(run_dir, "blackboard.jsonl")
    EVENTS_PATH = os.path.join(run_dir, "events.jsonl")
    _run_meta = {"id": run_id, "board": BOARD_PATH, "events": EVENTS_PATH, "proc": None}


# ---------- credentials (BYOK, local-only, 0600) ----------

_CREDENTIAL_KEYS = ("DEEPSEEK_API_KEY", "OPENROUTER_API_KEY")


def _credentials_read() -> dict:
    if not CREDENTIALS_PATH or not os.path.exists(CREDENTIALS_PATH):
        return {}
    try:
        with open(CREDENTIALS_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def _credentials_load() -> None:
    """Startup: load stored keys into the process env for keys the shell
    didn't already provide (env wins -- operator intent, same precedence as
    dsh's credential layers)."""
    for k, v in _credentials_read().items():
        if k in _CREDENTIAL_KEYS and isinstance(v, str) and v and k not in os.environ:
            os.environ[k] = v


def _credentials_write(updates: dict) -> None:
    if not CREDENTIALS_PATH:
        raise RuntimeError("credentials unavailable in legacy mode")
    data = _credentials_read()
    for k, v in updates.items():
        if k not in _CREDENTIAL_KEYS:
            continue
        v = str(v or "")
        if v:
            data[k] = v
            os.environ[k] = v   # sync NOW: the next _spawn_main inherits it
        else:
            data.pop(k, None)
    _atomic_write(CREDENTIALS_PATH, yaml.safe_dump(data, allow_unicode=True),
                  mode=0o600)


# ---------- user overrides: config.yaml + merged matrix (brief 1.4) ----------

def _load_yaml_dict(path: str) -> dict:
    """Read a yaml file that MUST be a mapping. Anything else (a bare
    scalar, a list, unreadable, malformed) is {}.

    Why this exists: a capability matrix that parsed into a non-dict used
    to reach `.get(...)` unguarded and raise AttributeError inside a
    request handler -- which ThreadingHTTPServer turns into a dropped
    connection with NO http response, i.e. the frontend reports "can't
    reach serve.py" and the settings page is bricked until someone
    hand-edits a file. Treating a structurally-wrong matrix as "no
    matrix" degrades instead."""
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _matrix_patch(cfg: dict | None = None) -> dict:
    """config.yaml's matrix_patch, guaranteed to be a dict. Same reasoning
    as _load_yaml_dict: a hand-edited (or historically corrupted) config
    must not be able to crash a handler."""
    cfg = _load_config() if cfg is None else cfg
    patch = cfg.get("matrix_patch")
    return patch if isinstance(patch, dict) else {}


UI_PREF_DEFAULTS = {"manager_agent": "", "assign_mode": "auto", "gate_mode": "auto"}
_ASSIGN_MODES = ("auto", "manual")
_GATE_MODES = ("auto", "ask")


def _matrix_agent_names() -> set[str]:
    """All agent names declared by the current merged capability matrix."""
    path = (MERGED_MATRIX_PATH if MERGED_MATRIX_PATH and
            os.path.exists(MERGED_MATRIX_PATH) else MATRIX)
    agents = _load_yaml_dict(path).get("agents")
    return set(agents) if isinstance(agents, dict) else set()


def _ui_prefs() -> dict:
    """Remembered UI choices; effective run policy is passed through argv."""
    raw = _load_config().get("ui_prefs")
    out = dict(UI_PREF_DEFAULTS)
    if isinstance(raw, dict):
        for key in out:
            if isinstance(raw.get(key), str):
                out[key] = raw[key]
    if out["assign_mode"] not in _ASSIGN_MODES:
        out["assign_mode"] = "auto"
    if out["gate_mode"] not in _GATE_MODES:
        out["gate_mode"] = "auto"
    return out


def _gate_argv(force_auto: bool = False) -> list[str]:
    """Return gate policy in argparse-safe ``--flag=value`` form.

    Scheduled runs force both values to auto: manual assignment alone also
    opens the plan gate and would otherwise block an unattended schedule.
    """
    prefs = ({"gate_mode": "auto", "assign_mode": "auto"}
             if force_auto else _ui_prefs())
    return [f"--gate-mode={prefs['gate_mode']}",
            f"--assign-mode={prefs['assign_mode']}"]


def _apply_manager_pref(router) -> str:
    """Apply the current manager pin to an existing chat-session router.

    Save the original route on first use so changing the preference back to
    automatic during a conversation restores the full fallback chain.
    """
    name = _ui_prefs()["manager_agent"]
    routes = getattr(router, "routes", None)
    if not isinstance(routes, dict):
        # Some embedders/tests provide a minimal Router-compatible object
        # and own manager_step themselves.  UI prefs must not turn that
        # into an unhandled HTTP exception and a dropped connection.
        return name
    base_route = getattr(router, "_ui_base_manager_route", None)
    if base_route is None:
        base_route = list(routes.get("manager", []))
        router._ui_base_manager_route = base_route
    routes["manager"] = [name] if name else list(base_route)
    return name


def _load_config() -> dict:
    if not CONFIG_PATH or not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def _save_config(cfg: dict) -> None:
    _atomic_write(CONFIG_PATH, yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                  mode=0o600)


def _json_list_load(path: str) -> list[dict]:
    """Shared reader for the record-style JSON files (goal templates,
    schedules): mirror of _load_config's error philosophy -- a missing or
    broken file is an empty collection, never a crash."""
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _json_list_save(path: str, items: list[dict]) -> None:
    _atomic_write(path, json.dumps(items, ensure_ascii=False, indent=2), mode=0o600)


def _load_last_project(data_dir: str) -> str | None:
    """Reads <data_dir>/last_project.json, written by
    POST /settings/switch-project. Re-validates the name against
    _PROJECT_NAME_RE even though only our own code writes this file --
    cheap defense against a hand-edited or corrupted marker reaching
    os.path.join() unchecked."""
    if not data_dir:
        return None
    path = os.path.join(data_dir, "last_project.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    name = data.get("project") if isinstance(data, dict) else None
    if isinstance(name, str) and _PROJECT_NAME_RE.match(name):
        return name
    return None


def _read_run_index(path: str) -> list[dict]:
    """Same per-line tolerant parsing as _runs_load, but returns a fresh
    list and never touches the _runs global (used by the read-only
    GET /settings/projects)."""
    out = []
    if not path or not os.path.exists(path):
        return out
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return out


def _base_allowed_tools(name: str) -> list[str] | None:
    """The tool set as shipped in the repo's OWN capability_matrix.yaml
    (MATRIX_BASE -- never the merged/patched file). This is the ceiling a
    tighten-only edit may never exceed, AND the full candidate set the UI
    must always offer -- including a tool a previous edit already removed,
    so removing one is reversible instead of a one-way ratchet (a tool
    that's gone from the MERGED matrix is not "gone forever", it was never
    gone from the base matrix)."""
    matrix = _load_yaml_dict(MATRIX_BASE)
    spec = (matrix.get("agents") or {}).get(name) or {}
    if not isinstance(spec, dict) or spec.get("kind") != "claude_code":
        return None
    raw = spec.get("allowed_tools")
    if not isinstance(raw, str):
        return None
    tools = [t.strip() for t in raw.split(",") if t.strip()]
    return tools or None


def _effective_allowed_tools(name: str) -> tuple[list[str], bool] | None:
    """Current EFFECTIVE tool set for one claude_code agent, read from the
    merged matrix (base + existing user patch). None when the agent isn't
    a claude_code agent with a tool list there. `patched` flags whether a
    user override exists (display only)."""
    matrix = _load_yaml_dict(MATRIX)
    spec = (matrix.get("agents") or {}).get(name) or {}
    if not isinstance(spec, dict) or spec.get("kind") != "claude_code":
        return None
    raw = spec.get("allowed_tools")
    if not isinstance(raw, str):
        return None
    tools = [t.strip() for t in raw.split(",") if t.strip()]
    if not tools:
        return None   # empty string empirically does NOT restrict tools
    agents = _matrix_patch().get("agents")
    entry = agents.get(name) if isinstance(agents, dict) else None
    patched = bool(entry.get("allowed_tools")) if isinstance(entry, dict) else False
    return tools, patched


def _scan_projects() -> list[dict]:
    """Read-only: never calls _configure() or assigns any global.
    DATA_DIR/<name>/ is a project iff <name> matches _PROJECT_NAME_RE and
    is a directory -- the same filter POST /settings/switch-project
    enforces on write, so nothing shows up here that couldn't also be
    switched to."""
    out = []
    if not DATA_DIR or not os.path.isdir(DATA_DIR):
        return out
    try:
        names = os.listdir(DATA_DIR)
    except OSError:
        names = []
    for name in names:
        if not _PROJECT_NAME_RE.match(name):
            continue
        full = os.path.join(DATA_DIR, name)
        if not os.path.isdir(full):
            continue
        entries = _read_run_index(os.path.join(full, "runs", "index.jsonl"))
        out.append({"name": name, "run_count": len(entries),
                    "last_ts": entries[-1].get("ts") if entries else None})
    out.sort(key=lambda p: p["last_ts"] or 0, reverse=True)
    return out


def _schedules_load() -> list[dict]:
    return _json_list_load(SCHEDULES_PATH)


def _schedules_save(schedules: list[dict]) -> None:
    _json_list_save(SCHEDULES_PATH, schedules)


def _deep_merge(base, patch):
    """Dicts merge recursively; anything else (scalars, lists) is replaced
    wholesale by the patch -- which is what a route list edit wants."""
    if isinstance(base, dict) and isinstance(patch, dict):
        out = dict(base)
        for k, v in patch.items():
            out[k] = _deep_merge(base.get(k), v) if k in base else v
        return out
    return patch


def _write_merged_matrix() -> str:
    """Matrix + user patch -> ONE yaml file, passed to the engine subprocess
    via --matrix. serve.py itself never consumes the merged values -- the
    Router that actually runs tasks lives in the main.py subprocess and
    reads exactly one yaml path (do not break that assumption)."""
    global MATRIX
    if not CONFIG_PATH:
        return MATRIX
    base = _load_yaml_dict(MATRIX_BASE)
    if not base:
        return MATRIX
    cfg = _load_config()
    # the budget override lives under config.yaml's own "budget" key, the
    # rest of the user's edits under "matrix_patch"; both must reach the
    # engine subprocess, with matrix_patch winning if both touch a field.
    # _matrix_patch() (not cfg.get) so a non-dict patch can never be
    # _deep_merge'd over the base -- _deep_merge replaces wholesale on a
    # non-dict, which would hand the ENGINE a destroyed matrix via
    # --matrix, not just break the settings page.
    patch = _deep_merge({"budget": cfg.get("budget") or {}}, _matrix_patch(cfg))
    merged = _deep_merge(base, patch)
    if not isinstance(merged, dict):
        return MATRIX   # unreachable today; belt-and-braces before we persist
    _atomic_write(MERGED_MATRIX_PATH,
                  yaml.safe_dump(merged, allow_unicode=True, sort_keys=False),
                  mode=0o600)
    MATRIX = MERGED_MATRIX_PATH
    return MATRIX


# ---------- spawning + run watchdog ----------

def _tail_goal(argv_tail: list[str]) -> str:
    for t in argv_tail:
        if t.startswith("--goal="):
            return t[len("--goal="):][:200]
        if t.startswith("--plan="):
            return "plan: " + os.path.basename(t[len("--plan="):])
    if "--resume" in argv_tail:
        return "rerun (STALE tasks)"
    return ""


def _engine_target_missing(argv_tail: list[str]) -> str:
    """引擎有没有活干？没有就返回原因，有就返回空串。

    main.py 需要 --goal / --plan / --resume 三者之一，而且 `--goal=` 这种
    空值它在 argparse 阶段就直接 exit 2 —— 那时一个事件都还没发出来，
    看门狗只能补一句「engine exited with code 2 before run_finished」。
    用户看到的就是一次凭空失败的运行（真实案例：2026-08-31 12:11 的
    run 26d78c24，events.jsonl 里只有那一行）。

    跟上面 MAIN_PATH 那条守卫是同一类问题：**能精确检测的条件不该退化成
    通用崩溃**。POST /run 早就在自己那一层挡住了空 goal，但经理确认那条路
    没有 —— 会话 schema 只校验 proposal["goal"] 是不是字符串，不校验非空。
    所以守卫放在这个唯一的收口处，而不是各个调用点各写一遍。
    """
    if "--resume" in argv_tail:
        return ""
    for item in argv_tail:
        for flag in ("--goal=", "--plan="):
            if item.startswith(flag) and item[len(flag):].strip():
                return ""
    return ("引擎没有拿到目标：--goal 和 --plan 都是空的，main.py 会在解析参数时"
            "直接退出，不会产生任何任务。这通常是经理的方案还没落进会话就点了"
            "确认 —— 重新说一遍目标即可。跟 agent 装没装无关，做体检看不出问题。")


def _spawn_main(argv_tail: list[str]) -> int:
    """Spawn the engine subprocess. The caller's tail is passed through
    verbatim (tests assert it exactly); machine-specific paths are added
    HERE. Legacy mode (no data dir): absolute main.py path only, unchanged
    behavior otherwise. Desktop mode: give a fresh run its own directory
    and wire board/events/workdir/matrix through explicitly; a rerun
    (--resume) reuses the latest run's directory so its blackboard and
    event stream simply continue."""
    global _current, _run_meta, BOARD_PATH, EVENTS_PATH
    # 先于 run 目录创建：拦住的这一次本来就跑不起来，不该在历史里留下
    # 一条空壳失败运行（MAIN_PATH 那条守卫在目录之后，因为那时已经有
    # run 可以承载 run_crashed；这里还没有，直接把原因交给调用方即可 ——
    # 每个 spawn 调用方都已经在 catch OSError）
    problem = _engine_target_missing(argv_tail)
    if problem:
        raise OSError(problem)
    resume = "--resume" in argv_tail
    run_dir = None
    if RUNS_DIR:
        if resume and _run_meta.get("id") and \
                os.path.isdir(os.path.join(RUNS_DIR, _run_meta["id"])):
            run_dir = os.path.join(RUNS_DIR, _run_meta["id"])
        else:
            run_id = uuid.uuid4().hex[:8]
            run_dir = os.path.join(RUNS_DIR, run_id)
            os.makedirs(run_dir, exist_ok=True)
            BOARD_PATH = os.path.join(run_dir, "blackboard.jsonl")
            EVENTS_PATH = os.path.join(run_dir, "events.jsonl")
            _run_meta = {"id": run_id, "board": BOARD_PATH,
                         "events": EVENTS_PATH, "proc": None,
                         "cancel_flag": {"cancel_requested": False}}
            _runs_add(run_id, _tail_goal(argv_tail))
    # MAIN_PATH 是 import 时用 __file__ 定死的绝对路径。进程一跑就是几周，
    # 期间仓库被挪走/改名的话它就指向一个不存在的文件 —— Python 的
    # 「can't open file」退出码 2，看门狗只能补一句「exited with code 2」，
    # 而用户被引向「重新体检」，那查的是 CLI 装没装，跟这毫无关系。
    # 能精确检测的事就不该退化成通用崩溃。（2026-08-31 用户实际踩到）
    if not os.path.exists(MAIN_PATH):
        msg = (f"引擎入口不见了：{MAIN_PATH}。Agent Hub 启动之后仓库被移动或"
               f"改名了，正在运行的服务还记着旧路径 —— 重启 Agent Hub 即可"
               f"（这跟 agent 的安装状态无关，做体检没有用）。")
        if RUNS_DIR and _run_meta.get("id"):
            _record_crash(_run_meta["id"], msg, _run_meta["events"])
        raise OSError(msg)
    argv = [sys.executable, MAIN_PATH, *argv_tail]
    if RUNS_DIR:
        argv += [f"--board={BOARD_PATH}", f"--events={EVENTS_PATH}",
                 f"--workdir={WORKDIR}", f"--matrix={MATRIX}"]
    # Give main.py its own session/process group (POSIX) or process group
    # (Windows) so POST /cancel can kill the WHOLE tree in one shot -- the
    # claude/dsh/codebuddy child subprocesses main.py spawns via
    # asyncio.create_subprocess_exec inherit this group by default (they
    # never set start_new_session themselves). Killing only main.py's own
    # pid would leave those children orphaned and still billing, invisibly.
    popen_kwargs = ({"start_new_session": True} if os.name != "nt"
                    else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP})
    try:
        _current = subprocess.Popen(argv, **popen_kwargs)
    except OSError as e:
        # a run dir may have been created just above: record the crash so the
        # events stream says what happened instead of the UI spinning forever
        _current = None
        if RUNS_DIR and _run_meta.get("id"):
            _record_crash(_run_meta["id"], f"spawn failed: {e}",
                          _run_meta["events"])
        raise
    if RUNS_DIR:
        _run_meta["proc"] = _current
        # Fresh dict every spawn (not setdefault): a --resume reuses the
        # same _run_meta object, and a run that was previously cancelled
        # must not leave a stale cancel_requested=True poisoning the next
        # one's watchdog verdict.
        _run_meta["cancel_flag"] = {"cancel_requested": False}
        threading.Thread(target=_watch_run,
                         args=(_run_meta["id"], _current, EVENTS_PATH,
                               _run_meta["cancel_flag"]),
                         daemon=True).start()
    return _current.pid


def _events_tail(path: str) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _tail_has_run_finished(tail: str) -> bool:
    for line in tail.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            if json.loads(line).get("type") == "run_finished":
                return True
        except json.JSONDecodeError:
            continue
    return False


def _run_finished_summary(tail: str) -> tuple[str, float]:
    status, cost = "done", 0.0
    for line in tail.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") == "run_finished":
            statuses = evt.get("statuses") or {}
            status = "failed" if any(v == "failed" for v in statuses.values()) else "done"
            cost = float(evt.get("total_cost_usd") or 0.0)
    return status, cost


def _read_events_full(path: str) -> list[dict]:
    """Full-history read of an events.jsonl. Unlike _events_tail (last
    64KB, for the watchdog's lightweight "did run_finished happen" check),
    export needs EVERY task_completed/agent_failed event -- per-task events
    fall out of the tail window on longer runs and the cost/status math
    would come out wrong."""
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def _task_rows_for_export(board, events: list[dict]) -> list[dict]:
    """One row per task id found on the blackboard, with terminal status
    and costs merged in from the event stream (the board alone doesn't
    carry cost/terminal state)."""
    tids = sorted({k[len("tasks/"):-len("/spec")] for k in board.keys()
                   if k.startswith("tasks/") and k.endswith("/spec")})
    cost_by, credits_by = {}, {}
    status_by, error_by, agent_by = {}, {}, {}
    for e in events:
        tid = e.get("task_id")
        if not tid:
            continue
        if e.get("type") == "task_completed":
            # task_completed.cost_usd is already the engine's FINAL running
            # total (includes all failed attempts) -- assign, don't add.
            status_by[tid] = "done"
            cost_by[tid] = float(e.get("cost_usd") or 0.0)
            credits_by[tid] = float(e.get("cost_credits") or 0.0)
            agent_by[tid] = e.get("agent") or ""
        elif e.get("type") == "task_failed":
            status_by[tid] = "failed"
            error_by[tid] = e.get("error") or ""
        elif e.get("type") == "agent_failed":
            # Failed attempts only matter if the task never completed:
            # a later task_completed overwrites these accumulated values.
            cost_by[tid] = cost_by.get(tid, 0.0) + float(e.get("cost_usd") or 0.0)
            credits_by[tid] = credits_by.get(tid, 0.0) + float(e.get("cost_credits") or 0.0)
    rows = []
    for tid in tids:
        spec = board.get(f"tasks/{tid}/spec") or {}
        rows.append({
            "id": tid,
            "title": spec.get("title", tid),
            "type": spec.get("type", ""),
            "status": status_by.get(tid) or board.get(f"tasks/{tid}/status") or "unknown",
            "agent": agent_by.get(tid) or board.get(f"tasks/{tid}/agent") or "",
            "cost_usd": round(cost_by.get(tid, 0.0), 4),
            "cost_credits": round(credits_by.get(tid, 0.0), 4),
            "error": error_by.get(tid, ""),
            "files": board.get(f"tasks/{tid}/files") or [],
        })
    return rows


_STATUS_ZH = {"done": "完成", "failed": "失败", "stale": "产出已过期"}


def _fmt_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f}MB"
    if n >= 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n}B"


def _render_export_summary(run_id: str, board, events: list[dict],
                           rows: list[dict], included: list[tuple],
                           skipped: list[tuple]) -> str:
    goal = board.get("goal/statement") or ""
    title = (goal[:80] or "Agent Hub 运行摘要")
    constraints = board.get("goal/constraints")

    # run-level terminal state, priority order cancelled > crashed > finished
    run_status = "进行中（导出时运行尚未结束）"
    finished = None
    for e in events:
        t = e.get("type")
        if t == "run_cancelled":
            run_status = "已取消"
        elif t == "run_crashed":
            run_status = "崩溃：" + (e.get("error") or "未知错误")
        elif t == "run_finished":
            finished = e
    if finished:
        statuses = finished.get("statuses") or {}
        run_status = ("完成 有任务失败" if any(v == "failed" for v in statuses.values())
                      else "完成 全部交付")

    total = sum(r["cost_usd"] for r in rows)
    note = ""
    if finished:
        total_full = round(float(finished.get("total_cost_usd") or 0.0), 4)
        total_paid = round(float(finished.get("paid_api_cost_usd") or 0.0), 4)
    else:
        total_full = total_paid = round(total, 4)
        note = "（运行未完全结束，此数字可能不完整）"

    lines = [
        f"# {title}",
        "",
        f"- 运行 ID：{run_id or '(未分配，单文件模式)'}",
        f"- 导出时间：{datetime.datetime.now().isoformat(timespec='seconds')}",
        f"- 运行状态：{run_status}",
        f"- 总花费（含 Summarizer 摘要开销）：${total_full:.4f}{note}",
        f"- 护栏统计口径花费（仅任务执行本身的按量付费，跟仪表盘成本面板一致）：${total_paid:.4f}{note}",
        "- 说明：claude_code 订阅额度与 codebuddy 积分消耗不计入以上美元数字；"
        "dsh 花费本项目测不到（详见项目文档），不在此统计范围内",
        "",
        "## 目标",
        "",
        goal or "（未记录）",
    ]
    if constraints:
        lines += ["", "## 约束", "", str(constraints)]
    lines += ["", "## 任务清单", "",
              "| 任务 | 状态 | 执行者 | 花费($) | 花费(积分) | 交付文件 |",
              "|---|---|---|---|---|---|"]
    for r in rows:
        file_list = "、".join(f.get("path", "") for f in r["files"]) or "—"
        lines.append(f"| {r['title']}（`{r['id']}`） | {_STATUS_ZH.get(r['status'], '进行中/未完成')} | "
                     f"{r['agent'] or '—'} | {r['cost_usd']:.4f} | {r['cost_credits']:.4f} | {file_list} |")
    for r in rows:
        if r["status"] == "failed":
            lines.append("")
            lines.append(f"> **`{r['id']}` 失败原因**：{r['error'] or '（未记录）'}")
    lines += ["", "## 交付文件索引", ""]
    by_task = {}
    for tid, rel, size in included:
        by_task.setdefault(tid, []).append((rel, size))
    for tid in sorted(by_task):
        lines.append(f"- `{tid}/`")
        for rel, size in sorted(by_task[tid]):
            lines.append(f"  - {rel}（{_fmt_size(size)}）")
    if not included:
        lines.append("（本次导出没有包含任何交付文件）")
    if skipped:
        lines += ["", f"## 未打包的文件（超出本次导出体积上限 {MAX_EXPORT_BYTES // (1024 * 1024)}MB）", ""]
        for tid, rel, size in skipped:
            lines.append(f"- `{tid}/{rel}`（{_fmt_size(size)}）")
    return "\n".join(lines) + "\n"


def _resolve_run(run_id: str) -> tuple[str, str, str] | None:
    """(run_id, board_path, events_path); empty run_id means the current
    run. None when not found or when run_id tries to escape RUNS_DIR --
    run_id comes straight from the query string, a brand-new attack
    surface GET /file never had (its task param is only ever a blackboard
    key, never a filesystem component), so it gets the same realpath
    containment treatment GET /file gives file paths."""
    if not RUNS_DIR:
        return ("", BOARD_PATH, EVENTS_PATH)
    rid = run_id or _run_meta.get("id", "")
    if not rid:
        return None
    runs_root = os.path.realpath(RUNS_DIR)
    run_dir = os.path.realpath(os.path.join(RUNS_DIR, rid))
    if not run_dir.startswith(runs_root + os.sep) or not os.path.isdir(run_dir):
        return None
    board_path = os.path.join(run_dir, "blackboard.jsonl")
    events_path = os.path.join(run_dir, "events.jsonl")
    if not os.path.exists(board_path):
        return None
    return (rid, board_path, events_path)


def _snippet(text: str, q: str, width: int = 60) -> str:
    """A short window around the first case-insensitive hit of q in text.
    No ellipses, no <mark> -- the design doc deliberately keeps snippets
    plain (§3.2/§5)."""
    t, needle = text.lower(), q.lower()
    pos = t.find(needle)
    if pos < 0:
        return text[:width]   # defensive; callers only call after matching
    half = max(0, (width - len(q)) // 2)
    return text[max(0, pos - half):pos + len(q) + half]


def _search_runs(q: str) -> dict:
    """Linear scan of index.jsonl (in-memory _runs, newest first) plus each
    run's blackboard.jsonl. No index, no output-text search: the design doc
    (§2/§3.1) argues the per-user scale makes a persistent index overkill,
    and tasks/*/output is deliberately out of scope."""
    ql = q.lower()
    results: list[dict] = []
    scanned = 0

    def hit(run, matched_in, task_id, snippet):
        results.append({"run_id": run.get("run_id"), "goal": run.get("goal"),
                        "ts": run.get("ts"), "status": run.get("status"),
                        "cost_usd": run.get("cost_usd"),
                        "matched_in": matched_in, "task_id": task_id,
                        "snippet": snippet})

    for run in reversed(_runs):
        if len(results) >= MAX_SEARCH_RESULTS:
            break
        scanned += 1
        goal = str(run.get("goal") or "")
        if ql in goal.lower():
            hit(run, "goal", None, _snippet(goal, q))
        if not RUNS_DIR:
            continue
        board_path = os.path.join(RUNS_DIR, run.get("run_id", ""), "blackboard.jsonl")
        if not os.path.exists(board_path):
            continue
        board = Blackboard(board_path, on_write=lambda e: None)
        for e in board.query("tasks/"):
            key = e.get("key") or ""
            if key.endswith("/spec"):
                title = str((e.get("value") or {}).get("title") or "")
                if title and ql in title.lower():
                    # spec writes DON'T pass task_id (hub/engine.py), so the
                    # entry field is None -- parse the id from the key itself
                    hit(run, "task_title", key.split("/")[1], _snippet(title, q))
            elif key.endswith("/files"):
                for f in (e.get("value") or []):
                    path = str(f.get("path") or "")
                    if path and ql in path.lower():
                        hit(run, "file", e.get("task_id"), path)
        for e in board.query("notes/"):
            text = str(e.get("value") or "")
            if text and ql in text.lower():
                hit(run, "note", e.get("task_id"), _snippet(text, q))
    return {"query": q, "total_runs_scanned": scanned,
            "truncated": len(results) >= MAX_SEARCH_RESULTS, "results": results}


def _record_crash(run_id: str, error: str, events_path: str) -> None:
    try:
        bus = EventBus(events_path)
        bus.emit("run_crashed", run_id=run_id, error=error[:500])
        bus.close()
    except OSError:
        pass
    _runs_update(run_id, status="crashed")


def _record_cancelled(run_id: str, events_path: str) -> None:
    """POST /cancel killed this run's whole process group on purpose --
    keep that distinct from _record_crash (an unrequested, unexpected
    exit): the frontend needs to say "you cancelled this" rather than
    "the engine crashed", and index.jsonl's status should say so too."""
    try:
        bus = EventBus(events_path)
        bus.emit("run_cancelled", run_id=run_id)
        bus.close()
    except OSError:
        pass
    _runs_update(run_id, status="cancelled")


def _watch_run(run_id: str, proc, events_path: str, cancel_flag: dict) -> None:
    """One daemon thread per run: when the engine exits, decide how it ended
    and keep index.jsonl (and, on a crash, the event stream) honest. An
    engine that exits nonzero WITHOUT a run_finished event crashed -- the
    frontend would otherwise spin on "running" forever. A deliberate
    POST /cancel also exits nonzero without run_finished (SIGTERM has no
    handler installed in main.py), so cancel_flag is checked FIRST -- same
    exit shape, opposite meaning, and only the caller who set the flag
    knows which one this was."""
    try:
        proc.wait()
    except Exception:
        return
    if cancel_flag.get("cancel_requested"):
        _record_cancelled(run_id, events_path)
        return
    tail = _events_tail(events_path)
    if proc.returncode != 0 and not _tail_has_run_finished(tail):
        _record_crash(run_id, f"engine exited with code {proc.returncode} before run_finished",
                      events_path)
    elif _tail_has_run_finished(tail):
        status, cost = _run_finished_summary(tail)
        _runs_update(run_id, status=status, cost_usd=round(cost, 4))
    else:
        _runs_update(run_id, status="done")


def _run_busy() -> bool:
    return bool(_current and _current.poll() is None)


def _posix_descendant_process_groups(root_pid: int) -> list[int]:
    """Snapshot process groups owned by descendants of `root_pid`.

    Most executors inherit main.py's group, but Codex deliberately starts a
    separate group so an executor timeout can kill Codex plus its tool
    children without killing the whole run. POST /cancel therefore needs
    both groups. This uses one argv-only `ps` snapshot and falls back to the
    root group if process inspection is unavailable.
    """
    try:
        root_pgid = os.getpgid(root_pid)
    except (ProcessLookupError, OSError):
        return []
    groups: list[int] = []
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,pgid="],
            capture_output=True, text=True, timeout=3, check=False)
        children: dict[int, list[tuple[int, int]]] = {}
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) != 3:
                continue
            try:
                pid, ppid, pgid = map(int, fields)
            except ValueError:
                continue
            children.setdefault(ppid, []).append((pid, pgid))
        stack = [root_pid]
        seen_pids = {root_pid}
        while stack:
            parent = stack.pop()
            for pid, pgid in children.get(parent, []):
                if pid in seen_pids:
                    continue
                seen_pids.add(pid)
                stack.append(pid)
                if pgid != root_pgid and pgid not in groups:
                    groups.append(pgid)
    except (OSError, subprocess.SubprocessError):
        pass
    # Detached descendant groups go first. If main.py dies first they are
    # reparented and can no longer be rediscovered from the original PID.
    groups.append(root_pgid)
    own_group = os.getpgrp()
    return [pgid for pgid in groups if pgid > 0 and pgid != own_group]


def _serve_process_group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _terminate_current_process_tree(proc) -> bool:
    """Terminate main.py and descendants, including nested process groups.

    Return False only when the platform tree-kill mechanism itself failed
    while the root process was still alive; callers must not report a false
    successful cancellation in that case.
    """
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=10, check=False)
            if result.returncode == 0 or proc.poll() is not None:
                return True
        except (OSError, subprocess.SubprocessError):
            result = None
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        except (ProcessLookupError, OSError):
            return proc.poll() is not None
        return False

    try:
        root_pgid = os.getpgid(proc.pid)
        # Freeze main.py's group before taking the descendant snapshot so it
        # cannot launch a new detached executor in the scan/signal race.
        os.killpg(root_pgid, signal.SIGSTOP)
    except (ProcessLookupError, OSError):
        return proc.poll() is not None
    groups = _posix_descendant_process_groups(proc.pid)
    for pgid in groups:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGTERM)
    # SIGTERM is pending while the root group is stopped; resume it so it can
    # shut down cleanly during the grace interval.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(root_pgid, signal.SIGCONT)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and any(
            _serve_process_group_alive(pgid) for pgid in groups):
        time.sleep(0.05)
    for pgid in groups:
        if _serve_process_group_alive(pgid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)
    return True


# ---------- best-effort in-process scheduler (NOT OS-level cron) ----------

SCHEDULE_POLL_SEC = 60     # how often _schedule_loop wakes up
SCHEDULE_GRACE_SEC = 300   # after due time, still willing to fire (5 min)


def _schedule_day_matches(entry: dict, now: datetime.datetime) -> bool:
    freq = entry.get("freq")
    if freq == "daily":
        return True
    if freq == "weekly":
        return now.weekday() == int(entry.get("weekday", -1))
    return False


def _schedule_seconds_past_due(entry: dict, now: datetime.datetime) -> float | None:
    """Seconds since today's HH:MM for this entry; negative = not yet due.
    None = malformed entry (skip it, don't let one dirty record stall the
    whole loop)."""
    try:
        hour, minute = int(entry["hour"]), int(entry["minute"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return (now - scheduled).total_seconds()


def _schedule_fire(entry: dict, now: datetime.datetime) -> bool:
    """Attempts to spawn via the atomic _spawn_main_if_idle. Returns False
    (leaving the entry's bookkeeping untouched) when it loses the race to
    spawn -- e.g. an HTTP-triggered run started between _schedule_tick's
    own busy pre-check and this call -- so the caller treats it exactly
    like that pre-check failing: retry next tick, still within the grace
    window, not a permanent "missed" for today. A real OSError (spawn
    itself broken) is different: retrying won't help, so that's recorded
    immediately.

    `now` is passed in rather than read off the wall clock: the caller
    already decided what "today" means for the once-per-day check, and
    stamping last_fired_date from a SECOND, independently-read clock can
    disagree with it across a midnight boundary -- which would let the
    same entry fire twice. (It also keeps _schedule_tick's documented
    "pure function of now" contract actually true, so tests can inject a
    fixed date instead of only passing on the day they were written.)"""
    try:
        pid = _spawn_main_if_idle([f"--goal={entry['goal']}"]
                                  + _gate_argv(force_auto=True))
    except OSError as e:
        entry["last_fired_status"] = f"spawn_failed: {e}"[:200]
        entry["last_fired_date"] = now.date().isoformat()
        print(f"[scheduler] fired '{entry.get('name')}': {entry['last_fired_status']}")
        return True
    if pid is None:
        return False
    entry["last_fired_status"] = "fired"
    entry["last_fired_date"] = now.date().isoformat()
    print(f"[scheduler] fired '{entry.get('name')}': fired")
    return True


def _schedule_tick(now: datetime.datetime | None = None) -> None:
    """One pass over schedules.json. Pure function of `now` (injectable for
    tests, like _watch_run takes its args explicitly): the daemon thread is
    just a shell that calls this every SCHEDULE_POLL_SEC. Once per day per
    entry, best-effort only: the app being closed or busy past the grace
    window is recorded as 'missed', never retried, never back-filled.
    The whole read-modify-write of schedules.json is one critical section
    (_schedules_lock) so a concurrent POST /schedules(/delete) can't lose
    an update to this tick's, or vice versa."""
    if not SCHEDULES_PATH:
        return
    now = now or datetime.datetime.now()
    today = now.date().isoformat()
    with _schedules_lock:
        schedules = _schedules_load()
        changed = False
        for entry in schedules:
            if not entry.get("enabled", True):
                continue
            if entry.get("last_fired_date") == today:
                continue   # one verdict per day, whatever yesterday's was
            if not _schedule_day_matches(entry, now):
                continue
            delta = _schedule_seconds_past_due(entry, now)
            if delta is None or delta < 0:
                continue
            if delta <= SCHEDULE_GRACE_SEC:
                if _run_busy():
                    continue   # still inside the grace window: try again next tick
                if _schedule_fire(entry, now):
                    changed = True
                # else: lost the spawn race inside _schedule_fire -- leave
                # the entry untouched, retry next tick like the busy
                # pre-check above
            else:
                entry["last_fired_date"] = today
                entry["last_fired_status"] = "missed"
                print(f"[scheduler] '{entry.get('name')}' missed its window, skipped")
                changed = True
        if changed:
            _schedules_save(schedules)


def _schedule_loop() -> None:
    while True:
        try:
            _schedule_tick()
        except Exception as e:
            # wide on purpose: a dead scheduler thread would silently kill
            # every scheduled goal forever with zero signal
            print(f"[scheduler] tick failed: {e}")
        time.sleep(SCHEDULE_POLL_SEC)


def _allowed_origins() -> set[str]:
    return {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}


MAX_PREVIEW_BYTES = 10 * 1024 * 1024   # refuse to slurp a huge file into RAM
# Cap on the summed ORIGINAL size of all files packed into one /export zip.
# Judgment call (no measured basis): over-cap files are skipped and listed
# honestly in SUMMARY.md instead of failing the whole export.
MAX_EXPORT_BYTES = 200 * 1024 * 1024
# Soft cap for /settings/search: checked per run before scanning it, NOT
# mid-run, so a single run that hits many times may push the total slightly
# over -- deliberate, keeps the scan loop simple (see the cross-run-search
# design doc §3.3).
MAX_SEARCH_RESULTS = 200


class Handler(SimpleHTTPRequestHandler):
    # Socket timeout per connection. BaseHTTPRequestHandler defaults this
    # to None, which means a client that opens a TCP connection and then
    # never sends a request line parks a handler thread in readline()
    # FOREVER. Browsers do exactly that routinely (preconnect /
    # speculative sockets), so over a long-running session threads grow
    # without bound -- measured: 300 such connections held 302 threads,
    # all released only when the client finally closed. This is a
    # per-socket-operation timeout, not a request budget: a slow handler
    # (e.g. /settings/preflight doing real LLM calls for minutes) is
    # unaffected because it isn't blocked on the socket while it works.
    timeout = 60

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/file":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._handle_file((qs.get("task") or [""])[0], (qs.get("path") or [""])[0])
        elif path == "/file/diff":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._handle_file_diff((qs.get("task") or [""])[0], (qs.get("path") or [""])[0])
        elif path == "/export":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._handle_export((qs.get("run") or [""])[0])
        elif path == "/events.jsonl":
            self._handle_events()
        elif path == "/blackboard.jsonl":
            # Raw board access for debugging/introspection. Added for the
            # redesign frontend (deleted 2026-09-05); dashboard.html never
            # used it, but a read-only view of the real board is worth
            # keeping on its own -- tests/test_config_validation.py covers it.
            self._handle_blackboard()
        elif path == "/health":
            self._handle_health()
        elif path == "/settings":
            self._handle_settings()
        elif path == "/settings/search":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._handle_settings_search((qs.get("q") or [""])[0])
        elif path == "/schedules":
            self._handle_schedules_list()
        elif path == "/chat/session/current":
            self._handle_chat_session_current()
        elif path == "/chat/progress":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._reply(200, {"steps": _progress_read((qs.get("session_id") or [""])[0])})
        elif path == "/settings/agents":
            self._handle_settings_agents()
        elif path == "/settings/projects":
            self._handle_settings_projects()
        elif path == "/settings/tools":
            self._handle_settings_tools()
        elif path == "/agents":
            self._handle_agents()
        else:
            super().do_GET()

    def do_POST(self):
        # CSRF (brief 1.5): a JSON Content-Type forces browsers into a CORS
        # preflight this server never answers, and an Origin allowlist only
        # admits our own localhost origins. Both must pass for every POST.
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._reply(400, {"error": "Content-Type must be application/json"})
            return
        origin = self.headers.get("Origin")
        if origin and origin not in _allowed_origins():
            self._reply(403, {"error": "cross-origin request rejected"})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        except (json.JSONDecodeError, ValueError):
            self._reply(400, {"error": "bad json"})
            return
        if self.path == "/run":
            self._handle_run(body)
        elif self.path == "/rerun":
            self._handle_rerun(body)
        elif self.path == "/cancel":
            self._handle_cancel(body)
        elif self.path == "/gate":
            self._handle_gate(body)
        elif self.path == "/chat/session/discard":
            self._handle_chat_session_discard(body)
        elif self.path == "/chat":
            self._handle_chat(body)
        elif self.path.startswith("/task/") and self.path.endswith("/message"):
            task_id = self.path[len("/task/"):-len("/message")]
            self._handle_task_message(task_id, body)
        elif self.path == "/shutdown":
            self._handle_shutdown(body)
        elif self.path == "/settings/preflight":
            self._handle_preflight(body)
        elif self.path == "/settings/config":
            self._handle_settings_config(body)
        elif self.path == "/settings/credentials":
            self._handle_settings_credentials(body)
        elif self.path == "/settings/switch-project":
            self._handle_switch_project(body)
        elif self.path == "/settings/tools":
            self._handle_settings_tools_save(body)
        elif self.path == "/settings/pick-workdir":
            self._handle_pick_workdir(body)
        elif self.path == "/settings/workdir":
            self._handle_save_workdir(body)
        elif self.path == "/settings/reveal-workdir":
            self._handle_reveal_workdir(body)
        elif self.path == "/schedules":
            self._handle_schedules_save(body)
        elif self.path == "/schedules/delete":
            self._handle_schedules_delete(body)
        else:
            # JSON like every other POST reply (send_error would emit an
            # HTML page); there is no static-file fallback on POST, so an
            # unknown path here is always an API mistake
            self._reply(404, {"error": f"unknown endpoint: {self.path}"})

    # ---------- GET handlers ----------

    def _handle_events(self):
        """The current run's event stream. The X-AgentHub-Run header is how
        frontends detect a run switch and reset their byte offset (each run
        has its own file, which can be SHORTER than the previous one)."""
        self._serve_local_jsonl(EVENTS_PATH)

    def _handle_blackboard(self):
        self._serve_local_jsonl(BOARD_PATH)

    def _serve_local_jsonl(self, path: str):
        if not os.path.exists(path):
            # JSON, not send_error's HTML page: every other API endpoint
            # answers in JSON, and a frontend that does res.json() on the
            # error path shouldn't get a parse failure instead of a
            # readable reason. NOTE this 404 is normal, not a fault -- it
            # is what "this project has no run yet" looks like.
            self._reply(404, {"error": "no run yet for this project"})
            return
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if RUNS_DIR:
            self.send_header("X-AgentHub-Run", _run_meta.get("id", ""))
        self.end_headers()
        self.wfile.write(data)

    def _handle_health(self):
        self._reply(200, {"version": VERSION, "commit": COMMIT, "data_dir": DATA_DIR,
                          "project": PROJECT, "busy": _run_busy(), "port": PORT})

    def _handle_settings(self):
        budget, routes, tiers = 2.0, {}, {}
        try:
            m = _load_yaml_dict(MATRIX)
            budget = float((m.get("budget") or {}).get("max_usd_per_run", 2.0))
            routes = m.get("routes") or {}
            tiers = m.get("tiers") or {}
        except (AttributeError, TypeError, ValueError):
            pass   # a structurally odd matrix falls back to the defaults above
        creds = _credentials_read()
        preflight = None
        if PREFLIGHT_PATH and os.path.exists(PREFLIGHT_PATH):
            try:
                with open(PREFLIGHT_PATH, encoding="utf-8") as f:
                    preflight = json.load(f)
            except (OSError, json.JSONDecodeError):
                preflight = None
        self._reply(200, {
            "version": VERSION, "commit": COMMIT,
            "data_dir": DATA_DIR, "project": PROJECT,
            "runtime_dir": RUNTIME_DIR, "credentials_path": CREDENTIALS_PATH,
            "workdir": WORKDIR,
            "port": PORT, "busy": _run_busy(),
            "budget": budget, "routes": routes, "tiers": tiers,
            "matrix_patch": _matrix_patch(),
            "ui_prefs": _ui_prefs(),
            "agents": _probe_agents(),
            "credentials": {
                "deepseek": bool(os.environ.get("DEEPSEEK_API_KEY")
                                 or creds.get("DEEPSEEK_API_KEY")),
                "openrouter": bool(os.environ.get("OPENROUTER_API_KEY")
                                   or creds.get("OPENROUTER_API_KEY")),
            },
            "preflight": preflight,
            "runs": list(_runs),
            "schedules": _schedules_load(),
        })

    def _handle_settings_agents(self):
        self._reply(200, {"agents": _probe_agents()})

    def _handle_agents(self):
        """Return matrix agent metadata with honest kind-level availability."""
        matrix = (MERGED_MATRIX_PATH if MERGED_MATRIX_PATH and
                  os.path.exists(MERGED_MATRIX_PATH) else MATRIX)
        try:
            router = Router(matrix, workdir=WORKDIR, mock_all=True)
        except Exception as exc:
            self._reply(500, {"error": f"读不了能力矩阵: {exc}"[:200]})
            return
        probe = {item["id"]: item for item in _probe_agents()}
        agents = []
        for name in router.all_agents():
            kind = router.kind(name)
            kind_probe = probe.get(kind)
            agents.append({
                "name": name,
                "kind": kind,
                "animal": router.animal(name),
                "tier": router.tier_of(name),
                "specialty": router.specialty(name),
                "blurb": router.blurb(name),
                "installed": (True if kind_probe is None
                              else bool(kind_probe.get("available"))),
                "detail": ("" if kind_probe is None
                           else str(kind_probe.get("detail") or "")),
            })
        self._reply(200, {
            "agents": agents,
            "precision": "kind",
            "note": "仅探测到 executor 是否安装，未验证这个具体模型可用",
        })

    def _handle_settings_projects(self):
        self._reply(200, {"current": PROJECT, "projects": _scan_projects()})

    def _tools_snapshot(self):
        """`base` is the FULL candidate set (repo's own matrix -- always
        offered, so a previously-removed tool can be re-checked); `allowed`
        is the current effective (checked) subset. Read from MATRIX_BASE's
        agent list, not the merged matrix's, so a fully-restored agent
        (whose patch entry we've since deleted, see _handle_settings_
        tools_save) still shows up."""
        out = []
        agents = _load_yaml_dict(MATRIX_BASE).get("agents")
        for name in sorted(agents if isinstance(agents, dict) else {}):
            base = _base_allowed_tools(name)
            if not base:
                continue
            eff = _effective_allowed_tools(name)
            allowed, patched = eff if eff else (base, False)
            out.append({"name": name, "base": base, "allowed": allowed,
                        "patched": patched})
        return out

    def _handle_settings_tools(self):
        self._reply(200, {"agents": self._tools_snapshot()})

    def _handle_settings_tools_save(self, body: dict):
        """Tighten-only relative to the matrix's OWN base set (not the
        currently-effective set) -- this is what makes a removal
        reversible: re-checking a tool the base matrix already grants is
        allowed even if a prior save had removed it, because it was never
        "added beyond the matrix", it's just back to baseline. Only a tool
        genuinely absent from the base matrix is rejected. Server-side
        validation is the final line -- the frontend checkboxes
        structurally can't add beyond `base`, but this must not trust
        that. Empty list clamps to ["Read"] (an empty string empirically
        does NOT restrict tools; Read is the proven minimum)."""
        if not CONFIG_PATH:
            self._reply(400, {"error": "no data directory configured"})
            return
        name = str(body.get("agent") or "")
        base = _base_allowed_tools(name)
        if base is None:
            self._reply(400, {"error": "agent not found or not a claude_code agent"})
            return
        allowed = body.get("allowed")
        if not isinstance(allowed, list) or not all(
                isinstance(t, str) and t.strip() for t in allowed):
            self._reply(400, {"error": "allowed must be a list of tool names"})
            return
        allowed = [t.strip() for t in allowed]
        extra = sorted(set(allowed) - set(base))
        if extra:
            self._reply(400, {"error": f"not in this agent's matrix tool set: {', '.join(extra)}"})
            return
        clamped = False
        if not allowed:
            allowed = ["Read"]
            clamped = True
        elif "Read" not in allowed:
            self._reply(400, {"error": "Read 是基础权限，不能单独去掉"})
            return
        cfg = _load_config()
        patch = _matrix_patch(cfg)
        if not isinstance(patch.get("agents"), dict):
            patch.pop("agents", None)   # a malformed agents value is discarded, not merged into
        if set(allowed) == set(base):
            # fully restored to the matrix's own baseline -- drop the
            # override entirely rather than persist a no-op patch forever
            patch.get("agents", {}).pop(name, None)
        else:
            # setdefault semantics: touch only this one key; other
            # existing patch fields for this agent survive untouched
            entry = patch.setdefault("agents", {}).get(name)
            if not isinstance(entry, dict):
                entry = patch["agents"][name] = {}
            entry["allowed_tools"] = ", ".join(allowed)
        cfg["matrix_patch"] = patch
        _save_config(cfg)
        _write_merged_matrix()
        self._reply(200, {"ok": True, "clamped": clamped,
                          "agents": self._tools_snapshot()})

    def _handle_file_diff(self, task_id: str, rel: str):
        """Live git diff (working tree vs HEAD) for one recorded
        deliverable. The file passes the exact same whitelist + realpath
        containment as GET /file BEFORE any subprocess exists; the path is
        then passed to git as a literal argv element (no shell, ever)."""
        resolved = _resolve_deliverable(task_id, rel)
        if resolved is None:
            self._reply(404, {"error": "not a recorded deliverable of this task"})
            return
        workdir, _full = resolved
        if shutil.which("git") is None:
            self._reply(200, {"ok": False, "error": "git not found"})
            return
        if not os.path.isdir(os.path.join(workdir, ".git")):
            self._reply(200, {"ok": False, "error": "workdir 不是 git 仓库，无法对比改动"})
            return
        try:
            # --literal-pathspecs: rel already passed the exact-string
            # whitelist match in _resolve_deliverable, so it must be
            # taken as a literal path here too -- without this flag git
            # still parses pathspec magic (":(glob)...", ":/...") out of
            # an argv element even after "--", so a deliverable that
            # happens to be named that way could make git diff something
            # other than the whitelisted file.
            r = subprocess.run(["git", "-C", workdir, "--literal-pathspecs",
                               "diff", "--", rel],
                               capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            self._reply(200, {"ok": False, "error": "git diff timed out"})
            return
        except OSError:
            self._reply(200, {"ok": False, "error": "git 启动失败"})
            return
        diff = r.stdout or ""
        truncated = len(diff) > MAX_DIFF_CHARS
        if truncated:
            diff = diff[:MAX_DIFF_CHARS]
        self._reply(200, {"ok": True, "diff": diff, "truncated": truncated,
                          "note": (r.stderr or "")[:200]})

    def _handle_switch_project(self, body: dict):
        """Validate + persist the choice, then shut down exactly like
        _handle_shutdown -- see that method's docstring for the two
        deadlock/hang traps this mirrors. No in-process hot switch: the
        NEXT process start reads last_project.json via
        _load_last_project(); re-running _configure() in THIS process is
        not safe (design doc §2 walks through every stale-global)."""
        if not DATA_DIR:
            self._reply(400, {"error": "no data directory configured"})
            return
        name = str(body.get("project") or "").strip()
        if not _PROJECT_NAME_RE.match(name):
            self._reply(400, {"error": "invalid project name (use letters, "
                                        "digits, - or _, max 64 chars)"})
            return
        if _run_busy():
            self._reply(409, {"error": _busy_error()})
            return
        # validation and the busy check BOTH come before the write --
        # a rejected switch must not leave a marker behind
        _atomic_write(os.path.join(DATA_DIR, "last_project.json"),
                      json.dumps({"project": name}))
        self._reply(200, {"ok": True, "project": name})
        if SERVER is not None:
            threading.Thread(target=SERVER.shutdown, daemon=True).start()

    # ---------- work area (workdir) picker ----------

    def _handle_pick_workdir(self, body: dict):
        """macOS native folder chooser via a FIXED osascript command (no
        user input ever reaches the argv -- nothing to inject). The chosen
        path is only a candidate string: it takes effect only after
        _handle_save_workdir validates it. Non-macOS: honest 400, manual
        path input is the fallback."""
        if sys.platform != "darwin":
            self._reply(400, {"error": "自动选择文件夹目前只在 macOS 支持，请手动粘贴路径"})
            return
        script = 'POSIX path of (choose folder with prompt "选择 Agent Hub 的工作区域")'
        try:
            # timeout is mandatory here: the chooser blocks until the user
            # acts, and a dialog that opened behind another window (or was
            # simply ignored) would otherwise park this handler thread
            # forever. 5 minutes is generous for picking a folder; on
            # expiry subprocess.run kills osascript, closing the dialog.
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            self._reply(200, {"cancelled": True,
                              "error": "选择框超时未操作（5 分钟），已取消"})
            return
        except OSError as e:
            self._reply(400, {"error": f"osascript 启动失败: {e}"})
            return
        path = (r.stdout or "").strip()
        if r.returncode != 0 or not path:
            self._reply(200, {"cancelled": True})
            return
        self._reply(200, {"path": path})

    def _handle_save_workdir(self, body: dict):
        """Persist a workdir override (config.yaml `workdir` key) and apply
        it to the in-process WORKDIR global immediately. WORKDIR is only
        consumed at _spawn_main time (the engine subprocess receives
        --workdir and records run/workdir on the blackboard), so the next
        run uses the new folder while past runs keep their recorded one --
        no restart, no busy-guard needed. Empty path = reset to default."""
        global WORKDIR
        if not CONFIG_PATH:
            self._reply(400, {"error": "no data directory configured"})
            return
        raw = str(body.get("path") or "").strip()
        cfg = _load_config()
        if not raw:
            cfg.pop("workdir", None)
            _save_config(cfg)
            WORKDIR = os.path.join(RUNTIME_DIR, "workdir")
            os.makedirs(WORKDIR, exist_ok=True)
            self._reply(200, {"ok": True, "workdir": WORKDIR})
            return
        if not os.path.isabs(raw):
            self._reply(400, {"error": "必须是绝对路径"})
            return
        full = os.path.realpath(raw)
        if not os.path.isdir(full):
            self._reply(400, {"error": "文件夹不存在或不是目录"})
            return
        if full == os.path.realpath(DATA_DIR) or \
                full.startswith(os.path.realpath(DATA_DIR) + os.sep):
            self._reply(400, {"error": "工作区域不能在数据目录内部（会跟运行记录混在一起）"})
            return
        cfg["workdir"] = full
        _save_config(cfg)
        WORKDIR = full
        self._reply(200, {"ok": True, "workdir": full})

    def _handle_reveal_workdir(self, body: dict):
        """Open the CURRENTLY CONFIGURED workdir in the OS file manager --
        server-side value only, never user input."""
        if not os.path.isdir(WORKDIR):
            self._reply(400, {"error": "当前工作区域不存在（文件夹可能已被删除）"})
            return
        opener = (["explorer", WORKDIR] if sys.platform == "win32"
                  else ["xdg-open", WORKDIR] if sys.platform.startswith("linux")
                  else ["open", WORKDIR])
        try:
            subprocess.Popen(opener)
        except OSError as e:
            self._reply(400, {"error": f"打开失败: {e}"})
            return
        self._reply(200, {"ok": True})

    def _handle_settings_search(self, q: str):
        q = (q or "").strip()
        if not q:
            self._reply(400, {"error": "need q"})
            return
        self._reply(200, _search_runs(q))

    # ---------- scheduled goals (create + list + delete; firing is the
    # best-effort _schedule_loop, which only lives in the real _main()) ----------

    def _handle_schedules_list(self):
        if not SCHEDULES_PATH:
            self._reply(400, {"error": "no data directory configured"})
            return
        self._reply(200, {"schedules": _schedules_load()})

    def _handle_schedules_save(self, body: dict):
        if not SCHEDULES_PATH:
            self._reply(400, {"error": "no data directory configured"})
            return
        name = str(body.get("name") or "").strip()
        goal = str(body.get("goal") or "").strip()
        if not name or not goal:
            self._reply(400, {"error": "need name and goal"})
            return
        freq = body.get("freq")
        if freq not in ("daily", "weekly"):
            self._reply(400, {"error": "freq must be daily or weekly"})
            return
        try:
            hour, minute = int(body.get("hour")), int(body.get("minute"))
        except (TypeError, ValueError):
            self._reply(400, {"error": "hour and minute must be numbers"})
            return
        if not (0 <= hour <= 23):
            self._reply(400, {"error": "hour out of range"})
            return
        if not (0 <= minute <= 59):
            self._reply(400, {"error": "minute out of range"})
            return
        entry = {"id": uuid.uuid4().hex[:8], "name": name, "goal": goal,
                 "freq": freq, "hour": hour, "minute": minute,
                 "enabled": True, "last_fired_date": None, "last_fired_status": None}
        if freq == "weekly":
            try:
                weekday = int(body.get("weekday"))
            except (TypeError, ValueError):
                self._reply(400, {"error": "weekday must be a number"})
                return
            if not (0 <= weekday <= 6):
                self._reply(400, {"error": "weekday out of range"})
                return
            entry["weekday"] = weekday
        # under the same lock _schedule_tick uses for its own
        # read-modify-write, so this can't race the scheduler thread
        with _schedules_lock:
            schedules = _schedules_load()
            schedules.append(entry)
            _schedules_save(schedules)
        self._reply(200, {"ok": True, "schedules": schedules})

    def _handle_schedules_delete(self, body: dict):
        if not SCHEDULES_PATH:
            self._reply(400, {"error": "no data directory configured"})
            return
        sid = str(body.get("id") or "")
        with _schedules_lock:
            schedules = _schedules_load()
            kept = [s for s in schedules if s.get("id") != sid]
            if len(kept) == len(schedules):
                self._reply(404, {"error": "schedule not found"})
                return
            _schedules_save(kept)
        self._reply(200, {"ok": True, "schedules": kept})

    def _handle_file(self, task_id: str, rel: str):
        """Serve one file a task actually produced, for the frontend's
        preview panel. The owning run's tasks/{id}/files list (written by
        hub/engine.py from a workdir snapshot diff, see hub/artifacts.py)
        is the whitelist: anything not recorded there is a 404, so there
        is no path to traverse in the first place -- `rel` is only ever
        compared against recorded strings, never used to build a path
        until it has matched one. Older runs are scanned newest-first, so
        a finished run's deliverables survive later runs."""
        board = _board_with_task(task_id)
        files = board.get(f"tasks/{task_id}/files") or [] if board is not None else []
        if not any(f.get("path") == rel for f in files):
            self._reply(404, {"error": "not a recorded deliverable of this task"})
            return
        workdir = os.path.realpath(board.get("run/workdir") or os.getcwd())
        full = os.path.realpath(os.path.join(workdir, rel))
        # defense in depth: the whitelist above already settles it, but a
        # blackboard carried over from another machine/run shouldn't be able
        # to hand out anything outside the current workdir either
        if not full.startswith(workdir + os.sep) or not os.path.isfile(full):
            self._reply(404, {"error": "file no longer available"})
            return
        if os.path.getsize(full) > MAX_PREVIEW_BYTES:
            self._reply(413, {"error": "file too large to preview"})
            return
        with open(full, "rb") as f:
            data = f.read()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _handle_export(self, run_id: str):
        """GET /export?run=<id>: one zip with every file this run's tasks
        actually produced (grouped by task id) plus a templated SUMMARY.md.
        Pure read: no new events, no LLM call, no blackboard writes. Each
        file passes the exact same whitelist + realpath containment checks
        GET /file applies; the run id itself gets the containment check
        _resolve_run implements."""
        resolved = _resolve_run(run_id)
        if resolved is None:
            self._reply(404, {"error": "run not found"})
            return
        rid, board_path, events_path = resolved
        board = Blackboard(board_path, on_write=lambda e: None)
        events = _read_events_full(events_path)
        rows = _task_rows_for_export(board, events)
        workdir = os.path.realpath(board.get("run/workdir") or WORKDIR or os.getcwd())
        buf = io.BytesIO()
        included, skipped, total = [], [], 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for row in rows:
                for f in row["files"]:
                    rel = f.get("path") or ""
                    full = os.path.realpath(os.path.join(workdir, rel))
                    # same containment as _handle_file: whitelist entries
                    # only, realpath prefix, isfile -- missing files are
                    # skipped (deleted/moved deliverables are normal), not
                    # a fatal error
                    if not full.startswith(workdir + os.sep) or not os.path.isfile(full):
                        continue
                    size = os.path.getsize(full)
                    if total + size > MAX_EXPORT_BYTES:
                        skipped.append((row["id"], rel, size))
                        continue   # don't break: a later smaller file may fit
                    zf.write(full, arcname=f"deliverables/{row['id']}/{rel}")
                    included.append((row["id"], rel, size))
                    total += size
            summary_md = _render_export_summary(rid, board, events, rows,
                                                included, skipped)
            # must stay inside the with-block: the directory structure is
            # only finalized when ZipFile closes
            zf.writestr("SUMMARY.md", summary_md)
        # getbuffer() (a view) instead of getvalue() (a full copy): the
        # zip is already held once in `buf`, no reason to hold it twice
        # while it's written out. MAX_EXPORT_BYTES bounds the packed
        # ORIGINAL file sizes, not the zip's on-the-wire size, so this
        # still isn't a hard ceiling on peak memory for incompressible
        # deliverables -- just no longer double what it has to be.
        data = buf.getbuffer()
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition",
                         f'attachment; filename="agent-hub-export-{rid or "run"}.zip"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    # ---------- POST handlers ----------

    def _handle_run(self, body: dict):
        if _run_busy():
            self._reply(409, {"error": _busy_error()})
            return
        if body.get("goal"):
            tail = [f"--goal={body['goal']}"]
        elif body.get("plan"):
            tail = [f"--plan={body['plan']}"]
        else:
            self._reply(400, {"error": "need goal or plan"})
            return
        if body.get("mock"):
            tail += ["--mock"]
        tail += _gate_argv()
        try:
            # the busy check above is a fast early-exit for the common
            # case; _spawn_main_if_idle re-checks atomically under
            # _spawn_lock right before actually spawning, since the
            # scheduler thread can now also spawn on its own between here
            # and there (see _spawn_lock's docstring)
            pid = _spawn_main_if_idle(tail)
        except OSError as e:
            self._reply(502, {"error": f"failed to start engine: {e}"[:300]})
            return
        if pid is None:
            self._reply(409, {"error": _busy_error()})
            return
        self._reply(200, {"ok": True, "pid": pid})

    def _handle_rerun(self, body: dict):
        """Force-rerun any tasks/{id}/status=="stale" left by a deep-
        collaboration follow-up (see hub/conversation.py's
        mark_stale_dependents). No body needed -- always targets the
        current BOARD_PATH. `main.py --resume` with neither --goal nor
        --plan reconstructs the DAG from the board itself (see
        Plan.from_blackboard); everything not STALE restores unchanged,
        only STALE tasks re-execute."""
        if _run_busy():
            self._reply(409, {"error": _busy_error()})
            return
        if not os.path.exists(BOARD_PATH):
            self._reply(400, {"error": "no existing blackboard to resume from"})
            return
        try:
            pid = _spawn_main_if_idle(["--resume"] + _gate_argv())
        except OSError as e:
            self._reply(502, {"error": f"failed to start engine: {e}"[:300]})
            return
        if pid is None:
            self._reply(409, {"error": _busy_error()})
            return
        self._reply(200, {"ok": True, "pid": pid})

    def _handle_cancel(self, body: dict):
        """Kill the current run's WHOLE process group (main.py + every
        claude/dsh/codebuddy child it spawned), not just main.py's own
        pid -- see docs/superpowers/specs/2026-08-16-cancel-run-design.md
        §1/§2 for why a plain proc.terminate() would silently orphan
        still-billing child subprocesses. Marks cancel_flag BEFORE
        signaling so _watch_run (racing this in its own thread) always
        sees the request, however fast the process dies."""
        if not _run_busy():
            self._reply(409, {"error": "no run in progress"})
            return
        if _run_meta.get("cancel_flag") is not None:
            _run_meta["cancel_flag"]["cancel_requested"] = True
        if not _terminate_current_process_tree(_current) and _run_busy():
            self._reply(502, {"error": "failed to terminate the complete process tree"})
            return
        self._reply(200, {"ok": True})

    def _handle_gate(self, body: dict):
        """Persist one user decision for a gate that is currently open."""
        gate_id = str(body.get("gate_id") or "")
        if not _GATE_ID_RE.match(gate_id):
            self._reply(400, {"error": "invalid gate_id"})
            return
        decision = str(body.get("decision") or "")
        allowed = (("approve", "cancel") if gate_id == "plan"
                   else ("approve", "skip", "cancel"))
        if decision not in allowed:
            self._reply(400, {"error": f"decision must be one of {allowed}"})
            return
        overrides = body.get("overrides", {})
        if overrides is None:
            overrides = {}
        if not isinstance(overrides, dict):
            self._reply(400, {"error": "overrides must be an object"})
            return
        known_agents = _matrix_agent_names()
        for agent in overrides.values():
            if not isinstance(agent, str) or agent not in known_agents:
                self._reply(400, {"error": f"unknown agent '{agent}'"})
                return
        if not BOARD_PATH or not os.path.exists(BOARD_PATH):
            self._reply(404, {"error": "没有正在进行的运行"})
            return
        # Double-clicks and multiple tabs must not append competing replies.
        with _gate_reply_lock:
            board = Blackboard(BOARD_PATH, on_write=lambda event: None)
            opened = board.get(f"run/gate/{gate_id}")
            if opened is None:
                self._reply(409, {"error": f"权限门 {gate_id} 没有打开"})
                return
            expected_token = (str(opened.get("gate_token") or "")
                              if isinstance(opened, dict) else "")
            gate_token = str(body.get("gate_token") or "")
            if expected_token:
                if not _GATE_TOKEN_RE.match(gate_token):
                    self._reply(409, {"error": f"权限门 {gate_id} 的页面已过期"})
                    return
                if gate_token != expected_token:
                    self._reply(409, {"error": f"权限门 {gate_id} 的页面已过期"})
                    return
            elif gate_token:
                # Legacy tokenless gate: a token from a newer/stale card must
                # never be accepted against a different gate instance.
                self._reply(409, {"error": f"权限门 {gate_id} 的页面已过期"})
                return
            previous = board.get(f"run/gate_reply/{gate_id}")
            previous_token = (str(previous.get("gate_token") or "")
                              if isinstance(previous, dict) else "")
            if previous is not None and previous_token == expected_token:
                self._reply(409, {"error": f"权限门 {gate_id} 已经回答"})
                return
            reply = {"decision": decision, "overrides": overrides}
            if expected_token:
                reply["gate_token"] = expected_token
            board.put(f"run/gate_reply/{gate_id}", reply, author="user")
        self._reply(200, {"ok": True})

    def _handle_chat_session_current(self):
        try:
            self._reply(200, _current_manager_sessions())
        except (SessionDataError, OSError) as e:
            # 损坏就如实报错并**保留原文件** —— 自动重建等于把用户唯一的
            # 现场证据抹掉，而且下次还会以同样的方式失败
            self._reply(500, {"error": str(e)})

    def _handle_chat_session_discard(self, body: dict):
        if not isinstance(body, dict):
            self._reply(400, {"error": "request body must be a JSON object"})
            return
        try:
            sid = _validate_session_id(body.get("session_id", ""))
        except ValueError as exc:
            self._reply(400, {"error": str(exc)})
            return
        try:
            active = _active_session_id()
            if not active:
                self._reply(200, {"ok": True})   # 幂等：已经没有活跃会话
                return
            if active != sid:
                # 另一个标签页已经开了新会话，这个请求来自过期的页面
                self._reply(409, {"error": "another manager session is active",
                                  "session_id": active})
                return
            if sid in _chat_inflight:
                self._reply(409, {"error": "manager turn is still in progress"})
                return
            path = _session_path(sid)
            if not _chat_lock.acquire(blocking=False):
                self._reply(409, {"error": "manager turn is still in progress"})
                return
            locked_reply = None
            try:
                active = _active_session_id()
                if not active:
                    locked_reply = (200, {"ok": True})
                elif active != sid:
                    locked_reply = (409, {
                        "error": "another manager session is active",
                        "session_id": active,
                    })
                elif sid in _chat_inflight:
                    locked_reply = (409, {
                        "error": "manager turn is still in progress",
                    })
                elif path:
                    try:
                        os.unlink(path)
                    except FileNotFoundError:
                        pass
                if locked_reply is None:
                    _clear_active_session(sid)
                    _sessions.pop(sid, None)
                    _progress_reset(sid)
            finally:
                _chat_lock.release()
            if locked_reply is not None:
                self._reply(*locked_reply)
                return
            self._reply(200, {"ok": True})
        except (SessionDataError, OSError) as e:
            self._reply(500, {"error": str(e)})

    def _handle_chat(self, body: dict):
        if not isinstance(body, dict):
            self._reply(400, {"error": "request body must be a JSON object"})
            return
        message = str(body.get("message", "")).strip()
        if not message:
            self._reply(400, {"error": "need message"})
            return
        if _run_busy():
            self._reply(409, {"error": _busy_error()})
            return
        sid = str(body.get("session_id") or uuid.uuid4().hex[:12])
        try:
            sid = _validate_session_id(sid)
        except ValueError as exc:
            self._reply(400, {"error": str(exc)})
            return

        # Reserve the one project-wide manager turn under the short-lived
        # state lock. The slow Router/model work happens after release so GET
        # current can report pending and discard can fail fast instead of
        # waiting behind the model call.
        if not _chat_lock.acquire(blocking=False):
            self._reply(409, {"error": "another manager turn is still in progress"})
            return
        turn_conflict = False
        try:
            if _chat_inflight:
                turn_conflict = True
            else:
                _chat_inflight.add(sid)
        finally:
            _chat_lock.release()
        if turn_conflict:
            self._reply(409, {
                "error": "another manager turn is still in progress"})
            return
        try:
            self._handle_chat_locked(body, sid, message)
        except SessionConflict as exc:
            self._reply(409, {"error": str(exc), "session_id": sid})
        except (SessionDataError, OSError) as exc:
            self._reply(500, {"error": str(exc), "session_id": sid})
        finally:
            with _chat_lock:
                _chat_inflight.discard(sid)

    def _handle_chat_locked(self, body: dict, sid: str, message: str):
        """Run one logically reserved turn without holding `_chat_lock`."""
        sess, _created = _session_for_chat(sid)
        router = _session_router(sess)
        pinned_mgr = _apply_manager_pref(router)

        staged = _session_stage(sess)
        retrying_incomplete_turn = (
            staged["history"] and staged["history"][-1].get("role") == "user" and
            staged["history"][-1].get("text") == message)
        if not retrying_incomplete_turn:
            answers = _choice_answers(body.get("choice_answers"))
            if staged["history"] and \
                    staged["history"][-1].get("role") == "manager" and \
                    staged["history"][-1].get("questions") and \
                    not staged["history"][-1].get("answered"):
                staged["history"][-1]["answered"] = True
                staged["history"][-1]["answers"] = answers
            staged["history"].append({"role": "user", "text": message,
                                      "ts": round(time.time(), 3)})

            correcting = (staged["awaiting_confirmation"] and
                          not manager.is_confirmation(message))
            if correcting:
                staged["awaiting_confirmation"] = False
            _session_save(staged)
            _sessions[sid] = staged
            sess = staged

        # 每轮只展示这一轮的进度，不累积历史
        _progress_reset(sid)

        if sess["awaiting_confirmation"]:
            if manager.is_confirmation(message):
                p = sess["proposal"]
                try:
                    pid, run_id = _spawn_manager_main_if_idle([
                        f"--goal={p['goal']}",
                        f"--constraints={p['constraints']}",
                    ] + _gate_argv())
                except OSError as e:
                    self._reply(502, {"error": f"failed to start engine: {e}"[:300],
                                      "session_id": sid})
                    return
                if pid is None:
                    self._reply(409, {"error": _busy_error(),
                                      "session_id": sid})
                    return
                cost = sess["cost_usd"]
                warning = _archive_manager_session(sess, run_id)
                response = {"status": "started", "pid": pid,
                            "session_id": sid, "manager_cost_usd": cost}
                if warning:
                    response["persistence_warning"] = warning[:300]
                self._reply(200, response)
                return

        force = (manager.should_force_start(message)
                 or sess["questions"] >= manager.MANAGER_MAX_TURNS)
        try:
            step = asyncio.run(manager.manager_step(
                sess["history"], router, WORKDIR, force_finish=force,
                on_event=_progress_sink(sid)))
        except RuntimeError as e:
            error = str(e)
            if pinned_mgr:
                error = (f"你指定的经理 {pinned_mgr} 不可用（{error}）——"
                         f"换一个，或把经理改回『自动』")
            self._reply(502, {"error": error, "session_id": sid,
                              "pinned_manager": pinned_mgr})
            return
        staged = _session_stage(sess)
        staged["cost_usd"] += step.get("_cost_usd", 0.0)

        if step["status"] == "asking" and not force:
            staged["questions"] += 1
            questions = step.get("questions") or []
            staged["history"].append({
                "role": "manager", "text": step["question"],
                "ts": round(time.time(), 3), "questions": questions,
                "answered": False, "answers": {},
            })
            _session_save(staged)
            _sessions[sid] = staged
            self._reply(200, {"status": "asking", "question": step["question"],
                              "questions": questions,
                              "session_id": sid,
                              "manager_cost_usd": staged["cost_usd"]})
            return
        if step["status"] == "asking":   # forced but still asking: mirror CLI behavior
            self._reply(502, {"error": "manager kept asking after being told to finalize",
                              "session_id": sid})
            return
        staged["proposal"] = {"goal": step["goal"],
                              "constraints": step.get("constraints", "")}
        staged["awaiting_confirmation"] = True
        proposal_text = ("确认一下——\n目标：" + staged["proposal"]["goal"] +
                         "\n约束：" +
                         (staged["proposal"]["constraints"] or "（无）") +
                         "\n没问题回复“对/开始”；要改的话直接说哪里不对。")
        staged["history"].append({"role": "manager", "text": proposal_text,
                                  "ts": round(time.time(), 3)})
        _session_save(staged)
        _sessions[sid] = staged
        self._reply(200, {"status": "confirm", "session_id": sid,
                          "manager_cost_usd": staged["cost_usd"],
                          **staged["proposal"]})

    def _handle_task_message(self, task_id: str, body: dict):
        if not task_id or "/" in task_id:
            self._reply(400, {"error": "invalid task id"})
            return
        message = str(body.get("message", "")).strip()
        finish = bool(body.get("finish"))
        if not message and not finish:
            self._reply(400, {"error": "need message"})
            return
        # Serialize concurrent follow-ups to the SAME task_id (see
        # _lock_for_task's docstring) -- a queued second request just waits
        # its turn rather than racing the blackboard, no new error shape
        # for the frontend to special-case.
        with _lock_for_task(task_id):
            board = _board_with_task(task_id)
            if board is None or board.get(f"tasks/{task_id}/spec") is None:
                self._reply(404, {"error": f"task '{task_id}' not found"})
                return

            if board.get(f"tasks/{task_id}/status") == "done":
                # Terminal -- the Engine coroutine that owned this task's
                # session has already returned and will never look at the
                # inbox again, so it's safe to resume it directly here and
                # answer synchronously (unchanged from the original MVP).
                router = Router(MATRIX, workdir=WORKDIR)
                try:
                    result, compacted = asyncio.run(
                        conversation.continue_task_conversation(
                            task_id, message, router, board))
                except conversation.ConversationError as e:
                    self._reply(404, {"error": str(e)})
                    return
                if not result.ok:
                    self._reply(502, {"error": result.error or "agent call failed",
                                      "cost_usd": round(result.cost_usd, 4)})
                    return
                # this reply just became tasks/{id}/output's new version --
                # any already-DONE task that reads it downstream may now be
                # holding a stale answer. Detection+notification only (see
                # mark_stale_dependents' docstring); no auto-rerun.
                stale = conversation.mark_stale_dependents(board, task_id)
                if stale:
                    bus = EventBus(EVENTS_PATH)
                    for dep_id in stale:
                        bus.emit("task_stale", task_id=dep_id, cause_task_id=task_id)
                    bus.close()
                self._reply(200, {"reply": result.output,
                                  "cost_usd": round(result.cost_usd, 4),
                                  "compacted": compacted,
                                  "stale_downstream": stale})
                return

            # Not done yet: a live Engine process may still own this task's
            # claude_code session (mid-turn, or already polling its inbox in
            # an AWAITING_INPUT loop -- see hub/engine.py's
            # _await_input_loop). Calling `claude -p --resume` from THIS
            # process too would race it across processes -- no in-process
            # lock can prevent that. So just queue and return; the eventual
            # reply surfaces later as a task_message_replied event for the
            # frontend's existing event-polling to pick up, not as this
            # HTTP response.
            if message:
                next_idx = len(board.query(f"tasks/{task_id}/inbox/"))
                board.put(f"tasks/{task_id}/inbox/{next_idx}", message,
                         author="user", task_id=task_id, kind="message")
            if finish:
                board.put(f"tasks/{task_id}/inbox_finish", True,
                         author="user", task_id=task_id, kind="fact")
            self._reply(200, {"queued": True})

    def _handle_shutdown(self, body: dict):
        # Two traps (brief 1.6): never call srv.shutdown() from the request
        # thread that serve_forever() is waiting on (deadlock), and never
        # wait unbounded on a run that may be parked in AWAITING_INPUT.
        if _run_busy():
            self._reply(409, {"error": "a run is in progress -- finish or terminate it first"})
            return
        self._reply(200, {"ok": True})
        if SERVER is not None:
            threading.Thread(target=SERVER.shutdown, daemon=True).start()

    def _handle_preflight(self, body: dict):
        """One-time REAL probe of every configured agent (a few tokens each)
        -- the desktop version of CLAUDE.md §4's lesson: structural
        unavailability (wrong model for the account) looks fine to an
        existence probe but fails every task that reaches it."""
        if _run_busy():
            self._reply(409, {"error": _busy_error()})
            return
        try:
            results = asyncio.run(_real_preflight())
        except Exception as e:
            self._reply(502, {"error": f"preflight failed: {e}"[:500]})
            return
        if PREFLIGHT_PATH:
            _atomic_write(PREFLIGHT_PATH,
                          json.dumps({"ts": round(time.time(), 3),
                                      "results": results}, ensure_ascii=False))
        self._reply(200, {"results": results})

    def _handle_settings_config(self, body: dict):
        """Both fields are validated BEFORE anything is persisted: whatever
        lands in config.yaml is deep-merged into the capability matrix that
        the engine subprocess consumes via --matrix, so a structurally
        wrong value here doesn't just break this page, it destroys the
        matrix for every future run -- and it survives restarts."""
        if not CONFIG_PATH:
            self._reply(400, {"error": "no data directory configured"})
            return
        cfg = _load_config()
        if "budget" in body:
            b = body["budget"]
            # `isinstance(True, int)` is True in Python -- without the
            # explicit bool reject, {"budget": true} silently becomes $1.00
            if isinstance(b, bool) or not isinstance(b, (int, float)):
                self._reply(400, {"error": "budget must be a number"})
                return
            b = float(b)
            if b != b or b in (float("inf"), float("-inf")) or b < 0:
                self._reply(400, {"error": "budget must be a finite, non-negative number"})
                return
            cfg.setdefault("budget", {})["max_usd_per_run"] = b
        if "matrix_patch" in body:
            mp = body["matrix_patch"]
            if mp is None:
                mp = {}
            if not isinstance(mp, dict):
                # _deep_merge REPLACES wholesale on a non-dict, so a scalar
                # here would overwrite the entire matrix with that scalar
                self._reply(400, {"error": "matrix_patch must be an object"})
                return
            cfg["matrix_patch"] = mp
        if "ui_prefs" in body:
            prefs = body["ui_prefs"]
            if not isinstance(prefs, dict):
                self._reply(400, {"error": "ui_prefs must be an object"})
                return
            old_prefs = _load_config().get("ui_prefs")
            merged = dict(old_prefs if isinstance(old_prefs, dict) else {})
            for key, value in prefs.items():
                if key not in UI_PREF_DEFAULTS:
                    self._reply(400, {"error": f"unknown ui_prefs key '{key}'"})
                    return
                if not isinstance(value, str):
                    self._reply(400, {"error": f"ui_prefs.{key} must be a string"})
                    return
                if key == "assign_mode" and value not in _ASSIGN_MODES:
                    self._reply(400, {"error": f"assign_mode must be one of {_ASSIGN_MODES}"})
                    return
                if key == "gate_mode" and value not in _GATE_MODES:
                    self._reply(400, {"error": f"gate_mode must be one of {_GATE_MODES}"})
                    return
                if key == "manager_agent" and value and value not in _matrix_agent_names():
                    self._reply(400, {"error": f"unknown agent '{value}'"})
                    return
                merged[key] = value
            cfg["ui_prefs"] = merged
        _save_config(cfg)
        _write_merged_matrix()
        budget = float((cfg.get("budget") or {}).get("max_usd_per_run", 2.0))
        self._reply(200, {"ok": True, "budget": budget})

    def _handle_settings_credentials(self, body: dict):
        if not CREDENTIALS_PATH:
            self._reply(400, {"error": "no data directory configured"})
            return
        updates = {}
        if "deepseek" in body:
            updates["DEEPSEEK_API_KEY"] = str(body["deepseek"] or "")
        if "openrouter" in body:
            updates["OPENROUTER_API_KEY"] = str(body["openrouter"] or "")
        _credentials_write(updates)
        creds = _credentials_read()
        self._reply(200, {"ok": True,
                          "deepseek": bool(os.environ.get("DEEPSEEK_API_KEY")
                                           or creds.get("DEEPSEEK_API_KEY")),
                          "openrouter": bool(os.environ.get("OPENROUTER_API_KEY")
                                             or creds.get("OPENROUTER_API_KEY"))})

    def _reply(self, code: int, obj: dict):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):  # quieter
        pass


# ---------- module-level helpers used by the handlers ----------

def _resolve_deliverable(task_id: str, rel: str) -> tuple[str, str] | None:
    """The whitelist + realpath containment that GET /file applies, as a
    reusable pure function: returns (workdir, full_path) only when rel is
    a RECORDED deliverable of this task and resolves inside workdir.
    _handle_file itself stays untouched (protected pattern)."""
    board = _board_with_task(task_id)
    files = board.get(f"tasks/{task_id}/files") or [] if board is not None else []
    if not any(f.get("path") == rel for f in files):
        return None
    workdir = os.path.realpath(board.get("run/workdir") or os.getcwd())
    full = os.path.realpath(os.path.join(workdir, rel))
    if not full.startswith(workdir + os.sep) or not os.path.isfile(full):
        return None
    return workdir, full


_GATE_ID_RE = re.compile(r"^(plan|task:[A-Za-z0-9_.-]{1,64})$")
_GATE_TOKEN_RE = re.compile(r"^[a-f0-9]{16}$")


def _open_gate_id() -> str:
    """Return the current run's first unanswered gate id, or ``""``."""
    if not BOARD_PATH or not os.path.exists(BOARD_PATH):
        return ""
    try:
        board = Blackboard(BOARD_PATH, on_write=lambda event: None)
    except OSError:
        return ""
    for entry in board.query("run/gate/"):
        gate_id = entry["key"][len("run/gate/"):]
        opened = entry.get("value")
        open_token = (str(opened.get("gate_token") or "")
                      if isinstance(opened, dict) else "")
        reply = board.get(f"run/gate_reply/{gate_id}")
        reply_token = (str(reply.get("gate_token") or "")
                       if isinstance(reply, dict) else "")
        if reply is None or reply_token != open_token:
            return gate_id
    return ""


def _busy_error() -> str:
    gate_id = _open_gate_id()
    if gate_id:
        return (f"当前运行正卡在权限确认上（{gate_id}）—— 先在对话里放行或取消它，"
                f"再开新的运行")
    return "a run is already in progress -- finish or cancel it first"


def _board_with_task(task_id: str):
    """Find the run whose blackboard knows this task: current run first,
    then older runs newest-first. Lets /file previews and follow-ups
    survive later runs (each run owns its own blackboard now)."""
    board = Blackboard(BOARD_PATH, on_write=lambda e: None)
    if board.get(f"tasks/{task_id}/spec") is not None or \
            board.get(f"tasks/{task_id}/files") is not None:
        return board
    if RUNS_DIR:
        for entry in reversed(_runs):
            p = os.path.join(RUNS_DIR, entry.get("run_id", ""), "blackboard.jsonl")
            if p == BOARD_PATH or not os.path.exists(p):
                continue
            b = Blackboard(p, on_write=lambda e: None)
            if b.get(f"tasks/{task_id}/spec") is not None or \
                    b.get(f"tasks/{task_id}/files") is not None:
                return b
    return None


def _probe_agents() -> list[dict]:
    """Existence probe only: which local agent CLIs are installed. Zero
    subprocesses, zero LLM calls (the real "does this model work for this
    account" question is what POST /settings/preflight answers)."""
    out = []
    # 跟 executors 走同一个解析：否则 GUI 启动时运行是好的，设置页却
    # 报「未安装」—— 两个地方对「装没装」给出矛盾答案最难排查
    claude = _cli_which("claude")
    out.append({"id": "claude_code", "label": "Claude Code", "kind": "claude_code",
                "available": bool(claude),
                "detail": ("claude -p（订阅额度）" if claude else
                           "未检测到 claude 命令 —— 安装并登录 Claude Code 后点“重新探测”")})
    dsh = _dsh_cmd(None)
    out.append({"id": "dsh", "label": "DeepSeek Harness (dsh)", "kind": "dsh",
                "available": bool(dsh),
                "detail": (dsh[0] if dsh else
                           "未检测到 dsh —— npm i -g @deepseek-ai/dsh")})
    node, script = _resolve_codebuddy_paths(None, None)
    cb_ok = bool(node and script)
    out.append({"id": "codebuddy", "label": "WorkBuddy (codebuddy)", "kind": "codebuddy",
                "available": cb_ok,
                "detail": ("WorkBuddy 内置 CodeBuddy CLI（积分/免费额度）" if cb_ok else
                           "未找到 WorkBuddy —— 安装后重新探测（或设 "
                           "WORKBUDDY_NODE_PATH / WORKBUDDY_SCRIPT_PATH）")})
    codex_cmd = _codex_cmd(None)
    out.append({"id": "codex", "label": "Codex CLI", "kind": "codex",
                "available": bool(codex_cmd),
                "detail": ("codex exec（复用 ChatGPT 登录；workspace-write 沙箱）"
                           if codex_cmd else "未检测到 codex —— npm i -g @openai/codex")})
    return out


async def _real_preflight() -> dict:
    router = Router(MATRIX, workdir=WORKDIR)
    probe = Task(id="_preflight", title="preflight", type=TaskType.LIGHT,
                 prompt="Reply with exactly: ok")
    names = (router.claude_code_agents() + router.codex_agents()
             + router.codebuddy_agents()
             + router.dsh_agents())
    out = {}
    for name in names:
        executor = router.executor_for(name)
        runner = (getattr(executor, "run_preflight", executor.run)
                  if router.kind(name) == "codex" else executor.run)
        result = await runner(probe, "")
        out[name] = {"ok": result.ok, "error": (result.error or "")[:300],
                     "cost_usd": round(result.cost_usd, 4),
                     "cost_credits": round(result.cost_credits, 4),
                     "usage": result.usage}
    return out


def _main() -> None:
    global SERVER
    ap = argparse.ArgumentParser(description="Agent Hub local server")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--data-dir", default=None,
                    help="user data directory (default: platform app-data dir)")
    ap.add_argument("--project", default=None,
                    help="project name, isolates runs per project (default: default)")
    args = ap.parse_args()
    _configure(args.data_dir, args.project, args.port)
    # best-effort scheduler lives only in the real entry point -- NOT in
    # _configure(), which tests call directly and repeatedly; a stray real
    # timer thread there would leak across every test in the process
    threading.Thread(target=_schedule_loop, daemon=True).start()
    print(f"Agent Hub -> http://localhost:{PORT}/dashboard.html")
    print(f"data: {RUNTIME_DIR}")
    SERVER = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    SERVER.serve_forever()


if __name__ == "__main__":
    _main()
