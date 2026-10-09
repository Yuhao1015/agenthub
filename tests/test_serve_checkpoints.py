"""serve.py and the change safety net (docs/decisions/change-safety-net.md §3.2,
§4, §5.3): checkpoint arguments for the engine, reruns in the run's own work
folder, retention, taking turns with the engine, the watchdog's closing
snapshot, the review / diff / rollback endpoints and writing follow-ups.

Real git in temp dirs (skipped honestly without git), fake engine processes
and a fake continue_task_conversation. No framework, zero LLM calls.

Run: python tests/test_serve_checkpoints.py
"""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
from test_gate_serve import authorize_local_agents
from hub import checkpoints, run_changes
from hub.blackboard import Blackboard
from hub.executors import ExecResult

RID = "abcd1234"
_GLOBALS = ("PORT", "MATRIX", "BOARD_PATH", "EVENTS_PATH", "WORKDIR", "DATA_DIR",
            "PROJECT", "RUNTIME_DIR", "RUNS_DIR", "INDEX_PATH", "CONFIG_PATH",
            "MERGED_MATRIX_PATH", "PREFLIGHT_PATH", "CREDENTIALS_PATH",
            "SCHEDULES_PATH", "SESSIONS_DIR", "ACTIVE_SESSION_PATH", "_current",
            "CHECKPOINT_KEEP_RUNS")


def needs_git(fn):
    def wrapper():
        if shutil.which("git") is None:
            print("  (skipped: no git on PATH)")
            return
        fn()
    return wrapper


def _rmtree(path):
    """git marks its object files read-only, and Windows refuses to delete those."""
    for root, _dirs, files in os.walk(path):
        for name in files:
            os.chmod(os.path.join(root, name), stat.S_IREAD | stat.S_IWRITE)
    shutil.rmtree(path)


def _write(root, rel, data: bytes):
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(data)


def _read(root, rel) -> bytes:
    with open(os.path.join(root, rel), "rb") as f:
        return f.read()


def _events(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path)
    r = conn.getresponse()
    body = r.read()
    conn.close()
    return r.status, (json.loads(body) if body else {})


def _post(port, path, obj):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", path, json.dumps(obj), {"Content-Type": "application/json"})
    r = conn.getresponse()
    body = r.read()
    conn.close()
    return r.status, (json.loads(body) if body else {})


def test_post_rejects_non_object_json():
    env = _Env(http=True)
    original_popen = serve.subprocess.Popen
    original_continue = serve.conversation.continue_task_conversation
    async def no_continue(*_args, **_kwargs):
        raise AssertionError("a malformed request must not call an agent")
    serve.subprocess.Popen = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("a malformed request must not launch a process"))
    serve.conversation.continue_task_conversation = no_continue
    try:
        for value in ([], None, "x", 5):
            code, data = _post(env.port, "/run", value)
            assert code == 400 and "error" in data, (value, code, data)
    finally:
        serve.subprocess.Popen = original_popen
        serve.conversation.continue_task_conversation = original_continue
        env.close()


class _Alive:
    """A stand-in engine process that is still running."""

    def poll(self):
        return None


class _Exited:
    """A stand-in engine process that already exited with `rc`."""

    def __init__(self, rc):
        self.returncode = rc

    def wait(self):
        return self.returncode


class _NeverEnds:
    """A fake engine for _spawn_main: its watchdog thread just waits forever."""
    pid, returncode = 1, None

    def poll(self):
        return None

    def wait(self):
        threading.Event().wait()


