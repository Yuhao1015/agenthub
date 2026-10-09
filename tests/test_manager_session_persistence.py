"""Manager clarification persistence. No real model calls.

Run: python3 tests/test_manager_session_persistence.py
"""
from __future__ import annotations

import copy
import json
import os
import stat
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from unittest import SkipTest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import serve
from test_gate_serve import authorize_local_agents

# macOS 的系统代理会劫持 localhost，urllib 必须显式关掉代理
# （CLAUDE.md 记过这个坑；用 http.client 的测试天然不受影响）
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _symlink(target, link):
    try:
        os.symlink(target, link, target_is_directory=os.path.isdir(target))
    except OSError as exc:
        if os.name == 'nt' and getattr(exc, 'winerror', None) == 1314:
            raise SkipTest('Windows account lacks symbolic-link privilege') from exc
        raise


def _get(port: int, path: str):
    try:
        with OPENER.open(f"http://127.0.0.1:{port}{path}") as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _post(port: int, path: str, body: dict):
    return _post_with_headers(port, path, body,
                              {"Content-Type": "application/json"})


def _post_with_headers(port: int, path: str, body: dict, headers: dict):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(), method="POST",
        headers=headers)
    try:
        with OPENER.open(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@contextmanager
def running_server(handler=serve.Handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@contextmanager
def isolated_desktop():
    names = ("DATA_DIR", "PROJECT", "RUNTIME_DIR", "RUNS_DIR", "INDEX_PATH",
             "CONFIG_PATH", "MERGED_MATRIX_PATH", "PREFLIGHT_PATH",
             "CREDENTIALS_PATH", "SCHEDULES_PATH", "SESSIONS_DIR",
             "ACTIVE_SESSION_PATH", "MATRIX", "BOARD_PATH", "EVENTS_PATH",
             "WORKDIR", "COMMIT")
    saved = {name: getattr(serve, name) for name in names}
    saved_sessions = dict(serve._sessions)
    saved_run_meta = dict(serve._run_meta)
    saved_runs = list(serve._runs)
    saved_active_sid = getattr(serve, "_active_sid", "")
    saved_inflight = set(getattr(serve, "_chat_inflight", set()))
    with tempfile.TemporaryDirectory() as td:
        serve._sessions.clear()
        serve._run_meta = {}
        serve._runs = []
        serve._active_sid = ""
        serve._chat_inflight.clear()
        serve._configure(td, "persist-test", 8787)
        authorize_local_agents()
        try:
            yield td
        finally:
            serve._sessions.clear()
            serve._sessions.update(saved_sessions)
            serve._run_meta = saved_run_meta
            serve._runs = saved_runs
            serve._active_sid = saved_active_sid
            serve._chat_inflight.clear()
            serve._chat_inflight.update(saved_inflight)
            for name, value in saved.items():
                setattr(serve, name, value)


def test_session_round_trip_excludes_router_and_is_private():
    with isolated_desktop():
        sess = serve._new_manager_session("a1b2c3d4e5f6", now=1000.0)
        sess["router"] = object()
        sess["history"].append({"role": "user", "text": "hello", "ts": 1000.0})
        serve._session_save(sess)
        path = os.path.join(serve.SESSIONS_DIR, "a1b2c3d4e5f6.json")
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        assert "router" not in raw
        assert raw["history"][0]["text"] == "hello"
        if os.name != 'nt':  # POSIX mode bits do not represent Windows ACLs.
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        loaded = serve._session_load_disk("a1b2c3d4e5f6")
        assert loaded["session_id"] == "a1b2c3d4e5f6"
        assert "router" not in loaded


def test_session_schema_rejects_malformed_snapshots():
    valid = {
        "schema_version": 1,
        "session_id": "a1b2c3d4e5f6",
        "created_at": 1000.0,
        "updated_at": 1001.0,
        "status": "active",
        "history": [
            {"role": "user", "text": "hello", "ts": 1000.0},
            {"role": "manager", "text": "范围？", "ts": 1001.0,
             "questions": [
                 {"id": "q1", "question": "范围？", "options": [
                     {"label": "全公司", "detail": "所有团队", "other": False}
                 ]}
             ],
             "answered": False, "answers": {}},
        ],
        "questions": 1,
        "awaiting_confirmation": False,
        "proposal": None,
        "cost_usd": 0.25,
        "run_id": "",
    }

    def set_field(name, value):
        return lambda data: data.__setitem__(name, value)

    def set_history_entry(name, value):
        return lambda data: data["history"][0].__setitem__(name, value)

    def set_question(name, value):
        return lambda data: data["history"][1]["questions"][0].__setitem__(
            name, value)

    def set_option(name, value):
        return lambda data: data["history"][1]["questions"][0]["options"][0].__setitem__(
            name, value)

    cases = (
        ("missing top-level field", lambda data: data.pop("created_at")),
        ("unknown router", set_field("router", object())),
        ("boolean schema version", set_field("schema_version", True)),
        ("numeric session id", set_field("session_id", 123456789012)),
        ("negative created timestamp", set_field("created_at", -1.0)),
        ("boolean created timestamp", set_field("created_at", True)),
        ("NaN updated timestamp", set_field("updated_at", float("nan"))),
        ("infinite updated timestamp", set_field("updated_at", float("inf"))),
        ("updated timestamp before creation",
         lambda data: data.update({"created_at": 1001.0, "updated_at": 1000.0})),
        ("invalid status", set_field("status", "done")),
        ("scalar history", set_field("history", "hello")),
        ("scalar history entry", set_field("history", ["hello"])),
        ("unknown history field", set_history_entry("router", "injected")),
        ("invalid history role", set_history_entry("role", "system")),
        ("non-string history text", set_history_entry("text", 1)),
        ("missing history timestamp", lambda data: data["history"][0].pop("ts")),
        ("negative history timestamp", set_history_entry("ts", -1.0)),
        ("NaN history timestamp", set_history_entry("ts", float("nan"))),
        ("boolean question count", set_field("questions", True)),
        ("negative question count", set_field("questions", -1)),
        ("non-boolean confirmation", set_field("awaiting_confirmation", "false")),
        ("awaiting confirmation without proposal",
         set_field("awaiting_confirmation", True)),
        ("scalar proposal", set_field("proposal", "goal")),
        ("incomplete proposal", set_field("proposal", {"goal": "G"})),
        ("unknown proposal field",
         set_field("proposal", {"goal": "G", "constraints": "C", "x": 1})),
        ("non-string proposal field",
         set_field("proposal", {"goal": "G", "constraints": None})),
        ("boolean cost", set_field("cost_usd", True)),
        ("negative cost", set_field("cost_usd", -0.01)),
        ("NaN cost", set_field("cost_usd", float("nan"))),
        ("infinite cost", set_field("cost_usd", float("inf"))),
        ("non-string run id", set_field("run_id", None)),
        ("active session with run id", set_field("run_id", "0a1b2c3d")),
        ("started session without proposal",
         lambda data: data.update({"status": "started", "run_id": "0a1b2c3d"})),
        ("question metadata on user entry",
         lambda data: data["history"][0].update(
             {"questions": [], "answered": False, "answers": {}})),
        ("partial question metadata", lambda data: data["history"][1].pop("answers")),
        ("non-list questions metadata",
         lambda data: data["history"][1].__setitem__("questions", {})),
        ("unknown question field", set_question("extra", True)),
        ("non-string question id", set_question("id", 1)),
        ("non-string question text", set_question("question", None)),
        ("non-list question options", set_question("options", {})),
        ("scalar option", lambda data: data["history"][1]["questions"][0].__setitem__(
             "options", ["all"])),
        ("unknown option field", set_option("extra", True)),
        ("non-string option label", set_option("label", 1)),
        ("non-string option detail", set_option("detail", None)),
        ("non-boolean option other", set_option("other", "false")),
        ("non-boolean answered",
         lambda data: data["history"][1].__setitem__("answered", 0)),
        ("non-dict answers", lambda data: data["history"][1].__setitem__(
             "answers", [])),
        ("non-string answer", lambda data: data["history"][1].__setitem__(
             "answers", {"q1": 1})),
    )

    for name, mutate in cases:
        data = copy.deepcopy(valid)
        mutate(data)
        try:
            serve._validate_session_data(data, expected_sid="a1b2c3d4e5f6")
        except serve.SessionDataError:
            pass
        else:
            raise AssertionError(f"malformed session accepted: {name}")

    started = copy.deepcopy(valid)
    started.update({"status": "started", "run_id": "0a1b2c3d",
                    "proposal": {"goal": "G", "constraints": "C"}})
    assert serve._validate_session_data(valid) == valid
    assert serve._validate_session_data(started) == started


def test_session_schema_accepts_valid_consistency_edges():
    base = {
        "schema_version": 1,
        "session_id": "a1b2c3d4e5f6",
        "created_at": 1000.0,
        "updated_at": 1000.0,
        "status": "active",
        "history": [],
        "questions": 0,
        "awaiting_confirmation": False,
        "proposal": None,
        "cost_usd": 0.0,
        "run_id": "",
    }
    proposal = {"goal": "G", "constraints": "C"}
    cases = (
        ("equal timestamps", {}),
        ("awaiting valid proposal",
         {"awaiting_confirmation": True, "proposal": proposal}),
        ("correction retains proposal", {"proposal": proposal}),
        ("started after confirmation",
         {"status": "started", "run_id": "0a1b2c3d", "proposal": proposal}),
    )

    for name, updates in cases:
        data = copy.deepcopy(base)
        data.update(copy.deepcopy(updates))
        assert serve._validate_session_data(data) == data, name


def test_session_schema_rejects_filename_sid_mismatch():
    data = serve._new_manager_session("a1b2c3d4e5f6", now=1000.0)
    try:
        serve._validate_session_data(data, expected_sid="000000000000")
    except serve.SessionDataError as exc:
        assert "does not match" in str(exc)
    else:
        raise AssertionError("session data accepted under a different filename SID")


def test_invalid_sid_never_builds_a_path():
    with isolated_desktop():
        for sid in ("../escape", "ABCDEF123456", "short", "a" * 13):
            try:
                serve._session_load_disk(sid)
            except ValueError as exc:
                assert "session id" in str(exc).lower()
            else:
                raise AssertionError(f"invalid sid accepted: {sid}")
        assert not os.path.exists(os.path.join(serve.RUNTIME_DIR, "escape.json"))


def test_active_pointer_round_trip_and_compare_before_clear():
    with isolated_desktop():
        serve._set_active_session("a1b2c3d4e5f6")
        assert serve._active_session_id() == "a1b2c3d4e5f6"
        serve._clear_active_session("000000000000")
        assert serve._active_session_id() == "a1b2c3d4e5f6"
        serve._clear_active_session("a1b2c3d4e5f6")
        assert serve._active_session_id() == ""


def test_stale_clear_cannot_delete_newer_pointer_under_interleaving():
    with isolated_desktop():
        old_sid = "a1b2c3d4e5f6"
        new_sid = "000000000000"
        serve._set_active_session(old_sid)

        unlink_entered = threading.Event()
        release_unlink = threading.Event()
        setter_attempted = threading.Event()
        setter_done = threading.Event()
        errors = []
        original_unlink = serve.os.unlink

        def paused_unlink(path):
            if path == serve.ACTIVE_SESSION_PATH:
                unlink_entered.set()
                if not release_unlink.wait(3):
                    raise AssertionError("test did not release active pointer unlink")
            return original_unlink(path)

        def stale_clear():
            try:
                serve._clear_active_session(old_sid)
            except BaseException as exc:
                errors.append(exc)

        def set_new_pointer():
            setter_attempted.set()
            try:
                serve._set_active_session(new_sid)
            except BaseException as exc:
                errors.append(exc)
            finally:
                setter_done.set()

        serve.os.unlink = paused_unlink
        clear_thread = threading.Thread(target=stale_clear)
        setter_thread = threading.Thread(target=set_new_pointer)
        try:
            clear_thread.start()
            assert unlink_entered.wait(2), "stale clear never reached pointer unlink"
            setter_thread.start()
            assert setter_attempted.wait(2), "new pointer setter never started"
            setter_finished_while_clear_paused = setter_done.wait(1)
        finally:
            release_unlink.set()
            clear_thread.join(3)
            setter_thread.join(3)
            serve.os.unlink = original_unlink

        assert not clear_thread.is_alive() and not setter_thread.is_alive()
        assert not errors, errors
        assert not setter_finished_while_clear_paused, \
            "new pointer write was not serialized with compare-and-clear"
        assert serve._active_session_id() == new_sid
        with open(serve.ACTIVE_SESSION_PATH, encoding="utf-8") as f:
            assert json.load(f) == {"session_id": new_sid}


def test_current_endpoint_returns_active_and_current_run_archive():
    with isolated_desktop():
        sid = "a1b2c3d4e5f6"
        sess = serve._new_manager_session(sid, now=1000.0)
        sess["history"] = [{"role": "user", "text": "draft", "ts": 1000.0}]
        serve._session_save(sess)
        sess["router"] = object()
        serve._sessions[sid] = sess
        serve._set_active_session(sid)
        run_id = "0a1b2c3d"
        run_dir = os.path.join(serve.RUNS_DIR, run_id)
        os.makedirs(run_dir)
        archive = {**serve._session_disk_dict(sess), "status": "started",
                   "run_id": run_id,
                   "proposal": {"goal": "old run", "constraints": ""},
                   "history": [{"role": "user", "text": "old run", "ts": 900.0}]}
        serve._atomic_write(os.path.join(run_dir, "manager-session.json"),
                            json.dumps(archive), mode=0o600)
        serve._run_meta = {"id": run_id}
        with running_server() as port:
            code, body = _get(port, "/chat/session/current")
        assert code == 200, (code, body)
        assert body["active"]["session_id"] == sid
        assert body["active"]["history"][0]["text"] == "draft"
        assert body["run"]["run_id"] == run_id
        assert body["run"]["history"][0]["text"] == "old run"
        # Router 不可序列化，也绝不该出现在给前端的快照里
        assert "router" not in json.dumps(body)
    print("PASS: test_current_endpoint_returns_active_and_current_run_archive")


def _write_started_archive(path: str, run_id: str, text: str) -> None:
    sess = serve._new_manager_session("a1b2c3d4e5f6", now=1000.0)
    sess.update({
        "status": "started",
        "run_id": run_id,
        "proposal": {"goal": "G", "constraints": ""},
        "history": [{"role": "user", "text": text, "ts": 1000.0}],
    })
    serve._atomic_write(path, json.dumps(serve._session_disk_dict(sess)), mode=0o600)


def test_current_rejects_symlinked_run_directory_escape():
    with isolated_desktop() as td:
        run_id = "deadbeef"
        outside_dir = os.path.join(td, "outside-run")
        os.makedirs(outside_dir)
        outside_archive = os.path.join(outside_dir, "manager-session.json")
        _write_started_archive(outside_archive, run_id, "OUTSIDE_SECRET")
        _symlink(outside_dir, os.path.join(serve.RUNS_DIR, run_id))
        serve._run_meta = {"id": run_id}

        with running_server() as port:
            code, body = _get(port, "/chat/session/current")

        assert code == 500 and "outside" in body["error"].lower(), (code, body)


def test_current_rejects_symlinked_run_archive_escape():
    with isolated_desktop() as td:
        run_id = "cafebabe"
        run_dir = os.path.join(serve.RUNS_DIR, run_id)
        os.makedirs(run_dir)
        outside_archive = os.path.join(td, "outside-manager-session.json")
        _write_started_archive(outside_archive, run_id, "OUTSIDE_SECRET")
        _symlink(outside_archive, os.path.join(run_dir, "manager-session.json"))
        serve._run_meta = {"id": run_id}

        with running_server() as port:
            code, body = _get(port, "/chat/session/current")

        assert code == 500 and "outside" in body["error"].lower(), (code, body)


def test_discard_is_idempotent_but_rejects_a_stale_tab():
    with isolated_desktop():
        sid = "a1b2c3d4e5f6"
        other = "000000000000"
        sess = serve._new_manager_session(sid)
        serve._session_save(sess)
        serve._set_active_session(sid)
        with running_server() as port:
            code, _ = _post(port, "/chat/session/discard", {"session_id": other})
            assert code == 409, code
            code, _ = _post(port, "/chat/session/discard", {"session_id": sid})
            assert code == 200, code
            code, _ = _post(port, "/chat/session/discard", {"session_id": sid})
            assert code == 200, "重复丢弃必须幂等"
        assert serve._active_session_id() == ""
        assert not os.path.exists(os.path.join(serve.SESSIONS_DIR, f"{sid}.json"))
    print("PASS: test_discard_is_idempotent_but_rejects_a_stale_tab")


def test_pending_session_is_reported_and_cannot_be_discarded():
    with isolated_desktop():
        sid = "a1b2c3d4e5f6"
        sess = serve._new_manager_session(sid)
        serve._session_save(sess)
        serve._set_active_session(sid)
        serve._chat_inflight.add(sid)

        with running_server() as port:
            code, current = _get(port, "/chat/session/current")
            assert code == 200 and current["active"]["pending"] is True, current
            code, body = _post(port, "/chat/session/discard", {"session_id": sid})

        assert code == 409 and "progress" in body["error"], (code, body)
        assert serve._active_session_id() == sid
        assert os.path.exists(serve._session_path(sid))


def test_discard_rechecks_pending_after_inflight_transition_before_delete():
    with isolated_desktop():
        sid = "a1b2c3d4e5f6"
        sess = serve._new_manager_session(sid)
        serve._session_save(sess)
        serve._set_active_session(sid)
        session_path = serve._session_path(sid)
        path_lookup_entered = threading.Event()
        release_path_lookup = threading.Event()
        transition_holds_lock = threading.Event()
        release_transition = threading.Event()
        response = {}
        transition_errors = []
        original_session_path = serve._session_path

        def paused_session_path(value):
            path = original_session_path(value)
            path_lookup_entered.set()
            if not release_path_lookup.wait(3):
                raise AssertionError("test did not release discard path lookup")
            return path

        def discard(port):
            try:
                response["value"] = _post(
                    port, "/chat/session/discard", {"session_id": sid})
            except BaseException as exc:
                response["error"] = exc

        def become_inflight():
            try:
                with serve._chat_lock:
                    serve._chat_inflight.add(sid)
                    transition_holds_lock.set()
                    if not release_transition.wait(3):
                        raise AssertionError("test did not release inflight transition")
            except BaseException as exc:
                transition_errors.append(exc)

        serve._session_path = paused_session_path
        try:
            with running_server() as port:
                discard_thread = threading.Thread(target=discard, args=(port,))
                discard_thread.start()
                assert path_lookup_entered.wait(2), \
                    "discard never reached the post-pending path lookup"
                transition_thread = threading.Thread(target=become_inflight)
                transition_thread.start()
                assert transition_holds_lock.wait(2), \
                    "inflight transition never acquired the chat lock"
                release_path_lookup.set()
                discard_thread.join(2)
                discard_finished_while_turn_held_lock = not discard_thread.is_alive()
                release_transition.set()
                transition_thread.join(3)
                discard_thread.join(3)
        finally:
            release_path_lookup.set()
            release_transition.set()
            serve._session_path = original_session_path

        assert discard_finished_while_turn_held_lock, \
            "discard waited for the manager-turn lock instead of returning 409"
        assert not discard_thread.is_alive(), "discard deadlocked with inflight transition"
        assert not transition_thread.is_alive(), "inflight transition did not finish"
        assert not transition_errors, transition_errors
        assert "error" not in response, response
        code, body = response["value"]
        assert code == 409 and "progress" in body["error"], (code, body)
        assert serve._active_session_id() == sid
        assert os.path.exists(session_path)


def test_invalid_sid_http_returns_json_400():
    with isolated_desktop(), running_server() as port:
        code, body = _post(port, "/chat/session/discard", {"session_id": "../escape"})
        assert code == 400, (code, body)
        assert "session" in body["error"].lower()
    print("PASS: test_invalid_sid_http_returns_json_400")


def test_discard_endpoint_uses_common_content_type_and_origin_gates():
    with isolated_desktop(), running_server() as port:
        code, body = _post_with_headers(
            port, "/chat/session/discard", {"session_id": "a1b2c3d4e5f6"},
            {"Content-Type": "text/plain"})
        assert code == 400 and "content-type" in body["error"].lower(), (code, body)

        code, body = _post_with_headers(
            port, "/chat/session/discard", {"session_id": "a1b2c3d4e5f6"},
            {"Content-Type": "application/json", "Origin": "https://evil.example"})
        assert code == 403 and "cross-origin" in body["error"], (code, body)


def test_discard_rejects_non_object_json_without_dropping_connection():
    with isolated_desktop():
        try:
            with running_server() as port:
                code, body = _post(port, "/chat/session/discard", [])
        except Exception as exc:
            code, body = "DROPPED", {"error": type(exc).__name__}

        assert code == 400 and "object" in body["error"].lower(), (code, body)


def test_current_and_discard_work_in_legacy_mode_without_writes():
    with isolated_desktop() as td:
        sid = "a1b2c3d4e5f6"
        legacy_cwd = os.path.join(td, "legacy-cwd")
        os.makedirs(legacy_cwd)
        previous_cwd = os.getcwd()
        serve.RUNTIME_DIR = ""
        serve.RUNS_DIR = ""
        serve.SESSIONS_DIR = ""
        serve.ACTIVE_SESSION_PATH = ""
        serve._active_sid = sid
        sess = serve._new_manager_session(sid)
        sess["history"] = [{"role": "user", "text": "memory only", "ts": 1.0}]
        sess["router"] = object()
        serve._sessions[sid] = sess
        try:
            os.chdir(legacy_cwd)
            with running_server() as port:
                code, current = _get(port, "/chat/session/current")
                assert code == 200, (code, current)
                assert current["active"]["history"][0]["text"] == "memory only"
                assert "router" not in json.dumps(current)
                code, body = _post(
                    port, "/chat/session/discard", {"session_id": sid})
            assert code == 200 and body == {"ok": True}, (code, body)
            assert serve._active_session_id() == ""
            assert sid not in serve._sessions
            assert os.listdir(legacy_cwd) == []
        finally:
            os.chdir(previous_cwd)


def test_corrupt_session_returns_json_500_without_overwrite():
    """损坏的会话文件要报 500 并**保留原文件** —— 自动覆盖等于把用户
    唯一的现场证据抹掉。"""
    with isolated_desktop():
        sid = "a1b2c3d4e5f6"
        path = os.path.join(serve.SESSIONS_DIR, f"{sid}.json")
        serve._atomic_write(path, "{broken", mode=0o600)
        serve._set_active_session(sid)
        with running_server() as port:
            code, body = _get(port, "/chat/session/current")
        assert code == 500 and "unreadable" in body["error"], (code, body)
        assert open(path, encoding="utf-8").read() == "{broken"
    print("PASS: test_corrupt_session_returns_json_500_without_overwrite")


def test_invalid_utf8_session_returns_typed_error_and_json_500():
    with isolated_desktop():
        sid = "a1b2c3d4e5f6"
        path = os.path.join(serve.SESSIONS_DIR, f"{sid}.json")
        original = b"\xff\xfe{not-utf8-json"
        with open(path, "wb") as f:
            f.write(original)
        serve._set_active_session(sid)

        with running_server() as port:
            code, body = _get(port, "/chat/session/current")
        assert code == 500 and "unreadable" in body["error"], (code, body)

        try:
            serve._session_load_disk(sid)
        except serve.SessionDataError as exc:
            assert "unreadable" in str(exc)
        else:
            raise AssertionError("invalid UTF-8 did not raise SessionDataError")

        with open(path, "rb") as f:
            assert f.read() == original


def test_invalid_utf8_active_pointer_returns_json_500_without_overwrite():
    with isolated_desktop():
        original = b"\xff\xfe{not-utf8-json"
        with open(serve.ACTIVE_SESSION_PATH, "wb") as f:
            f.write(original)

        try:
            with running_server() as port:
                code, body = _get(port, "/chat/session/current")
        except Exception as exc:
            code, body = "DROPPED", {"error": type(exc).__name__}

        assert code == 500 and "unreadable" in body["error"], (code, body)
        with open(serve.ACTIVE_SESSION_PATH, "rb") as f:
            assert f.read() == original


def test_invalid_utf8_run_archive_returns_json_500_without_overwrite():
    with isolated_desktop():
        run_id = "0a1b2c3d"
        run_dir = os.path.join(serve.RUNS_DIR, run_id)
        os.makedirs(run_dir)
        path = os.path.join(run_dir, "manager-session.json")
        original = b"\xff\xfe{not-utf8-json"
        with open(path, "wb") as f:
            f.write(original)
        serve._run_meta = {"id": run_id}

        try:
            with running_server() as port:
                code, body = _get(port, "/chat/session/current")
        except Exception as exc:
            code, body = "DROPPED", {"error": type(exc).__name__}

        assert code == 500 and "unreadable" in body["error"], (code, body)
        with open(path, "rb") as f:
            assert f.read() == original


def test_restart_continues_with_exact_disk_history_and_cost():
    with isolated_desktop(), running_server() as port:
        original_router = serve.Router
        original_step = serve.manager.manager_step
        seen = []

        async def fake_step(history, router, workdir, force_finish=False,
                            on_event=None):
            seen.append(copy.deepcopy(history))
            if len(seen) == 1:
                return {"status": "asking", "question": "范围？",
                        "questions": [{"id": "q1", "question": "范围？",
                                       "options": []}], "_cost_usd": 0.25}
            return {"status": "ready", "goal": "G", "constraints": "C",
                    "_cost_usd": 0.5}

        serve.Router = lambda *args, **kwargs: SimpleNamespace(all_agents=lambda: [])
        serve.manager.manager_step = fake_step
        try:
            sid = "a1b2c3d4e5f6"
            code, first = _post(
                port, "/chat", {"session_id": sid, "message": "做报表"})
            assert code == 200 and first["status"] == "asking", (code, first)
            persisted_first = serve._session_load_disk(sid)
            assert persisted_first["questions"] == 1
            assert persisted_first["cost_usd"] == 0.25
            assert persisted_first["history"][-1] == {
                "role": "manager", "text": "范围？",
                "ts": persisted_first["history"][-1]["ts"],
                "questions": [{"id": "q1", "question": "范围？",
                               "options": []}],
                "answered": False, "answers": {},
            }
            with open(serve._session_path(sid), encoding="utf-8") as f:
                assert "router" not in json.load(f)

            serve._sessions.clear()  # process restart: only disk remains
            code, second = _post(port, "/chat", {
                "session_id": sid, "message": "全公司",
                "choice_answers": {"q1": "全公司"},
            })
            assert code == 200 and second["status"] == "confirm", (code, second)
            expected_prior = copy.deepcopy(persisted_first["history"])
            expected_prior[-1]["answered"] = True
            expected_prior[-1]["answers"] = {"q1": "全公司"}
            assert seen[1][:2] == expected_prior
            assert [item["text"] for item in seen[1]] == [
                "做报表", "范围？", "全公司"]
            assert second["manager_cost_usd"] == 0.75
            assert serve._sessions[sid].get("router") is not None

            persisted_second = serve._session_load_disk(sid)
            assert persisted_second["cost_usd"] == 0.75
            assert persisted_second["awaiting_confirmation"] is True
            assert persisted_second["proposal"] == {
                "goal": "G", "constraints": "C"}
            assert persisted_second["history"][-1]["text"] == (
                "确认一下——\n目标：G\n约束：C\n"
                "没问题回复“对/开始”；要改的话直接说哪里不对。")
        finally:
            serve.Router = original_router
            serve.manager.manager_step = original_step


def test_orphan_session_resume_claims_active_pointer():
    with isolated_desktop():
        sid = "a1b2c3d4e5f6"
        orphan = serve._new_manager_session(sid)
        orphan["history"] = [{"role": "user", "text": "from disk", "ts": 1.0}]
        serve._session_save(orphan)
        serve._sessions.clear()
        original_router = serve.Router
        original_step = serve.manager.manager_step

        async def fake_step(*args, **kwargs):
            return {"status": "asking", "question": "Q", "questions": [],
                    "_cost_usd": 0.0}

        serve.Router = lambda *args, **kwargs: SimpleNamespace(all_agents=lambda: [])
        serve.manager.manager_step = fake_step
        try:
            with running_server() as port:
                code, body = _post(
                    port, "/chat", {"session_id": sid, "message": "continue"})
                assert code == 200 and body["status"] == "asking", (code, body)
                assert serve._active_session_id() == sid
                code, current = _get(port, "/chat/session/current")
                assert code == 200 and current["active"]["session_id"] == sid
                code, conflict = _post(port, "/chat", {
                    "session_id": "000000000000", "message": "second draft"})
                assert code == 409 and "another manager session" in \
                    conflict["error"], (code, conflict)
        finally:
            serve.Router = original_router
            serve.manager.manager_step = original_step


def test_orphan_claim_preserves_file_when_newer_session_becomes_active():
    with isolated_desktop():
        orphan_sid = "a1b2c3d4e5f6"
        newer_sid = "000000000000"
        orphan = serve._new_manager_session(orphan_sid)
        orphan["history"] = [{"role": "user", "text": "orphan", "ts": 1.0}]
        serve._session_save(orphan)
        serve._sessions.clear()
        load_entered = threading.Event()
        release_load = threading.Event()
        response = {}
        manager_calls = []
        original_load = serve._session_load_disk
        original_router = serve.Router
        original_step = serve.manager.manager_step

        def paused_load(sid):
            loaded = original_load(sid)
            if sid == orphan_sid:
                load_entered.set()
                if not release_load.wait(3):
                    raise AssertionError("orphan load was not released")
            return loaded

        async def fake_step(*args, **kwargs):
            manager_calls.append(1)
            return {"status": "asking", "question": "Q", "questions": [],
                    "_cost_usd": 0.0}

        serve._session_load_disk = paused_load
        serve.Router = lambda *args, **kwargs: SimpleNamespace(all_agents=lambda: [])
        serve.manager.manager_step = fake_step
        try:
            with running_server() as port:
                request = threading.Thread(target=lambda: response.setdefault(
                    "value", _post(port, "/chat", {
                        "session_id": orphan_sid, "message": "continue"})))
                request.start()
                assert load_entered.wait(2), "request never loaded the orphan"
                newer = serve._new_manager_session(newer_sid)
                serve._session_save(newer)
                serve._set_active_session(newer_sid)
                release_load.set()
                request.join(3)
                assert not request.is_alive()
            code, body = response["value"]
            assert code == 409 and "another manager session" in body["error"], \
                (code, body)
            assert serve._active_session_id() == newer_sid
            assert os.path.exists(serve._session_path(orphan_sid))
            assert manager_calls == []
        finally:
            release_load.set()
            serve._session_load_disk = original_load
            serve.Router = original_router
            serve.manager.manager_step = original_step


def test_user_turn_is_persisted_before_manager_call():
    with isolated_desktop(), running_server() as port:
        sid = "a1b2c3d4e5f6"
        original_router = serve.Router
        original_step = serve.manager.manager_step
        snapshots = []

        async def fake_step(*args, **kwargs):
            snapshots.append(serve._session_load_disk(sid))
            return {"status": "asking", "question": "Q", "questions": [],
                    "_cost_usd": 0.0}

        serve.Router = lambda *args, **kwargs: SimpleNamespace(all_agents=lambda: [])
        serve.manager.manager_step = fake_step
        try:
            code, body = _post(
                port, "/chat", {"session_id": sid, "message": "persist first"})
            assert code == 200, (code, body)
            assert snapshots[0]["history"] == [{
                "role": "user", "text": "persist first",
                "ts": snapshots[0]["history"][0]["ts"],
            }]
            assert snapshots[0]["status"] == "active"
        finally:
            serve.Router = original_router
            serve.manager.manager_step = original_step


def test_choice_answers_are_persisted_on_previous_question():
    with isolated_desktop():
        sid = "a1b2c3d4e5f6"
        sess = serve._new_manager_session(sid)
        sess["history"] = [{"role": "manager", "text": "范围？", "ts": 1.0,
                            "questions": [{"id": "q1", "question": "范围？",
                                           "options": []}],
                            "answered": False, "answers": {}}]
        serve._session_save(sess)
        serve._set_active_session(sid)
        serve._sessions.clear()
        original_router = serve.Router
        original_step = serve.manager.manager_step

        async def fake_step(*args, **kwargs):
            return {"status": "ready", "goal": "G", "constraints": "",
                    "_cost_usd": 0.0}

        serve.Router = lambda *args, **kwargs: SimpleNamespace(all_agents=lambda: [])
        serve.manager.manager_step = fake_step
        try:
            with running_server() as port:
                code, _ = _post(port, "/chat", {"session_id": sid,
                    "message": "范围？：全公司",
                    "choice_answers": {"q1": "全公司"}})
            assert code == 200
            saved = serve._session_load_disk(sid)
            assert saved["history"][0]["answered"] is True
            assert saved["history"][0]["answers"] == {"q1": "全公司"}
        finally:
            serve.Router = original_router
            serve.manager.manager_step = original_step


def test_choice_answers_normalize_only_string_pairs_with_limits():
    answers = serve._choice_answers({
        "q1": "yes",
        "k" * 101: "v" * 2001,
        "number": 42,
        7: "not a string key",
    })
    assert answers == {"q1": "yes", "k" * 100: "v" * 2000}
    assert serve._choice_answers([("q1", "yes")]) == {}


def test_manager_failure_keeps_user_message_and_clears_pending():
    with isolated_desktop():
        original_router = serve.Router
        original_step = serve.manager.manager_step

        async def fail_step(*args, **kwargs):
            raise RuntimeError("offline")

        serve.Router = lambda *args, **kwargs: SimpleNamespace(all_agents=lambda: [])
        serve.manager.manager_step = fail_step
        try:
            with running_server() as port:
                code, body = _post(port, "/chat", {
                    "session_id": "a1b2c3d4e5f6", "message": "keep me"})
                assert code == 502 and "offline" in body["error"], (code, body)
                code, current = _get(port, "/chat/session/current")
            assert code == 200
            assert current["active"]["history"][-1]["text"] == "keep me"
            assert current["active"]["pending"] is False
        finally:
            serve.Router = original_router
            serve.manager.manager_step = original_step


def test_storage_failure_before_manager_call_returns_json_500():
    with isolated_desktop(), running_server() as port:
        original_router = serve.Router
        original_step = serve.manager.manager_step
        original_save = serve._session_save
        saves = []
        manager_calls = []

        def fail_user_save(sess):
            saves.append(copy.deepcopy(sess["history"]))
            if len(saves) == 2:
                raise OSError("disk full")
            return original_save(sess)

        async def fake_step(history, *args, **kwargs):
            manager_calls.append(copy.deepcopy(history))
            return {"status": "asking", "question": "Q", "questions": [],
                    "_cost_usd": 0.0}

        serve.Router = lambda *args, **kwargs: SimpleNamespace(all_agents=lambda: [])
        serve.manager.manager_step = fake_step
        serve._session_save = fail_user_save
        try:
            code, body = _post(port, "/chat", {
                "session_id": "a1b2c3d4e5f6", "message": "do not spend"})
            assert code == 500 and "disk full" in body["error"], (code, body)
            assert manager_calls == []
            saved = serve._session_load_disk("a1b2c3d4e5f6")
            assert saved["history"] == []
            code, current = _get(port, "/chat/session/current")
            assert code == 200 and current["active"]["history"] == [], current

            serve._session_save = original_save
            code, body = _post(port, "/chat", {
                "session_id": "a1b2c3d4e5f6", "message": "do not spend"})
            assert code == 200 and body["status"] == "asking", (code, body)
            assert [item["text"] for item in manager_calls[0]] == ["do not spend"]
            saved = serve._session_load_disk("a1b2c3d4e5f6")
            assert [item["text"] for item in saved["history"]] == [
                "do not spend", "Q"]
        finally:
            serve.Router = original_router
            serve.manager.manager_step = original_step
            serve._session_save = original_save


def test_storage_failure_after_manager_call_rolls_cache_back_for_retry():
    with isolated_desktop(), running_server() as port:
        sid = "a1b2c3d4e5f6"
        original_router = serve.Router
        original_step = serve.manager.manager_step
        original_save = serve._session_save
        saves = []
        manager_histories = []

        def fail_manager_save(sess):
            saves.append(copy.deepcopy(serve._session_disk_dict(sess)))
            if len(saves) == 3:
                raise OSError("post-save failed")
            return original_save(sess)

        async def fake_step(history, *args, **kwargs):
            manager_histories.append(copy.deepcopy(history))
            return {"status": "asking", "question": "Q", "questions": [],
                    "_cost_usd": 0.25}

        serve.Router = lambda *args, **kwargs: SimpleNamespace(all_agents=lambda: [])
        serve.manager.manager_step = fake_step
        serve._session_save = fail_manager_save
        try:
            code, body = _post(
                port, "/chat", {"session_id": sid, "message": "one turn"})
            assert code == 500 and "post-save failed" in body["error"], \
                (code, body)
            saved = serve._session_load_disk(sid)
            assert [item["text"] for item in saved["history"]] == ["one turn"]
            assert saved["questions"] == 0 and saved["cost_usd"] == 0.0
            code, current = _get(port, "/chat/session/current")
            assert code == 200
            assert current["active"]["history"] == saved["history"]
            assert current["active"]["questions"] == 0
            assert current["active"]["cost_usd"] == 0.0

            serve._session_save = original_save
            code, body = _post(
                port, "/chat", {"session_id": sid, "message": "one turn"})
            assert code == 200 and body["status"] == "asking", (code, body)
            assert [[item["text"] for item in history]
                    for history in manager_histories] == [
                        ["one turn"], ["one turn"]]
            saved = serve._session_load_disk(sid)
            assert [item["text"] for item in saved["history"]] == [
                "one turn", "Q"]
            assert saved["questions"] == 1 and saved["cost_usd"] == 0.25
        finally:
            serve.Router = original_router
            serve.manager.manager_step = original_step
            serve._session_save = original_save


def test_chat_returns_typed_json_errors_for_session_state():
    with isolated_desktop():
        original_router = serve.Router
        original_step = serve.manager.manager_step

        async def fake_step(*args, **kwargs):
            return {"status": "asking", "question": "Q", "questions": [],
                    "_cost_usd": 0.0}

        serve.Router = lambda *args, **kwargs: SimpleNamespace(all_agents=lambda: [])
        serve.manager.manager_step = fake_step
        try:
            with running_server() as port:
                code, body = _post(port, "/chat", {
                    "session_id": "../escape", "message": "bad sid"})
                assert code == 400 and "session" in body["error"].lower(), \
                    (code, body)

                active_sid = "a1b2c3d4e5f6"
                sess = serve._new_manager_session(active_sid)
                serve._session_save(sess)
                serve._set_active_session(active_sid)
                code, body = _post(port, "/chat", {
                    "session_id": "000000000000", "message": "stale tab"})
                assert code == 409 and \
                    body["session_id"] == "000000000000", (code, body)

                os.unlink(serve._session_path(active_sid))
                serve._sessions.clear()
                code, body = _post(port, "/chat", {
                    "session_id": active_sid, "message": "missing state"})
                assert code == 500 and "missing" in body["error"].lower(), \
                    (code, body)
        finally:
            serve.Router = original_router
            serve.manager.manager_step = original_step


def test_chat_rejects_non_object_json_without_dropping_connection():
    with isolated_desktop(), running_server() as port:
        try:
            code, body = _post(port, "/chat", [])
        except Exception as exc:
            code, body = "DROPPED", {"error": type(exc).__name__}
        assert code == 400 and "object" in body["error"].lower(), (code, body)


def test_pending_snapshot_stays_responsive_during_manager_work():
    with isolated_desktop():
        entered = threading.Event()
        release = threading.Event()
        response = {}
        original_router = serve.Router
        original_step = serve.manager.manager_step

        async def slow_step(*args, **kwargs):
            entered.set()
            release.wait(3)
            return {"status": "asking", "question": "Q", "questions": [],
                    "_cost_usd": 0.0}

        serve.Router = lambda *args, **kwargs: SimpleNamespace(all_agents=lambda: [])
        serve.manager.manager_step = slow_step
        try:
            with running_server() as port:
                first = threading.Thread(target=lambda: response.setdefault(
                    "first", _post(port, "/chat", {
                        "session_id": "a1b2c3d4e5f6", "message": "one"})))
                first.start()
                assert entered.wait(2)
                code, current = _get(port, "/chat/session/current")
                assert code == 200 and current["active"]["pending"] is True, current
                release.set()
                first.join(3)
                assert not first.is_alive()
        finally:
            release.set()
            serve.Router = original_router
            serve.manager.manager_step = original_step


def test_second_concurrent_turn_gets_409_without_calling_manager_twice():
    with isolated_desktop():
        entered = threading.Event()
        release = threading.Event()
        calls = []
        response = {}
        original_router = serve.Router
        original_step = serve.manager.manager_step

        async def slow_step(*args, **kwargs):
            calls.append(1)
            entered.set()
            release.wait(3)
            return {"status": "asking", "question": "Q", "questions": [],
                    "_cost_usd": 0.0}

        serve.Router = lambda *args, **kwargs: SimpleNamespace(all_agents=lambda: [])
        serve.manager.manager_step = slow_step
        try:
            with running_server() as port:
                first = threading.Thread(target=lambda: response.setdefault(
                    "first", _post(port, "/chat", {
                        "session_id": "a1b2c3d4e5f6", "message": "one"})))
                first.start()
                assert entered.wait(2)
                code, body = _post(port, "/chat", {
                    "session_id": "a1b2c3d4e5f6", "message": "two"})
                assert code == 409 and "progress" in body["error"], (code, body)
                release.set()
                first.join(3)
                assert not first.is_alive()
            assert calls == [1]
        finally:
            release.set()
            serve.Router = original_router
            serve.manager.manager_step = original_step


def test_blocked_concurrent_reply_does_not_hold_chat_lock():
    with isolated_desktop():
        sid = "a1b2c3d4e5f6"
        sess = serve._new_manager_session(sid)
        serve._session_save(sess)
        serve._set_active_session(sid)
        serve._chat_inflight.add(sid)
        reply_entered = threading.Event()
        release_reply = threading.Event()
        lock_owned_during_reply = []
        results = {}

        class BlockingReplyHandler(serve.Handler):
            def _reply(self, code, obj):
                if code == 409 and obj.get("error") == \
                        "another manager turn is still in progress":
                    lock_owned_during_reply.append(
                        serve._chat_lock._is_owned())
                    reply_entered.set()
                    if not release_reply.wait(3):
                        raise AssertionError("blocked reply was not released")
                return super()._reply(code, obj)

        def request(name, method, port, path, body=None):
            try:
                if method == "GET":
                    results[name] = _get(port, path)
                else:
                    results[name] = _post(port, path, body)
            except BaseException as exc:
                results[name] = exc

        try:
            with running_server(BlockingReplyHandler) as port:
                reject = threading.Thread(target=request, args=(
                    "reject", "POST", port, "/chat",
                    {"session_id": sid, "message": "second"}))
                reject.start()
                assert reply_entered.wait(2), "concurrent 409 reply never blocked"

                current = threading.Thread(target=request, args=(
                    "current", "GET", port, "/chat/session/current"))
                discard = threading.Thread(target=request, args=(
                    "discard", "POST", port, "/chat/session/discard",
                    {"session_id": sid}))
                current.start()
                discard.start()
                current.join(1)
                discard.join(1)
                operations_finished_while_reply_blocked = (
                    not current.is_alive() and not discard.is_alive())

                release_reply.set()
                reject.join(3)
                current.join(3)
                discard.join(3)
                assert not reject.is_alive()
                assert not current.is_alive() and not discard.is_alive()
        finally:
            release_reply.set()

        assert lock_owned_during_reply == [False]
        assert operations_finished_while_reply_blocked
        assert results["current"][0] == 200
        assert results["discard"][0] == 409
        assert results["reject"][0] == 409


def test_lock_contention_reply_runs_after_failed_acquire():
    with isolated_desktop():
        lock_held = threading.Event()
        release_lock = threading.Event()
        replies = []
        original_current = serve._current

        class ReplyProbe:
            def _reply(self, code, obj):
                replies.append((code, obj, serve._chat_lock._is_owned()))

        def hold_lock():
            with serve._chat_lock:
                lock_held.set()
                if not release_lock.wait(3):
                    raise AssertionError("lock holder was not released")

        serve._current = None
        holder = threading.Thread(target=hold_lock)
        holder.start()
        try:
            assert lock_held.wait(2)
            serve.Handler._handle_chat(ReplyProbe(), {
                "session_id": "a1b2c3d4e5f6", "message": "blocked"})
        finally:
            release_lock.set()
            holder.join(3)
            serve._current = original_current
        assert replies == [(409, {
            "error": "another manager turn is still in progress"}, False)]


def _ready_session(sid: str, now: float = 1000.0) -> dict:
    sess = serve._new_manager_session(sid, now=now)
    sess.update({
        "awaiting_confirmation": True,
        "proposal": {"goal": "G", "constraints": "C"},
        "history": [{"role": "manager", "text": "proposal", "ts": now}],
    })
    return sess


def _started_session(sid: str, run_id: str, now: float = 1000.0) -> dict:
    sess = _ready_session(sid, now=now)
    sess.update({"status": "started", "run_id": run_id})
    return sess


def _write_session_snapshot(sess: dict) -> None:
    serve._atomic_write(
        serve._session_path(sess["session_id"]),
        json.dumps(serve._session_disk_dict(sess)), mode=0o600)


def test_started_session_moves_into_spawned_run_after_durable_archive():
    with isolated_desktop():
        sid = "a1b2c3d4e5f6"
        sess = _ready_session(sid)
        serve._session_save(sess)
        serve._set_active_session(sid)
        serve._sessions[sid] = sess
        run_id = "0a1b2c3d"
        os.makedirs(os.path.join(serve.RUNS_DIR, run_id))
        serve._run_meta = {"id": run_id}

        warning = serve._archive_manager_session(sess, run_id)

        assert warning == ""
        archive_path = os.path.join(
            serve.RUNS_DIR, run_id, "manager-session.json")
        with open(archive_path, encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["status"] == "started" and saved["run_id"] == run_id
        assert saved["history"][-1]["text"] == "收到，马上安排开工。"
        if os.name != 'nt':
            assert stat.S_IMODE(os.stat(archive_path).st_mode) == 0o600
        assert serve._active_session_id() == ""
        assert not os.path.exists(serve._session_path(sid))
        assert sid not in serve._sessions


def test_archive_failure_keeps_started_fallback_and_blocks_second_spawn():
    with isolated_desktop():
        sid = "a1b2c3d4e5f6"
        run_id = "0a1b2c3d"
        sess = _ready_session(sid)
        serve._session_save(sess)
        serve._set_active_session(sid)
        serve._sessions[sid] = sess
        spawn_calls = []
        original_spawn = serve._spawn_manager_main_if_idle
        original_atomic = serve._atomic_write

        def fake_spawn(argv):
            spawn_calls.append(argv)
            os.makedirs(os.path.join(serve.RUNS_DIR, run_id), exist_ok=True)
            serve._run_meta = {"id": run_id}
            return 4242, run_id

        def fail_archive(path, text, mode=0o600):
            if path.endswith("manager-session.json"):
                raise OSError("read-only run dir")
            return original_atomic(path, text, mode)

        serve._spawn_manager_main_if_idle = fake_spawn
        serve._atomic_write = fail_archive
        try:
            with running_server() as port:
                code, first = _post(
                    port, "/chat", {"session_id": sid, "message": "对"})
                code2, second = _post(
                    port, "/chat", {"session_id": sid, "message": "对"})
        finally:
            serve._spawn_manager_main_if_idle = original_spawn
            serve._atomic_write = original_atomic

        assert code == 200 and first["status"] == "started", (code, first)
        assert "read-only run dir" in first["persistence_warning"]
        assert code2 == 409 and "already started" in second["error"], \
            (code2, second)
        assert len(spawn_calls) == 1
        fallback = serve._session_load_disk(sid)
        assert fallback["status"] == "started" and fallback["run_id"] == run_id
        assert [item["text"] for item in fallback["history"]].count(
            "收到，马上安排开工。") == 1
        assert serve._active_session_id() == sid


def test_repeated_archive_is_idempotent():
    with isolated_desktop():
        sid = "a1b2c3d4e5f6"
        run_id = "0a1b2c3d"
        sess = _ready_session(sid)
        serve._session_save(sess)
        serve._set_active_session(sid)
        serve._sessions[sid] = sess
        os.makedirs(os.path.join(serve.RUNS_DIR, run_id))
        serve._run_meta = {"id": run_id}

        assert serve._archive_manager_session(sess, run_id) == ""

        archive_path = os.path.join(
            serve.RUNS_DIR, run_id, "manager-session.json")
        with open(archive_path, "rb") as f:
            first_archive = f.read()
        first_updated_at = sess["updated_at"]

        assert serve._archive_manager_session(sess, run_id) == ""

        with open(archive_path, "rb") as f:
            assert f.read() == first_archive, \
                "an identical retry must preserve the existing archive"
        assert sess["updated_at"] == first_updated_at, \
            "an identical retry must not create a false timestamp conflict"
        with open(archive_path, encoding="utf-8") as f:
            saved = json.load(f)
        assert [item["text"] for item in saved["history"]].count(
            "收到，马上安排开工。") == 1
        assert serve._active_session_id() == ""
        assert not os.path.exists(serve._session_path(sid))


def test_valid_same_id_archive_conflict_preserves_archive_and_fallback():
    with isolated_desktop():
        sid = "a1b2c3d4e5f6"
        run_id = "0a1b2c3d"
        sess = _started_session(sid, run_id, now=1000.0)
        _write_session_snapshot(sess)
        serve._set_active_session(sid)
        serve._sessions[sid] = sess
        run_dir = os.path.join(serve.RUNS_DIR, run_id)
        os.makedirs(run_dir)
        existing = _started_session(sid, run_id, now=900.0)
        existing["history"] = [
            {"role": "manager", "text": "ORIGINAL EVIDENCE", "ts": 900.0}
        ]
        archive_path = os.path.join(run_dir, "manager-session.json")
        original_payload = serve._session_disk_dict(existing)
        serve._atomic_write(archive_path, json.dumps(
            original_payload, ensure_ascii=False, indent=2), mode=0o600)

        try:
            serve._write_run_manager_archive(sess, run_id)
        except serve.SessionDataError as exc:
            assert "conflict" in str(exc).lower(), str(exc)
        else:
            raise AssertionError("a valid conflicting archive was overwritten")

        with open(archive_path, encoding="utf-8") as f:
            assert json.load(f) == original_payload
        fallback = serve._session_load_disk(sid)
        assert fallback["status"] == "started"
        assert fallback["run_id"] == run_id
        assert serve._active_session_id() == sid
        assert sid in serve._sessions


def test_identical_valid_archive_is_preserved_byte_for_byte():
    with isolated_desktop():
        sid = "a1b2c3d4e5f6"
        run_id = "0a1b2c3d"
        sess = _started_session(sid, run_id, now=1000.0)
        run_dir = os.path.join(serve.RUNS_DIR, run_id)
        os.makedirs(run_dir)
        archive_path = os.path.join(run_dir, "manager-session.json")
        original = json.dumps(
            serve._session_disk_dict(sess), ensure_ascii=False,
            separators=(",", ":")).encode()
        with open(archive_path, "wb") as f:
            f.write(original)

        serve._write_run_manager_archive(sess, run_id)

        with open(archive_path, "rb") as f:
            assert f.read() == original


def test_manager_run_id_requires_exactly_eight_lower_hex_characters():
    with isolated_desktop():
        sess = _started_session("a1b2c3d4e5f6", "deadbeef")
        invalid_ids = (
            "deadbee", "deadbeef0", "DEADBEEF", "deadbeeG",
            "../beef0", "dead-bee", "１２３４５６７８",
        )
        before = sorted(os.listdir(serve.RUNS_DIR))
        for run_id in invalid_ids:
            try:
                serve._write_run_manager_archive(sess, run_id)
            except serve.SessionDataError as exc:
                assert "run id" in str(exc)
            else:
                raise AssertionError(f"invalid run id accepted: {run_id!r}")
        assert sorted(os.listdir(serve.RUNS_DIR)) == before

        os.makedirs(os.path.join(serve.RUNS_DIR, "deadbeef"))
        serve._write_run_manager_archive(sess, "deadbeef")
        assert os.path.isfile(os.path.join(
            serve.RUNS_DIR, "deadbeef", "manager-session.json"))


def test_archive_writer_rejects_symlink_escapes():
    with isolated_desktop() as td:
        sess = _started_session("a1b2c3d4e5f6", "deadbeef")
        outside_dir = os.path.join(td, "outside-run")
        os.makedirs(outside_dir)
        _symlink(outside_dir, os.path.join(serve.RUNS_DIR, "deadbeef"))
        try:
            serve._write_run_manager_archive(sess, "deadbeef")
        except serve.SessionDataError as exc:
            assert "outside" in str(exc).lower() or "symlink" in str(exc).lower()
        else:
            raise AssertionError("symlinked run directory was accepted")
        assert os.listdir(outside_dir) == []

        os.unlink(os.path.join(serve.RUNS_DIR, "deadbeef"))
        run_dir = os.path.join(serve.RUNS_DIR, "deadbeef")
        os.makedirs(run_dir)
        outside_archive = os.path.join(td, "outside-manager-session.json")
        with open(outside_archive, "w", encoding="utf-8") as f:
            f.write("evidence")
        _symlink(outside_archive,
                   os.path.join(run_dir, "manager-session.json"))
        try:
            serve._write_run_manager_archive(sess, "deadbeef")
        except serve.SessionDataError as exc:
            assert "outside" in str(exc).lower() or "symlink" in str(exc).lower()
        else:
            raise AssertionError("symlinked archive was accepted")
        with open(outside_archive, encoding="utf-8") as f:
            assert f.read() == "evidence"


def test_confirm_keeps_active_state_until_spawn_succeeds():
    with isolated_desktop():
        sid = "a1b2c3d4e5f6"
        sess = _ready_session(sid)
        serve._session_save(sess)
        serve._set_active_session(sid)
        serve._sessions[sid] = sess
        original_spawn = serve._spawn_manager_main_if_idle
        serve._spawn_manager_main_if_idle = lambda _argv: (None, "")
        try:
            with running_server() as port:
                code, body = _post(
                    port, "/chat", {"session_id": sid, "message": "对"})
        finally:
            serve._spawn_manager_main_if_idle = original_spawn

        assert code == 409, (code, body)
        saved = serve._session_load_disk(sid)
        assert saved["status"] == "active" and saved["run_id"] == ""
        assert all(item["text"] != "收到，马上安排开工。"
                   for item in saved["history"])
        assert serve._active_session_id() == sid


def test_successful_confirm_archives_session_after_spawn():
    with isolated_desktop():
        sid = "a1b2c3d4e5f6"
        run_id = "0a1b2c3d"
        sess = _ready_session(sid)
        serve._session_save(sess)
        serve._set_active_session(sid)
        serve._sessions[sid] = sess
        original_spawn = serve._spawn_manager_main_if_idle

        def fake_spawn(_argv):
            os.makedirs(os.path.join(serve.RUNS_DIR, run_id))
            serve._run_meta = {"id": run_id}
            return 4242, run_id

        serve._spawn_manager_main_if_idle = fake_spawn
        try:
            with running_server() as port:
                code, body = _post(
                    port, "/chat", {"session_id": sid, "message": "对"})
        finally:
            serve._spawn_manager_main_if_idle = original_spawn

        assert code == 200 and body["status"] == "started", (code, body)
        assert "persistence_warning" not in body
        with open(os.path.join(serve.RUNS_DIR, run_id,
                               "manager-session.json"), encoding="utf-8") as f:
            archive = json.load(f)
        assert archive["status"] == "started" and archive["run_id"] == run_id
        assert archive["history"][-1]["text"] == "收到，马上安排开工。"
        assert serve._active_session_id() == ""
        assert not os.path.exists(serve._session_path(sid))


def test_confirm_archives_to_captured_run_when_global_run_changes():
    with isolated_desktop():
        sid = "a1b2c3d4e5f6"
        spawned_run_id = "aaaaaaaa"
        competing_run_id = "bbbbbbbb"
        sess = _ready_session(sid)
        serve._session_save(sess)
        serve._set_active_session(sid)
        serve._sessions[sid] = sess
        original_spawn = serve._spawn_main
        original_archive = serve._archive_manager_session

        def fake_spawn(_argv):
            os.makedirs(os.path.join(serve.RUNS_DIR, spawned_run_id),
                        exist_ok=True)
            serve._run_meta = {"id": spawned_run_id}
            return 1111

        def interleaving_archive(session, captured_run_id=None):
            os.makedirs(os.path.join(serve.RUNS_DIR, competing_run_id),
                        exist_ok=True)
            serve._run_meta = {"id": competing_run_id}
            if captured_run_id is None:  # pre-fix call shape for RED evidence
                return original_archive(session)
            return original_archive(session, captured_run_id)

        serve._spawn_main = fake_spawn
        serve._archive_manager_session = interleaving_archive
        try:
            with running_server() as port:
                code, body = _post(
                    port, "/chat", {"session_id": sid, "message": "对"})
        finally:
            serve._spawn_main = original_spawn
            serve._archive_manager_session = original_archive

        assert code == 200 and body["pid"] == 1111, (code, body)
        assert os.path.isfile(os.path.join(
            serve.RUNS_DIR, spawned_run_id, "manager-session.json"))
        assert not os.path.exists(os.path.join(
            serve.RUNS_DIR, competing_run_id, "manager-session.json"))
        with open(os.path.join(serve.RUNS_DIR, spawned_run_id,
                               "manager-session.json"), encoding="utf-8") as f:
            archive = json.load(f)
        assert archive["run_id"] == spawned_run_id


def test_reconcile_recovers_started_fallback_and_is_idempotent():
    with isolated_desktop():
        sid = "a1b2c3d4e5f6"
        run_id = "0a1b2c3d"
        sess = _started_session(sid, run_id)
        _write_session_snapshot(sess)
        serve._set_active_session(sid)
        os.makedirs(os.path.join(serve.RUNS_DIR, run_id))

        serve._reconcile_manager_sessions(now=2000.0)
        serve._reconcile_manager_sessions(now=2000.0)

        archive_path = os.path.join(
            serve.RUNS_DIR, run_id, "manager-session.json")
        with open(archive_path, encoding="utf-8") as f:
            archive = json.load(f)
        assert archive == serve._session_disk_dict(sess)
        assert not os.path.exists(serve._session_path(sid))
        assert serve._active_session_id() == ""


def test_reconcile_preserves_corrupt_session_and_active_pointer():
    with isolated_desktop():
        sid = "a1b2c3d4e5f6"
        path = serve._session_path(sid)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{broken evidence")
        serve._set_active_session(sid)

        serve._reconcile_manager_sessions(now=10_000_000.0)

        with open(path, encoding="utf-8") as f:
            assert f.read() == "{broken evidence"
        assert serve._active_session_id() == sid


def test_reconcile_does_not_overwrite_corrupt_run_archive():
    with isolated_desktop():
        sid = "a1b2c3d4e5f6"
        run_id = "0a1b2c3d"
        sess = _started_session(sid, run_id)
        _write_session_snapshot(sess)
        serve._set_active_session(sid)
        run_dir = os.path.join(serve.RUNS_DIR, run_id)
        os.makedirs(run_dir)
        archive_path = os.path.join(run_dir, "manager-session.json")
        with open(archive_path, "w", encoding="utf-8") as f:
            f.write("{corrupt archive evidence")

        serve._reconcile_manager_sessions(now=2000.0)

        with open(archive_path, encoding="utf-8") as f:
            assert f.read() == "{corrupt archive evidence"
        assert os.path.exists(serve._session_path(sid))
        assert serve._active_session_id() == sid


def test_reconcile_archives_fallback_without_clearing_new_active_pointer():
    with isolated_desktop():
        started_sid = "111111111111"
        active_sid = "222222222222"
        run_id = "0a1b2c3d"
        started = _started_session(started_sid, run_id)
        active = serve._new_manager_session(active_sid, now=1900.0)
        _write_session_snapshot(started)
        _write_session_snapshot(active)
        serve._sessions[active_sid] = active
        serve._set_active_session(active_sid)
        os.makedirs(os.path.join(serve.RUNS_DIR, run_id))

        serve._reconcile_manager_sessions(now=2000.0)

        assert os.path.exists(os.path.join(
            serve.RUNS_DIR, run_id, "manager-session.json"))
        assert not os.path.exists(serve._session_path(started_sid))
        assert os.path.exists(serve._session_path(active_sid))
        assert serve._active_session_id() == active_sid
        assert serve._sessions[active_sid]["status"] == "active"


def test_reconcile_expires_only_drafts_older_than_30_days_with_injected_time():
    with isolated_desktop():
        expired_sid = "111111111111"
        boundary_sid = "222222222222"
        started_sid = "333333333333"
        run_id = "0a1b2c3d"
        expired = serve._new_manager_session(expired_sid, now=99.0)
        boundary = serve._new_manager_session(boundary_sid, now=100.0)
        started = _started_session(started_sid, run_id, now=1.0)
        for sess in (expired, boundary, started):
            _write_session_snapshot(sess)
        serve._sessions.update({
            expired_sid: expired, boundary_sid: boundary, started_sid: started})
        serve._set_active_session(expired_sid)
        os.makedirs(os.path.join(serve.RUNS_DIR, run_id))
        unknown_path = os.path.join(serve.SESSIONS_DIR, "notes.json")
        with open(unknown_path, "w", encoding="utf-8") as f:
            f.write("keep")

        serve._reconcile_manager_sessions(
            now=100.0 + serve.SESSION_TTL_SEC)

        assert not os.path.exists(serve._session_path(expired_sid))
        assert expired_sid not in serve._sessions
        assert os.path.exists(serve._session_path(boundary_sid)), \
            "a draft exactly 30 days old must not expire"
        assert boundary_sid in serve._sessions
        assert not os.path.exists(serve._session_path(started_sid))
        assert os.path.exists(os.path.join(
            serve.RUNS_DIR, run_id, "manager-session.json"))
        assert os.path.exists(unknown_path)
        assert serve._active_session_id() == ""


def test_reconcile_preserves_symlinked_session_evidence():
    with isolated_desktop() as td:
        sid = "a1b2c3d4e5f6"
        outside_path = os.path.join(td, "outside-session.json")
        outside = serve._new_manager_session(sid, now=1.0)
        with open(outside_path, "w", encoding="utf-8") as f:
            json.dump(serve._session_disk_dict(outside), f)
        session_path = serve._session_path(sid)
        _symlink(outside_path, session_path)
        serve._set_active_session(sid)

        serve._reconcile_manager_sessions(
            now=1.0 + serve.SESSION_TTL_SEC + 1)

        assert os.path.islink(session_path)
        with open(outside_path, encoding="utf-8") as f:
            assert json.load(f)["status"] == "active"
        assert serve._active_session_id() == sid


def test_archive_and_reconcile_legacy_helpers_never_write_cwd():
    with isolated_desktop() as td:
        sid = "a1b2c3d4e5f6"
        cwd = os.path.join(td, "legacy-cwd")
        os.makedirs(cwd)
        previous_cwd = os.getcwd()
        serve.RUNS_DIR = ""
        serve.SESSIONS_DIR = ""
        serve.ACTIVE_SESSION_PATH = ""
        serve._active_sid = sid
        serve._run_meta = {}
        serve._sessions[sid] = _ready_session(sid)
        try:
            os.chdir(cwd)
            assert serve._archive_manager_session(serve._sessions[sid], "") == ""
            serve._reconcile_manager_sessions(now=10_000_000.0)
            assert os.listdir(cwd) == []
        finally:
            os.chdir(previous_cwd)
        assert serve._active_session_id() == ""
        assert sid not in serve._sessions



def test_full_http_chain_survives_server_restart():
    """Disk -> HTTP -> dashboard snapshot -> resumed manager, across a cold start.

    Tasks 1-6 each proved one link on its own.  This is the only test that walks
    the whole chain over a real socket on both sides of a restart, so it is the
    one that catches a seam that works alone but not in series.
    """
    with isolated_desktop() as td:
        sid = "a1b2c3d4e5f6"
        original_router = serve.Router
        original_step = serve.manager.manager_step
        histories = []

        async def fake_step(history, router, workdir,
                            force_finish=False, on_event=None):
            histories.append([item["text"] for item in history])
            if len(histories) == 1:
                return {"status": "asking", "question": "给谁用？",
                        "questions": [{"id": "q1", "question": "给谁用？",
                                       "options": [{"label": "财务", "detail": "",
                                                    "other": False}]}],
                        "_cost_usd": 0.1}
            return {"status": "ready", "goal": "财务报表",
                    "constraints": "财务团队使用", "_cost_usd": 0.2}

        serve.Router = lambda *args, **kwargs: SimpleNamespace(all_agents=lambda: [])
        serve.manager.manager_step = fake_step
        try:
            with running_server() as first_port:
                code, first = _post(first_port, "/chat",
                                    {"session_id": sid, "message": "做报表"})
                assert code == 200 and first["status"] == "asking", first

            # A restart loses every in-process cache.  _configure() is what the
            # real process runs on boot, so re-running it (rather than poking
            # the globals by hand) also exercises startup reconcile.
            serve._sessions.clear()
            serve._progress.clear()
            serve._chat_inflight.clear()
            serve._active_sid = ""
            serve._run_meta = {}
            serve._configure(td, "persist-test", 8787)
            assert sid not in serve._sessions, \
                "the restart simulation must start from a cold cache"

            with running_server() as second_port:
                code, restored = _get(second_port, "/chat/session/current")
                assert code == 200, restored
                active = restored["active"]
                assert active["session_id"] == sid, active
                assert [item["text"] for item in active["history"]] == \
                    ["做报表", "给谁用？"], active["history"]
                assert active["pending"] is False
                # The unanswered question comes back as an open choice card.
                assert active["history"][-1]["answered"] is False
                assert active["history"][-1]["questions"][0]["id"] == "q1"

                code, second = _post(second_port, "/chat", {
                    "session_id": sid, "message": "给谁用？：财务",
                    "choice_answers": {"q1": "财务"}})
                assert code == 200 and second["status"] == "confirm", second

            # The second manager call saw the whole conversation, rebuilt from
            # disk by a server that never handled the first turn.
            assert histories[1] == ["做报表", "给谁用？", "给谁用？：财务"], histories
            assert abs(second["manager_cost_usd"] - 0.3) < 1e-9, second

            # The answered card is frozen on disk, so a later refresh renders it
            # answered instead of reopening a question the user already closed.
            with open(serve._session_path(sid), encoding="utf-8") as f:
                on_disk = json.load(f)
            question_turn = on_disk["history"][1]
            assert question_turn["answered"] is True, question_turn
            assert question_turn["answers"] == {"q1": "财务"}, question_turn
        finally:
            serve.Router = original_router
            serve.manager.manager_step = original_step



if __name__ == "__main__":
    skipped = 0
    for name in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {name} ==")
        try:
            globals()[name]()
        except SkipTest as exc:
            skipped += 1
            print(f'SKIP {name}: {exc}')
    print(f"\nManager-session persistence tests passed; {skipped} environment skips.")