class _Env:
    """A desktop-mode serve.py in a temp data dir; close() restores every global."""

    def __init__(self, http=False):
        self.saved = {"_runs": list(serve._runs), "_run_meta": dict(serve._run_meta),
                      **{k: getattr(serve, k) for k in _GLOBALS}}
        self.tmp = tempfile.mkdtemp(prefix="ah_srvck_")
        self.srv, self.port = None, 8787
        if http:
            self.srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
            self.port = self.srv.server_address[1]
            threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        serve._configure(self.tmp, "default", self.port)
        authorize_local_agents()
        serve._current = None

    def run(self, rid=RID, workdir=None):
        """Register a run (it becomes the current one) and return its board."""
        serve._runs_add(rid, "goal")
        serve._adopt_latest_run()
        run_dir = os.path.join(serve.RUNS_DIR, rid)
        os.makedirs(run_dir, exist_ok=True)
        board = Blackboard(os.path.join(run_dir, "blackboard.jsonl"), on_write=None)
        board.put("run/workdir", os.path.realpath(workdir or serve.WORKDIR), author="engine")
        return board

    def segment(self, board, change=None, close=True, rid=RID):
        wd = board.get("run/workdir")
        seg, _ = run_changes.begin(board, checkpoints.store_path(serve.RUNTIME_DIR, wd), wd,
                                   rid, "run", "engine")
        if change:
            change(wd)
        if close:
            run_changes.end(board, seg, author="engine")
        return seg

    def close(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
        for k, v in self.saved.items():
            if k == "_runs":
                serve._runs = list(v)
            elif k == "_run_meta":
                serve._run_meta = dict(v)
            else:
                setattr(serve, k, v)
        _rmtree(self.tmp)


@needs_git
def test_env_close_removes_shadow_store_and_closes_server():
    env = _Env(http=True)
    board = env.run()
    env.segment(board, change=lambda wd: _write(wd, "a.txt", b"x"))
    temp, server = env.tmp, env.srv
    env.close()
    assert not os.path.exists(temp), temp
    assert server.socket.fileno() == -1


def _spawn_capturing(argv_tail):
    """serve._spawn_main with the engine process faked; returns the engine argv."""
    captured = {}
    real_popen = subprocess.Popen

    def fake_popen(argv, **kw):
        if any(str(a).endswith("main.py") for a in argv):
            captured["argv"] = argv
            return _NeverEnds()
        return real_popen(argv, **kw)
    serve.subprocess.Popen = fake_popen
    try:
        serve._spawn_main(argv_tail)
    finally:
        serve.subprocess.Popen = real_popen
    return captured["argv"]


def test_spawn_passes_checkpoint_args():
    """Review Focus 3 too: the data dir is always excluded from checkpoints."""
    env = _Env()
    try:
        argv = _spawn_capturing(["--goal=hello"])
        rid = serve._run_meta["id"]
        store = checkpoints.store_path(serve.RUNTIME_DIR, serve.WORKDIR)
        assert f"--workdir={serve.WORKDIR}" in argv, argv
        assert f"--checkpoint-store={store}" in argv, argv
        assert f"--run-id={rid}" in argv, argv
        assert f"--checkpoint-exclude={serve.DATA_DIR}" in argv, argv
    finally:
        env.close()


def test_rerun_uses_the_runs_own_workdir():
    env = _Env()
    try:
        other = os.path.join(env.tmp, "elsewhere")
        os.makedirs(other)
        env.run(workdir=other)
        argv = _spawn_capturing(["--resume"])
        real = os.path.realpath(other)
        assert f"--workdir={real}" in argv, argv
        assert f"--checkpoint-store={checkpoints.store_path(serve.RUNTIME_DIR, real)}" in argv
        assert f"--run-id={RID}" in argv, argv
    finally:
        env.close()


@needs_git
def test_retention_keeps_only_the_latest_runs():
    env = _Env()
    try:
        wd = serve.WORKDIR
        store = checkpoints.store_path(serve.RUNTIME_DIR, wd)
        for rid in ("aaaa0001", "aaaa0002", "aaaa0003"):
            serve._runs_add(rid, "goal")
            checkpoints.snapshot(store, wd, checkpoints.ref(rid, "seg1-before"))
        serve.CHECKPOINT_KEEP_RUNS = 2
        serve._prune_checkpoints()
        refs = subprocess.run(["git", "--git-dir", store, "for-each-ref",
                               "--format=%(refname)"],
                              capture_output=True, check=True).stdout.decode().split()
        assert sorted(r.split("/")[3] for r in refs) == ["aaaa0002", "aaaa0003"], refs
    finally:
        env.close()


def test_no_engine_starts_while_serve_is_checkpointing():
    env = _Env(http=True)
    calls = []
    real_popen = subprocess.Popen
    try:
        # The current run is over (POST /cancel) but left its plan gate unanswered, and
        # nobody ever will: the reason a start is refused now is the checkpoint work
        # (the watchdog's closing snapshot), not the gate of a run that no longer exists.
        board = env.run()
        board.put("run/gate/plan", {"kind": "plan"}, author="engine")
        assert serve._open_gate_id() == "plan"
        serve._checkpoint_lock.acquire()
        serve.subprocess.Popen = lambda argv, **kw: calls.append(argv)
        try:
            assert serve._spawn_main_if_idle(["--goal=x"]) is None
            assert serve._spawn_manager_main_if_idle(["--goal=x"]) == (None, "")
            for path, body in (("/run", {"goal": "x"}), ("/rerun", {})):
                code, data = _post(env.port, path, body)
                assert code == 409, (path, code, data)
                error = data["error"]
                assert "正在记录文件改动" in error and "追问或回滚" in error, (path, error)
                assert "权限确认" not in error and "放行或取消" not in error, (path, error)
                # a follow-up can hold the lock for minutes: never promise "a few seconds"
                assert "等它结束" in error and "几秒" not in error, (path, error)
            assert calls == [], "the engine must wait its turn"
            assert "追问或回滚" in serve._busy_error()
        finally:
            serve.subprocess.Popen = real_popen
            serve._checkpoint_lock.release()
    finally:
        env.close()


@needs_git
def test_watchdog_closes_a_cancelled_runs_segment_first():
    env = _Env()
    try:
        board = env.run()
        env.segment(board, change=lambda wd: _write(wd, "half.txt", b"x"), close=False)
        events = serve._run_meta["events"]
        open(events, "w").close()
        serve._watch_run(RID, _Exited(1), events, {"cancel_requested": True})
        seg = Blackboard(board.path, on_write=None).get("run/checkpoints/seg/1")
        assert seg["after"] and seg["note"] == "", seg
        last = _events(events)[-1]
        assert last["type"] == "run_cancelled" and seg["ended"] <= last["ts"], (seg, last)
        serve._runs_load()
        entry = [e for e in serve._runs if e["run_id"] == RID][0]
        assert entry["status"] == "cancelled" and entry["changes"] == 1, entry
    finally:
        env.close()


@needs_git
def test_watchdog_closes_a_crashed_runs_segment():
    env = _Env()
    try:
        board = env.run()
        env.segment(board, change=lambda wd: _write(wd, "half.txt", b"x"), close=False)
        events = serve._run_meta["events"]
        open(events, "w").close()
        serve._watch_run(RID, _Exited(1), events, {"cancel_requested": False})
        seg = Blackboard(board.path, on_write=None).get("run/checkpoints/seg/1")
        assert seg["after"], seg
        assert _events(events)[-1]["type"] == "run_crashed"
    finally:
        env.close()


def test_rerun_survives_a_board_with_a_torn_last_line():
    """POST /rerun must never drop the connection (CLAUDE.md §6.8): a board that
    cannot be read falls back to the current WORKDIR, the pre-feature behavior."""
    env = _Env()
    try:
        other = os.path.join(env.tmp, "elsewhere")
        os.makedirs(other)
        board = env.run(workdir=other)
        with open(board.path, "ab") as f:      # a kill left the last line cut inside a character
            f.write('{"key": "tasks/x/output", "value": "中'.encode("utf-8")[:-1])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            argv = _spawn_capturing(["--resume"])
        store = checkpoints.store_path(serve.RUNTIME_DIR, serve.WORKDIR)
        assert f"--workdir={serve.WORKDIR}" in argv, argv
        assert f"--workdir={os.path.realpath(other)}" not in argv, argv
        assert f"--checkpoint-store={store}" in argv, argv
        assert f"--run-id={RID}" in argv, argv
        lines = err.getvalue().strip().splitlines()
        assert len(lines) == 1 and "run/workdir" in lines[0], lines
    finally:
        env.close()


@needs_git
def test_close_run_checkpoints_never_raises_when_the_index_write_fails():
    env = _Env()
    real = serve._runs_update
    try:
        board = env.run()
        env.segment(board, change=lambda wd: _write(wd, "half.txt", b"x"), close=False)

        def broken(run_id, **fields):
            raise RuntimeError("index write blew up")
        serve._runs_update = broken
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            assert serve._close_run_checkpoints(RID) is None
        lines = err.getvalue().strip().splitlines()
        assert len(lines) == 1 and RID in lines[0], lines
        seg = Blackboard(board.path, on_write=None).get("run/checkpoints/seg/1")
        assert seg["after"] and seg["ended"], seg     # the segment was closed before the write failed
    finally:
        serve._runs_update = real
        env.close()


@needs_git
def test_watchdog_records_the_end_even_when_the_changes_write_fails():
    real = serve._runs_update

    def index_locked(run_id, **fields):
        if "changes" in fields:                 # only the change count cannot be written
            raise OSError("index.jsonl is locked")
        return real(run_id, **fields)

    for cancelled, event, status in ((True, "run_cancelled", "cancelled"),
                                     (False, "run_crashed", "crashed")):
        env = _Env()
        try:
            board = env.run()
            env.segment(board, change=lambda wd: _write(wd, "half.txt", b"x"), close=False)
            events = serve._run_meta["events"]
            open(events, "w").close()
            serve._runs_update = index_locked
            with contextlib.redirect_stderr(io.StringIO()):
                serve._watch_run(RID, _Exited(1), events, {"cancel_requested": cancelled})
            assert _events(events)[-1]["type"] == event, _events(events)
            seg = Blackboard(board.path, on_write=None).get("run/checkpoints/seg/1")
            assert seg["after"], seg
            serve._runs_load()
            entry = [e for e in serve._runs if e["run_id"] == RID][0]
            assert entry["status"] == status, entry
        finally:
            serve._runs_update = real
            env.close()


@needs_git
def test_retention_carries_on_when_one_store_fails():
    """A store that cannot be pruned must not stop a new run, nor the stores after it."""
    env = _Env()
    real_prune = checkpoints.prune

    def run_ids(store):
        out = subprocess.run(["git", "--git-dir", store, "for-each-ref",
                              "--format=%(refname)"],
                             capture_output=True, check=True).stdout.decode().split()
        return sorted({r.split("/")[3] for r in out})
    try:
        other = os.path.join(env.tmp, "second")
        os.makedirs(other)
        workdirs = [serve.WORKDIR, other]
        stores = [checkpoints.store_path(serve.RUNTIME_DIR, wd) for wd in workdirs]
        for rid in ("aaaa0001", "aaaa0002", "aaaa0003"):
            serve._runs_add(rid, "goal")
            for store, wd in zip(stores, workdirs):
                checkpoints.snapshot(store, wd, checkpoints.ref(rid, "seg1-before"))
        serve.CHECKPOINT_KEEP_RUNS = 2
        # the store pruned FIRST is the one that breaks: the next one must still be pruned
        broken, healthy = sorted(stores, key=os.path.basename)

        def prune(store, keep):
            if os.path.basename(store) == os.path.basename(broken):
                raise OSError("the disk went away")
            return real_prune(store, keep)
        checkpoints.prune = prune
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            serve._prune_checkpoints()
        lines = err.getvalue().strip().splitlines()
        assert len(lines) == 1 and os.path.basename(broken) in lines[0], lines
        assert run_ids(broken) == ["aaaa0001", "aaaa0002", "aaaa0003"], run_ids(broken)
        assert run_ids(healthy) == ["aaaa0002", "aaaa0003"], run_ids(healthy)
    finally:
        checkpoints.prune = real_prune
        env.close()


def test_retention_survives_an_unreadable_checkpoints_folder():
    """If the checkpoints folder itself cannot be listed (permissions, just deleted),
    retention is skipped and a new run still spawns."""
    env = _Env()
    real_listdir = serve.os.listdir     # serve.os is the shared os module: delegate for every other path
    try:
        root = os.path.join(serve.RUNTIME_DIR, "checkpoints")
        os.makedirs(root)

        def listdir(path="."):
            if os.path.normcase(os.fspath(path)) == os.path.normcase(root):
                raise PermissionError(13, "Permission denied", root)
            return real_listdir(path)
        serve.os.listdir = listdir
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            assert serve._prune_checkpoints() is None
        lines = err.getvalue().strip().splitlines()
        assert len(lines) == 1 and root in lines[0], lines
    finally:
        serve.os.listdir = real_listdir
        env.close()


def test_no_quit_or_project_switch_while_serve_is_checkpointing():
    """The watchdog does git work between the engine exiting and recording how the run
    ended: quitting or switching project in that gap would leave the run "running" with
    no end event (CLAUDE.md §6.15). No run is alive here, only the checkpoint lock is held."""
    env = _Env(http=True)
    real_server = serve.SERVER

    class _Server:                      # stands in for serve.SERVER: counts, stops nothing
        calls = 0

        def shutdown(self):
            self.calls += 1
    try:
        serve.SERVER = _Server()
        serve._checkpoint_lock.acquire()
        try:
            got = {path: _post(env.port, path, body) for path, body in (
                ("/shutdown", {}), ("/settings/switch-project", {"project": "other"}))}
        finally:
            serve._checkpoint_lock.release()
        for path, (code, data) in got.items():
            assert code == 409 and "正在记录文件改动" in data.get("error", ""), got
            # settings.html shows it as "退出失败：…" / "切换失败：…": no talk of starting a run
            assert "开始新的运行" not in data["error"], got
        assert not os.path.exists(os.path.join(serve.DATA_DIR, "last_project.json")), got
        assert serve.SERVER.calls == 0, "the server must not shut down"
    finally:
        serve.SERVER = real_server
        env.close()


@needs_git
def test_run_changes_endpoint():
    env = _Env(http=True)
    try:
        board = env.run()
        env.segment(board, change=lambda wd: _write(wd, "a.txt", b"x\n"))
        code, data = _get(env.port, f"/run/changes?run={RID}")
        assert code == 200 and data["run"] == RID and data["changed"] == 1, data
        assert data["segments"][0]["files"][0]["path"] == "a.txt"
        code, cur = _get(env.port, "/run/changes")
        assert code == 200 and cur["run"] == RID, "no run = the current run"
        assert _get(env.port, "/run/changes?run=ffff0000")[0] == 404
        assert _get(env.port, "/run/changes?run=..%2F..")[0] == 404
    finally:
        env.close()


@needs_git
def test_run_changes_closes_a_dangling_segment_only_when_idle():
    env = _Env(http=True)
    try:
        board = env.run()
        env.segment(board, change=lambda wd: _write(wd, "a.txt", b"x"), close=False)
        serve._current = _Alive()
        code, data = _get(env.port, f"/run/changes?run={RID}")
        assert code == 200 and data["segments"][0]["open"] is True, \
            "never snapshot while an engine may be running"
        serve._current = None
        code, data = _get(env.port, f"/run/changes?run={RID}")
        seg = data["segments"][0]
        assert seg["open"] is False and run_changes.LATE_NOTE in seg["note"], seg
        assert data["changed"] == 1
    finally:
        env.close()


@needs_git
def test_segment_diff_endpoint_is_whitelisted():
    env = _Env(http=True)
    try:
        board = env.run()
        _write(board.get("run/workdir"), "a.txt", b"one\n")
        env.segment(board, change=lambda wd: _write(wd, "a.txt", b"two\n"))
        code, data = _get(env.port, f"/run/changes/diff?run={RID}&seg=1&path=a.txt")
        assert code == 200 and data["ok"] is True and "+two" in data["diff"], data
        # %C2%B2 is "²": str.isdigit() says yes, int() raises -- must be a 404, not a dropped connection
        for q in ("seg=1&path=b.txt", "seg=x&path=a.txt", "seg=2&path=a.txt", "seg=%C2%B2&path=a.txt",
                  "seg=" + "9" * 5000 + "&path=a.txt"):
            assert _get(env.port, f"/run/changes/diff?run={RID}&{q}")[0] == 404, q
    finally:
        env.close()


@needs_git
def test_file_diff_prefers_the_tasks_own_checkpoints():
    """The default work folder is not a git repo: with checkpoints the
    deliverable preview's diff works anyway, and shows this task's change."""
    env = _Env(http=True)
    try:
        board = env.run()
        wd = board.get("run/workdir")
        _write(wd, "a.txt", b"old\n")
        seg, _ = run_changes.begin(board, checkpoints.store_path(serve.RUNTIME_DIR, wd), wd,
                                   RID, "run", "engine")
        run_changes.record_task(board, seg, "t1", "before",
                                run_changes.task_snapshot(seg, "t1", "before"))
        _write(wd, "a.txt", b"new\n")
        run_changes.record_task(board, seg, "t1", "after",
                                run_changes.task_snapshot(seg, "t1", "after"))
        run_changes.end(board, seg, author="engine")
        board.put("tasks/t1/spec", {"title": "t", "type": "coding"}, author="system",
                  kind="spec")
        board.put("tasks/t1/files", [{"path": "a.txt", "size": 4}], author="engine",
                  task_id="t1", kind="fact")
        code, data = _get(env.port, "/file/diff?task=t1&path=a.txt")
        assert code == 200 and data["ok"] is True, data
        assert "-old" in data["diff"] and "+new" in data["diff"], data
        assert "检查点" in data["note"], data
    finally:
        env.close()


@needs_git
def test_run_changes_never_closes_when_a_spawn_slips_in():
    env = _Env(http=True)
    original_lock = serve._checkpoint_lock

    class _SpawningLock:
        def __init__(self):
            self.lock = threading.Lock()

        def acquire(self, blocking=True, timeout=-1):
            serve._current = _Alive()
            return self.lock.acquire(blocking, timeout)

        def release(self):
            self.lock.release()

        def locked(self):
            return self.lock.locked()

    try:
        board = env.run()
        env.segment(board, change=lambda wd: _write(wd, "a.txt", b"x"), close=False)
        serve._checkpoint_lock = _SpawningLock()
        code, data = _get(env.port, f"/run/changes?run={RID}")
        assert code == 200 and data["segments"][0]["open"] is True, data
    finally:
        serve._checkpoint_lock = original_lock
        serve._current = None
        env.close()


@needs_git
def test_run_changes_does_not_wait_for_the_checkpoint_lock():
    env = _Env(http=True)
    try:
        board = env.run()
        env.segment(board, change=lambda wd: _write(wd, "a.txt", b"x"), close=False)
        serve._checkpoint_lock.acquire()
        try:
            code, data = _get(env.port, f"/run/changes?run={RID}")
            assert code == 200 and data["segments"][0]["open"] is True, data
        finally:
            serve._checkpoint_lock.release()
    finally:
        env.close()


@needs_git
def test_run_changes_survives_a_board_it_cannot_write():
    env = _Env(http=True)
    try:
        board = env.run()
        env.segment(board, change=lambda wd: _write(wd, "a.txt", b"x"), close=False)
        os.chmod(board.path, stat.S_IREAD)
        try:
            code, data = _get(env.port, f"/run/changes?run={RID}")
            assert code == 200 and data["segments"][0]["open"] is True, data
        finally:
            os.chmod(board.path, stat.S_IREAD | stat.S_IWRITE)
    finally:
        env.close()


@needs_git
def test_review_endpoints_survive_a_torn_board():
    env = _Env(http=True)
    try:
        board = env.run()
        env.segment(board, change=lambda wd: _write(wd, "a.txt", b"x\n"))
        with open(board.path, "ab") as f:
            f.write(b'{"key": "\xe4\xb8')
        for path in (f"/run/changes?run={RID}",
                     f"/run/changes/diff?run={RID}&seg=1&path=a.txt"):
            code, data = _get(env.port, path)
            assert code == 500 and "error" in data, (path, code, data)
    finally:
        env.close()


def test_file_endpoints_survive_a_torn_board():
    env = _Env(http=True)
    original_popen = serve.subprocess.Popen
    original_continue = serve.conversation.continue_task_conversation
    async def no_continue(*_args, **_kwargs):
        raise AssertionError("preview must not call an agent")
    serve.subprocess.Popen = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("preview must not launch a process"))
    serve.conversation.continue_task_conversation = no_continue
    try:
        board = env.run()
        with open(board.path, "ab") as f:
            f.write(b'{"key": "\xe4\xb8')
        for path in ("/file?task=t1&path=a.txt", "/file/diff?task=t1&path=a.txt"):
            code, data = _get(env.port, path)
            assert code in (404, 500) and "error" in data, (path, code, data)
    finally:
        serve.subprocess.Popen = original_popen
        serve.conversation.continue_task_conversation = original_continue
        env.close()


def _rollback_setup(env):
    board = env.run()
    wd = board.get("run/workdir")
    _write(wd, "a.txt", b"one")
    env.segment(board, change=lambda w: (_write(w, "a.txt", b"two"),
                                         _write(w, "new.txt", b"n")))
    return board, wd


def _index_entry(rid=RID):
    serve._runs_load()
    return [e for e in serve._runs if e["run_id"] == rid][0]


@needs_git
def test_rollback_endpoint_restores_records_and_can_be_undone():
    env = _Env(http=True)
    try:
        _board, wd = _rollback_setup(env)
        code, data = _post(env.port, "/run/rollback", {"run": RID})
        assert code == 200 and data["restored"] == ["a.txt", "new.txt"], data
        assert _read(wd, "a.txt") == b"one" and not os.path.exists(os.path.join(wd, "new.txt"))
        ev = [e for e in _events(serve._run_meta["events"]) if e["type"] == "run_rolled_back"]
        assert (ev[-1]["restored"], ev[-1]["skipped"], ev[-1]["undo"]) == (2, 0, False), ev
        assert _index_entry()["rolled_back"] is True

        code, data = _post(env.port, "/run/rollback", {"run": RID})
        assert code == 409 and "上一次回滚还没撤销" in data["error"], data

        code, data = _post(env.port, "/run/rollback/undo", {"run": RID})
        assert code == 200 and data["restored"] == ["a.txt", "new.txt"], data
        assert _read(wd, "a.txt") == b"two" and os.path.exists(os.path.join(wd, "new.txt"))
        ev = [e for e in _events(serve._run_meta["events"]) if e["type"] == "run_rolled_back"]
        assert ev[-1]["undo"] is True, ev
        assert _index_entry()["rolled_back"] is False
        code, data = _post(env.port, "/run/rollback/undo", {"run": RID})
        assert code == 409 and "已经撤销过" in data["error"], data
    finally:
        env.close()


@needs_git
def test_undo_endpoint_keeps_rollback_flag_when_all_files_are_skipped():
    env = _Env(http=True)
    try:
        board, wd = _rollback_setup(env)
        with _Followup(writes_file=False):
            code, data = _post(env.port, "/run/rollback", {"run": RID})
        assert code == 200 and _index_entry()["rolled_back"] is True, data
        _write(wd, "a.txt", b"later")
        _write(wd, "new.txt", b"later")
        with _Followup(writes_file=False):
            code, data = _post(env.port, "/run/rollback/undo", {"run": RID})
        assert code == 200 and data["restored"] == [] and len(data["skipped"]) == 2, data
        assert data["undone"] is False and _index_entry()["rolled_back"] is True
        assert Blackboard(board.path, on_write=None).get(
            f"{run_changes.ROLLBACK_PREFIX}1")["undone"] is False

        _write(wd, "a.txt", b"one")
        os.remove(os.path.join(wd, "new.txt"))
        with _Followup(writes_file=False):
            code, data = _post(env.port, "/run/rollback/undo", {"run": RID})
        assert code == 200 and data["undone"] is True and _index_entry()["rolled_back"] is False
    finally:
        env.close()


@needs_git
def test_undo_endpoint_reports_and_records_partial_progress_before_retry():
    env = _Env(http=True)
    try:
        board = env.run()
        wd = board.get("run/workdir")
        _write(wd, "a.txt", b"one")
        _write(wd, "deleted.txt", b"old")
        env.segment(board, change=lambda w: (_write(w, "a.txt", b"two"),
                                             os.remove(os.path.join(w, "deleted.txt"))))
        with _Followup(writes_file=False):
            code, data = _post(env.port, "/run/rollback", {"run": RID})
        assert code == 200 and data["restored"] == ["a.txt", "deleted.txt"], data

        original_read = checkpoints._BlobReader.read
        def boom(self, sha):
            raise checkpoints.CheckpointError("模拟：撤销读 blob 失败")
        checkpoints._BlobReader.read = boom
        try:
            with _Followup(writes_file=False):
                code, data = _post(env.port, "/run/rollback/undo", {"run": RID})
        finally:
            checkpoints._BlobReader.read = original_read
        assert code == 500 and data["restored"] == ["deleted.txt"] and "重试" in data["error"], data
        assert _index_entry()["rolled_back"] is True
        rec = Blackboard(board.path, on_write=None).get(f"{run_changes.ROLLBACK_PREFIX}1")
        assert rec["undone"] is False and rec["undo"]["restored"] == ["deleted.txt"], rec

        with _Followup(writes_file=False):
            code, data = _post(env.port, "/run/rollback/undo", {"run": RID})
        assert code == 200 and data["restored"] == ["a.txt"] and data["undone"] is True, data
        assert _index_entry()["rolled_back"] is False
        assert _read(wd, "a.txt") == b"two" and not os.path.exists(os.path.join(wd, "deleted.txt"))
    finally:
        env.close()


@needs_git
def test_rollback_flag_includes_earlier_nonundoable_rollback():
    env = _Env(http=True)
    original_snapshot = checkpoints.snapshot
    try:
        _board, _wd = _rollback_setup(env)
        def no_first_post(store, workdir, ref_name, exclude=()):
            if ref_name.endswith("rollback1-post"):
                return {"commit": "", "skipped": [], "error": "模拟：拍不了"}
            return original_snapshot(store, workdir, ref_name, exclude)
        checkpoints.snapshot = no_first_post
        with _Followup(writes_file=False):
            code, data = _post(env.port, "/run/rollback", {"run": RID, "paths": ["a.txt"]})
        assert code == 200 and data["restored"] == ["a.txt"] and not data["post"], data
        checkpoints.snapshot = original_snapshot
        assert _index_entry()["rolled_back"] is True

        with _Followup(writes_file=False):
            code, data = _post(env.port, "/run/rollback", {"run": RID, "paths": ["absent.txt"]})
        assert code == 200 and data["restored"] == [], data
        assert _index_entry()["rolled_back"] is True
        with _Followup(writes_file=False):
            code, data = _post(env.port, "/run/rollback/undo", {"run": RID})
        assert code == 200 and data["undone"] is True, data
        assert _index_entry()["rolled_back"] is True
    finally:
        checkpoints.snapshot = original_snapshot
        env.close()


@needs_git
def test_rollback_clears_a_stale_index_lock_first():
    """An engine killed mid-snapshot leaves index.lock behind. With no run in
    progress serve.py clears it instead of failing the rollback (spec §5.3)."""
    env = _Env(http=True)
    try:
        _board, wd = _rollback_setup(env)
        with open(os.path.join(checkpoints.store_path(serve.RUNTIME_DIR, wd), "index.lock"), "w"):
            pass
        code, data = _post(env.port, "/run/rollback", {"run": RID})
        assert code == 200 and data["restored"] == ["a.txt", "new.txt"], data
    finally:
        env.close()


@needs_git
def test_rollback_refused_while_a_run_is_in_progress():
    env = _Env(http=True)
    try:
        _board, wd = _rollback_setup(env)
        serve._current = _Alive()
        code, data = _post(env.port, "/run/rollback", {"run": RID})
        assert code == 409 and "有运行在进行" in data["error"], data
        assert _read(wd, "a.txt") == b"two", "nothing touched"
    finally:
        env.close()


@needs_git
def test_rollback_refused_while_a_run_is_in_progress_leaves_the_open_segment_alone():
    env = _Env(http=True)
    try:
        board = env.run()
        wd = board.get("run/workdir")
        _write(wd, "a.txt", b"one")
        env.segment(board, change=lambda w: _write(w, "a.txt", b"two"), close=False)
        serve._current = _Alive()
        code, data = _post(env.port, "/run/rollback", {"run": RID})
        assert code == 409 and "有运行在进行" in data["error"], data
        seg = Blackboard(board.path, on_write=None).get("run/checkpoints/seg/1")
        assert not seg.get("after") and not seg.get("ended"), seg
    finally:
        serve._current = None
        env.close()


@needs_git
def test_rollback_endpoint_refuses_gone_checkpoints():
    """Review Focus 4."""
    env = _Env(http=True)
    try:
        _board, wd = _rollback_setup(env)
        _rmtree(checkpoints.store_path(serve.RUNTIME_DIR, wd))
        code, data = _post(env.port, "/run/rollback", {"run": RID})
        assert code == 409 and "已不存在" in data["error"], data
        assert _read(wd, "a.txt") == b"two"
    finally:
        env.close()


def test_rollback_rejects_bad_input_and_waits_its_turn():
    env = _Env(http=True)
    try:
        env.run()
        assert _post(env.port, "/run/rollback", {"run": RID, "paths": "a.txt"})[0] == 400
        assert _post(env.port, "/run/rollback", {"run": "../x"})[0] == 404
        serve._checkpoint_lock.acquire()
        try:
            code, data = _post(env.port, "/run/rollback", {"run": RID})
            assert code == 409 and "稍后再试" in data["error"], data
            assert "上一次运行收尾" in data["error"], data
        finally:
            serve._checkpoint_lock.release()
    finally:
        env.close()


@needs_git
def test_rollback_survives_a_torn_board():
    env = _Env(http=True)
    try:
        board, wd = _rollback_setup(env)
        with open(board.path, "ab") as f:
            f.write(b'{"key": "\xe4\xb8')
        code, data = _post(env.port, "/run/rollback", {"run": RID})
        assert code == 500 and "error" in data, data
        assert "没有动任何文件" in data["error"], data
        assert _read(wd, "a.txt") == b"two", "nothing touched"
        assert serve._checkpoint_lock.locked() is False
    finally:
        env.close()


@needs_git
def test_rollback_touches_nothing_when_the_board_cannot_be_written():
    env = _Env(http=True)
    try:
        board = env.run()
        wd = board.get("run/workdir")
        _write(wd, "a.txt", b"one")
        env.segment(board, change=lambda w: (_write(w, "a.txt", b"two"),
                                             _write(w, "new.txt", b"n")), close=False)
        os.chmod(board.path, stat.S_IREAD)
        try:
            code, data = _post(env.port, "/run/rollback", {"run": RID})
            assert code == 500 and "没有动任何文件" in data["error"], data
            assert _read(wd, "a.txt") == b"two" and os.path.exists(os.path.join(wd, "new.txt"))
            assert serve._checkpoint_lock.locked() is False
        finally:
            os.chmod(board.path, stat.S_IREAD | stat.S_IWRITE)
    finally:
        env.close()


@needs_git
def test_rollback_reports_a_record_it_could_not_write():
    env = _Env(http=True)
    try:
        board, _wd = _rollback_setup(env)
        os.chmod(board.path, stat.S_IREAD)
        try:
            code, data = _post(env.port, "/run/rollback", {"run": RID})
            assert code == 500 and "没有记下来" in data["error"], data
            assert serve._checkpoint_lock.locked() is False
            assert Blackboard(board.path, on_write=None).query("run/checkpoints/rollback/") == []
            events_path = serve._run_meta["events"]
            assert not os.path.exists(events_path) or not any(
                e["type"] == "run_rolled_back" for e in _events(events_path))
        finally:
            os.chmod(board.path, stat.S_IREAD | stat.S_IWRITE)
    finally:
        env.close()


@needs_git
def test_rollback_answers_even_when_the_run_index_write_fails():
    env = _Env(http=True)
    real_update = serve._runs_update
    try:
        _board, wd = _rollback_setup(env)

        def broken(run_id, **fields):
            raise OSError("disk full")
        serve._runs_update = broken
        code, data = _post(env.port, "/run/rollback", {"run": RID})
        assert code == 200 and data["restored"] == ["a.txt", "new.txt"], data
        assert _read(wd, "a.txt") == b"one"
        assert serve._checkpoint_lock.locked() is False
    finally:
        serve._runs_update = real_update
        env.close()


@needs_git
def test_rollback_answers_when_event_emit_raises_non_oserror():
    env = _Env(http=True)
    original_bus = serve.EventBus
    class BrokenBus:
        def __init__(self, *_args, **_kwargs):
            pass
        def emit(self, *_args, **_kwargs):
            raise ValueError("bad event")
        def close(self):
            pass
    try:
        _board, wd = _rollback_setup(env)
        serve.EventBus = BrokenBus
        with _Followup(writes_file=False) as fake:
            code, data = _post(env.port, "/run/rollback", {"run": RID})
        assert fake.called is False
        assert code == 200 and data["restored"] == ["a.txt", "new.txt"], data
        assert _read(wd, "a.txt") == b"one" and _index_entry()["rolled_back"] is True
    finally:
        serve.EventBus = original_bus
        env.close()


@needs_git
def test_rollback_that_restores_nothing_does_not_flag_the_run():
    env = _Env(http=True)
    try:
        _board, wd = _rollback_setup(env)
        _write(wd, "a.txt", b"three")
        _write(wd, "new.txt", b"changed")
        code, data = _post(env.port, "/run/rollback", {"run": RID})
        assert code == 200 and data["restored"] == [], data
        assert _index_entry()["rolled_back"] is False
        ev = [e for e in _events(serve._run_meta["events"]) if e["type"] == "run_rolled_back"]
        assert (ev[-1]["restored"], ev[-1]["skipped"], ev[-1]["undo"]) == (0, 2, False), ev
    finally:
        env.close()


@needs_git
def test_rollback_with_an_empty_selection_restores_nothing():
    env = _Env(http=True)
    try:
        _board, wd = _rollback_setup(env)
        code, data = _post(env.port, "/run/rollback", {"run": RID, "paths": []})
        assert code == 200 and data["restored"] == [], data
        assert _read(wd, "a.txt") == b"two" and os.path.exists(os.path.join(wd, "new.txt"))
    finally:
        env.close()


def _followup_setup(env, agent, workdir=None):
    board = env.run(workdir=workdir)
    board.put("tasks/t1/spec", {"title": "写代码", "type": "coding"}, author="system",
              kind="spec")
    board.put("tasks/t1/agent", agent, author="engine", task_id="t1", kind="fact")
    board.put("tasks/t1/status", "done", author="engine", task_id="t1", kind="fact")
    return board


class _Followup:
    """Stands in for conversation.continue_task_conversation (zero quota), and
    spies on serve.Router to see which work folder the follow-up runs in (B13)."""

    def __init__(self, writes_file, raises=False):
        self.writes_file = writes_file
        self.raises = raises
        self.called = False
        self.workdir = None
        self._real_continue = serve.conversation.continue_task_conversation
        self._real_router = serve.Router
        self._real_popen = serve.subprocess.Popen

    def __enter__(self):
        outer = self

        async def fake(task_id, message, router, board):
            outer.called = True
            if outer.writes_file:
                _write(outer.workdir, "from-followup.txt", b"x")
            if outer.raises:
                raise RuntimeError("boom")
            return ExecResult(ok=True, output="好的"), False

        def spy_router(path, workdir=".", **kw):
            outer.workdir = workdir
            return outer._real_router(path, workdir=workdir, **kw)
        serve.conversation.continue_task_conversation = fake
        serve.Router = spy_router
        def git_only(argv, **kw):
            if os.path.basename(str(argv[0])).lower() not in ("git", "git.exe"):
                raise AssertionError("follow-up test cannot launch an agent")
            return outer._real_popen(argv, **kw)
        serve.subprocess.Popen = git_only
        return self

    def __exit__(self, *exc):
        serve.conversation.continue_task_conversation = self._real_continue
        serve.Router = self._real_router
        serve.subprocess.Popen = self._real_popen


@needs_git
def test_writing_followup_gets_its_own_segment_in_the_runs_workdir():
    env = _Env(http=True)
    try:
        other = os.path.join(env.tmp, "project")
        os.makedirs(other)
        board = _followup_setup(env, "cc-sonnet", workdir=other)
        with _Followup(writes_file=True) as f:
            code, data = _post(env.port, "/task/t1/message", {"message": "再改改"})
        assert code == 200 and data["reply"] == "好的", data
        assert f.workdir == os.path.realpath(other), "B13: the run's own work folder"
        b = Blackboard(board.path, on_write=None)
        seg = b.get("run/checkpoints/seg/1")
        assert seg["kind"] == "followup" and seg["task_id"] == "t1" and seg["after"], seg
        assert b.get("tasks/t1/checkpoints/1")["after"] == seg["after"]
        s = run_changes.summary(b, serve.RUNTIME_DIR)
        assert [x["path"] for x in s["segments"][0]["files"]] == ["from-followup.txt"], s
    finally:
        env.close()


def test_readonly_followup_takes_no_checkpoint():
    env = _Env(http=True)
    try:
        board = _followup_setup(env, "cc-opus")
        with _Followup(writes_file=False) as f:
            code, data = _post(env.port, "/task/t1/message", {"message": "解释一下"})
        assert code == 200 and f.called, data
        assert Blackboard(board.path, on_write=None).query("run/checkpoints/") == []
    finally:
        env.close()


def test_writing_followup_refused_while_a_run_is_in_progress():
    env = _Env(http=True)
    try:
        _followup_setup(env, "cc-sonnet")
        serve._current = _Alive()
        with _Followup(writes_file=True) as f:
            code, data = _post(env.port, "/task/t1/message", {"message": "x"})
        assert code == 409 and "有运行在进行" in data["error"], data
        assert f.called is False, "the agent must not be called at all"
    finally:
        env.close()


def test_readonly_followup_still_allowed_while_a_run_is_in_progress():
    env = _Env(http=True)
    try:
        _followup_setup(env, "cc-opus")
        serve._current = _Alive()
        with _Followup(writes_file=False) as f:
            code, data = _post(env.port, "/task/t1/message", {"message": "x"})
        assert code == 200 and f.called, data
    finally:
        env.close()


def test_writing_followup_waits_for_the_checkpoint_lock():
    env = _Env(http=True)
    try:
        _followup_setup(env, "cc-sonnet")
        serve._checkpoint_lock.acquire()
        try:
            with _Followup(writes_file=True) as f:
                code, data = _post(env.port, "/task/t1/message", {"message": "x"})
            assert code == 409, data
            assert "正在记录文件改动" in data["error"] and "上一次运行收尾" in data["error"], data
            assert f.called is False
        finally:
            serve._checkpoint_lock.release()
    finally:
        env.close()


@needs_git
def test_writing_followup_refused_when_the_board_cannot_be_written():
    env = _Env(http=True)
    try:
        board = _followup_setup(env, "cc-sonnet")
        os.chmod(board.path, stat.S_IREAD)
        try:
            with _Followup(writes_file=True) as f:
                code, data = _post(env.port, "/task/t1/message", {"message": "x"})
            assert code == 500 and "没有动任何文件" in data["error"], data
            assert f.called is False
            assert serve._checkpoint_lock.locked() is False
        finally:
            os.chmod(board.path, stat.S_IREAD | stat.S_IWRITE)
    finally:
        env.close()


@needs_git
def test_writing_followup_answers_even_when_the_closing_write_fails():
    env = _Env(http=True)
    real_end = serve.run_changes.end
    try:
        _followup_setup(env, "cc-sonnet")

        def broken_end(*args, **kwargs):
            raise OSError("disk full")
        serve.run_changes.end = broken_end
        with _Followup(writes_file=True):
            code, data = _post(env.port, "/task/t1/message", {"message": "再改改"})
        assert code == 200 and data["reply"] == "好的", data
        assert serve._checkpoint_lock.locked() is False
    finally:
        serve.run_changes.end = real_end
        env.close()


def test_followup_refused_when_the_runs_folder_is_gone():
    for agent in ("cc-sonnet", "cc-opus"):
        env = _Env(http=True)
        try:
            workdir = os.path.join(env.tmp, "project")
            os.makedirs(workdir)
            board = _followup_setup(env, agent, workdir=workdir)
            os.rmdir(workdir)
            with _Followup(writes_file=(agent == "cc-sonnet")) as f:
                code, data = _post(env.port, "/task/t1/message", {"message": "x"})
            assert code == 409 and "工作目录已经不在了" in data["error"], data
            assert f.called is False
            assert Blackboard(board.path, on_write=None).query("run/checkpoints/") == []
            assert serve._checkpoint_lock.locked() is False
        finally:
            env.close()


@needs_git
def test_writing_followup_whose_agent_raises_answers_502():
    env = _Env(http=True)
    try:
        board = _followup_setup(env, "cc-sonnet")
        with _Followup(writes_file=True, raises=True):
            code, data = _post(env.port, "/task/t1/message", {"message": "x"})
        assert code == 502 and data["error"] and not data["error"].startswith("追问失败："), data
        assert serve._checkpoint_lock.locked() is False
        b = Blackboard(board.path, on_write=None)
        assert b.get("run/checkpoints/seg/1")["after"]
        s = run_changes.summary(b, serve.RUNTIME_DIR)
        assert [x["path"] for x in s["segments"][0]["files"]] == ["from-followup.txt"], s
    finally:
        env.close()


def test_done_followup_answers_when_router_fails():
    env = _Env(http=True)
    try:
        _followup_setup(env, "cc-sonnet")
        with _Followup(writes_file=False) as fake:
            def broken_router(*_args, **_kwargs):
                raise ValueError("bad matrix")
            serve.Router = broken_router
            code, data = _post(env.port, "/task/t1/message", {"message": "x"})
        assert fake.called is False
        assert code == 502 and "error" in data, data
    finally:
        env.close()


def test_done_followup_answers_when_stale_notification_fails():
    env = _Env(http=True)
    original_mark = serve.conversation.mark_stale_dependents
    try:
        _followup_setup(env, "cc-opus")
        def broken_mark(*_args, **_kwargs):
            raise ValueError("bad dependent")
        serve.conversation.mark_stale_dependents = broken_mark
        with _Followup(writes_file=False) as fake:
            code, data = _post(env.port, "/task/t1/message", {"message": "x"})
        assert fake.called is True
        assert code == 200 and data["reply"] == "好的" and "追问已完成" in data["warning"], data
    finally:
        serve.conversation.mark_stale_dependents = original_mark
        env.close()


def test_writing_followup_warns_when_checkpoint_is_unavailable():
    env = _Env(http=True)
    original_available = checkpoints.available
    try:
        board = _followup_setup(env, "cc-sonnet")
        checkpoints.available = lambda: False
        with _Followup(writes_file=True) as fake:
            code, data = _post(env.port, "/task/t1/message", {"message": "x"})
        assert fake.called is True and code == 200, data
        assert _read(board.get("run/workdir"), "from-followup.txt") == b"x"
        assert "不能一键回滚" in data["warning"] and "没装 git" in data["warning"], data
    finally:
        checkpoints.available = original_available
        env.close()


@needs_git
def test_writing_followup_does_not_reclose_a_segment_closed_meanwhile():
    env = _Env(http=True)
    original_lock = serve._checkpoint_lock

    class _ClosingLock:
        def __init__(self, board):
            self.board = board
            self.lock = threading.Lock()
            self.after = ""

        def acquire(self, blocking=True, timeout=-1):
            run_changes.close_open(Blackboard(self.board.path, on_write=None),
                                   serve.RUNTIME_DIR, RID, note="", author="system",
                                   exclude=(serve.DATA_DIR,))
            self.after = Blackboard(self.board.path, on_write=None).get(
                "run/checkpoints/seg/1")["after"]
            return self.lock.acquire(blocking, timeout)

        def release(self):
            self.lock.release()

        def locked(self):
            return self.lock.locked()

    try:
        board = _followup_setup(env, "cc-sonnet")
        env.segment(board, change=lambda w: _write(w, "run.txt", b"x"), close=False)
        serve._checkpoint_lock = _ClosingLock(board)
        with _Followup(writes_file=True):
            code, data = _post(env.port, "/task/t1/message", {"message": "再改改"})
        assert code == 200, data
        b = Blackboard(board.path, on_write=None)
        seg1 = b.get("run/checkpoints/seg/1")
        assert seg1["note"] == "" and seg1["after"] == serve._checkpoint_lock.after, seg1
        assert b.get("run/checkpoints/seg/2")["kind"] == "followup"
    finally:
        serve._checkpoint_lock = original_lock
        env.close()


if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll serve checkpoint tests passed.")
