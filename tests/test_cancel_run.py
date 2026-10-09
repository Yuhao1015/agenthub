"""POST /cancel: kill the current run's WHOLE process group, not just
main.py's own pid -- a plain proc.terminate() would silently orphan any
still-billing claude/dsh/codebuddy child subprocess (see
docs/superpowers/specs/2026-08-16-cancel-run-design.md). The core
correctness property under test is that a GRANDCHILD subprocess (the
thing that simulates `claude -p`) actually stops running too, not just
that the HTTP call returns 200.

No framework, plain asserts. Zero real LLM calls.

Run: python3 tests/test_cancel_run.py
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
from test_gate_serve import authorize_local_agents

_GLOBALS = ("PORT", "MATRIX", "BOARD_PATH", "EVENTS_PATH", "WORKDIR", "DATA_DIR",
            "PROJECT", "RUNTIME_DIR", "RUNS_DIR", "INDEX_PATH", "CONFIG_PATH",
            "MERGED_MATRIX_PATH", "PREFLIGHT_PATH", "CREDENTIALS_PATH",
            "_current", "_run_meta", "MAIN_PATH")


def _snap():
    return {"_runs": list(serve._runs), "_run_meta": dict(serve._run_meta),
            **{k: getattr(serve, k) for k in _GLOBALS}}


def _restore(s):
    for k, v in s.items():
        if k == "_runs":
            serve._runs = list(v)
        elif k == "_run_meta":
            serve._run_meta = dict(v)
        else:
            setattr(serve, k, v)


def _post(port, path, obj):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path, json.dumps(obj), {"Content-Type": "application/json"})
    r = conn.getresponse()
    data = json.loads(r.read() or b"{}")
    conn.close()
    return r.status, data


def _wait_until(predicate, timeout=8.0, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


_CHILD_SRC = """
import os, signal, time
hb = os.environ["AH_TEST_HEARTBEAT_DIR"]
if os.name != "nt":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
deadline = time.time() + 30
while time.time() < deadline:
    with open(os.path.join(hb, "child.beat"), "w") as f:
        f.write(str(time.time()))
    time.sleep(0.15)
"""

_PARENT_SRC = """
import os, subprocess, sys, time
hb = os.environ["AH_TEST_HEARTBEAT_DIR"]
child = subprocess.Popen([sys.executable, os.path.join(hb, "child.py")])
with open(os.path.join(hb, "child.pid"), "w") as f:
    f.write(str(child.pid))
deadline = time.time() + 30
while time.time() < deadline:
    with open(os.path.join(hb, "parent.beat"), "w") as f:
        f.write(str(time.time()))
    time.sleep(0.15)
"""

_DETACHED_PARENT_SRC = """
import os, subprocess, sys, time
hb = os.environ["AH_TEST_HEARTBEAT_DIR"]
kwargs = ({"start_new_session": True} if os.name != "nt" else
          {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP})
child = subprocess.Popen([sys.executable, os.path.join(hb, "child.py")], **kwargs)
with open(os.path.join(hb, "child.pid"), "w") as f:
    f.write(str(child.pid))
deadline = time.time() + 30
while time.time() < deadline:
    with open(os.path.join(hb, "parent.beat"), "w") as f:
        f.write(str(time.time()))
    time.sleep(0.15)
"""


def _beat_mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def test_cancel_kills_whole_process_tree():
    """The core property: cancelling must stop the GRANDCHILD process
    (the stand-in for `claude -p`), not just main.py itself."""
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_cancel_")
    hb = tempfile.mkdtemp(prefix="ah_cancel_hb_")
    orig_env = os.environ.get("AH_TEST_HEARTBEAT_DIR")
    os.environ["AH_TEST_HEARTBEAT_DIR"] = hb
    with open(os.path.join(hb, "child.py"), "w", encoding="utf-8") as f:
        f.write(_CHILD_SRC)
    parent_script = os.path.join(hb, "parent.py")
    with open(parent_script, "w", encoding="utf-8") as f:
        f.write(_PARENT_SRC)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        serve._configure(tmp, "default", port)
        authorize_local_agents()
        serve.MAIN_PATH = parent_script

        pid = serve._spawn_main(["--goal=cancel-test"])
        assert pid > 0
        if os.name != "nt":
            assert os.getpgid(pid) != os.getpgid(os.getpid()), \
                "child must be in its own process group, not serve.py's"

        # wait for the grandchild to actually be up and beating
        assert _wait_until(lambda: _beat_mtime(os.path.join(hb, "child.beat")) is not None), \
            "grandchild never started"

        code, data = _post(port, "/cancel", {})
        assert code == 200 and data.get("ok") is True, (code, data)

        # cancel_flag must be set synchronously, before the watchdog thread
        # even has a chance to observe proc.wait() returning
        assert serve._run_meta["cancel_flag"]["cancel_requested"] is True

        # the grandchild's heartbeat must stop advancing -- proof it was
        # actually killed, not just orphaned and left running
        m1 = _beat_mtime(os.path.join(hb, "child.beat"))
        assert m1 is not None
        time.sleep(1.5)
        m2 = _beat_mtime(os.path.join(hb, "child.beat"))
        time.sleep(1.5)
        m3 = _beat_mtime(os.path.join(hb, "child.beat"))
        assert m2 == m3, f"grandchild still running after cancel: {m1} -> {m2} -> {m3}"

        # watchdog thread should have recorded this as cancelled, not crashed
        assert _wait_until(lambda: (serve._runs_load(), serve._runs and
                                     serve._runs[-1].get("status") == "cancelled")[1])
        text = open(serve.EVENTS_PATH, encoding="utf-8").read()
        assert '"type": "run_cancelled"' in text, text
        assert '"type": "run_crashed"' not in text, text
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(hb, ignore_errors=True)
        if orig_env is None:
            os.environ.pop("AH_TEST_HEARTBEAT_DIR", None)
        else:
            os.environ["AH_TEST_HEARTBEAT_DIR"] = orig_env


def test_cancel_kills_descendant_that_started_its_own_process_group():
    """CodexExecutor starts a dedicated process group so its own timeout can
    kill tool children.  POST /cancel must discover and terminate that group
    too; killing only main.py's group would orphan it."""
    if os.name == "nt":
        return  # Windows tree termination uses taskkill /T; covered by argv unit tests.
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_cancel_detached_")
    hb = tempfile.mkdtemp(prefix="ah_cancel_detached_hb_")
    orig_env = os.environ.get("AH_TEST_HEARTBEAT_DIR")
    os.environ["AH_TEST_HEARTBEAT_DIR"] = hb
    with open(os.path.join(hb, "child.py"), "w", encoding="utf-8") as f:
        f.write(_CHILD_SRC)
    parent_script = os.path.join(hb, "parent.py")
    with open(parent_script, "w", encoding="utf-8") as f:
        f.write(_DETACHED_PARENT_SRC)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    child_pid = None
    try:
        serve._configure(tmp, "default", port)
        authorize_local_agents()
        serve.MAIN_PATH = parent_script
        serve._spawn_main(["--goal=cancel-detached-test"])
        pid_path = os.path.join(hb, "child.pid")
        assert _wait_until(lambda: os.path.exists(pid_path))
        child_pid = int(open(pid_path, encoding="utf-8").read())
        assert os.getpgid(child_pid) == child_pid, "fixture child must own its process group"
        assert _wait_until(lambda: _beat_mtime(os.path.join(hb, "child.beat")) is not None)

        code, data = _post(port, "/cancel", {})
        assert code == 200 and data.get("ok") is True
        time.sleep(0.8)
        m1 = _beat_mtime(os.path.join(hb, "child.beat"))
        time.sleep(1.0)
        m2 = _beat_mtime(os.path.join(hb, "child.beat"))
        assert m1 == m2, f"detached descendant still running: {m1} -> {m2}"
    finally:
        if child_pid is not None:
            try:
                os.killpg(child_pid, 9)
            except (ProcessLookupError, PermissionError):
                pass
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(hb, ignore_errors=True)
        if orig_env is None:
            os.environ.pop("AH_TEST_HEARTBEAT_DIR", None)
        else:
            os.environ["AH_TEST_HEARTBEAT_DIR"] = orig_env


def test_cancel_without_running_returns_409():
    orig_current = serve._current
    serve._current = None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, data = _post(port, "/cancel", {})
        assert code == 409, (code, data)
    finally:
        srv.shutdown()
        serve._current = orig_current


def test_windows_cancel_does_not_hide_failed_taskkill():
    original_name = serve.os.name
    original_run = serve.subprocess.run
    had_ctrl = hasattr(serve.signal, "CTRL_BREAK_EVENT")
    original_ctrl = getattr(serve.signal, "CTRL_BREAK_EVENT", None)
    calls = []

    class Proc:
        pid = 5151
        signaled = False

        def poll(self): return None
        def send_signal(self, _sig): self.signaled = True

    try:
        serve.os.name = "nt"
        serve.signal.CTRL_BREAK_EVENT = 1
        serve.subprocess.run = lambda argv, **kwargs: (
            calls.append(list(argv)) or types.SimpleNamespace(returncode=1))
        proc = Proc()
        assert serve._terminate_current_process_tree(proc) is False
        assert proc.signaled is True
    finally:
        serve.os.name = original_name
        serve.subprocess.run = original_run
        if had_ctrl:
            serve.signal.CTRL_BREAK_EVENT = original_ctrl
        else:
            delattr(serve.signal, "CTRL_BREAK_EVENT")
    assert calls == [["taskkill", "/PID", "5151", "/T", "/F"]]


class _FakeProc:
    def __init__(self, returncode):
        self.returncode = returncode

    def wait(self):
        return self.returncode


def test_watch_run_cancel_flag_takes_priority_over_crash_detection():
    """Same exit shape as an unrequested crash (nonzero, no run_finished)
    -- only cancel_flag distinguishes "you asked for this" from "this
    actually crashed"."""
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_watchcancel_")
    try:
        serve._configure(tmp, "default", 8787)
        authorize_local_agents()
        serve._runs_add("r1", "cancel-me")
        events = os.path.join(serve.RUNS_DIR, "r1", "events.jsonl")
        os.makedirs(os.path.dirname(events), exist_ok=True)
        with open(events, "w", encoding="utf-8") as f:
            f.write("")   # no run_finished -- same shape as a real crash
        serve.EVENTS_PATH = events
        serve._watch_run("r1", _FakeProc(-15), events, {"cancel_requested": True})
        serve._runs_load()
        assert serve._runs[0]["status"] == "cancelled", serve._runs
        text = open(events, encoding="utf-8").read()
        assert '"type": "run_cancelled"' in text, text
        assert '"type": "run_crashed"' not in text, text
    finally:
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_watch_run_does_not_duplicate_engine_run_cancelled():
    """Bug B4 follow-up: a gate cancel both writes the reply (the engine may
    see it and emit run_cancelled itself) and kills the tree (the watchdog
    then records the cancellation). The stream must say it once, or the
    chat shows the cancellation message twice."""
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_watchdup_")
    try:
        serve._configure(tmp, "default", 8787)
        authorize_local_agents()
        serve._runs_add("r1", "cancel-me")
        events = os.path.join(serve.RUNS_DIR, "r1", "events.jsonl")
        os.makedirs(os.path.dirname(events), exist_ok=True)
        with open(events, "w", encoding="utf-8") as f:
            f.write('{"ts": 1, "id": "a", "type": "run_cancelled", '
                    '"reason": "task gate t1 rejected"}\n')
        serve.EVENTS_PATH = events
        serve._watch_run("r1", _FakeProc(-15), events, {"cancel_requested": True})
        serve._runs_load()
        assert serve._runs[0]["status"] == "cancelled", serve._runs
        text = open(events, encoding="utf-8").read()
        assert text.count('"type": "run_cancelled"') == 1, text
    finally:
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_watch_run_keeps_the_engines_own_crash_reason():
    """Bug B11: when the engine already wrote run_crashed with the real
    reason (e.g. planning failed) before exiting nonzero, the watchdog must
    not append its generic "exited with code 1" -- the dashboard would show
    that second, less useful message too."""
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_watchcrash2_")
    try:
        serve._configure(tmp, "default", 8787)
        authorize_local_agents()
        serve._runs_add("r1", "boom")
        events = os.path.join(serve.RUNS_DIR, "r1", "events.jsonl")
        os.makedirs(os.path.dirname(events), exist_ok=True)
        with open(events, "w", encoding="utf-8") as f:
            f.write('{"ts": 1, "id": "a", "type": "run_crashed", '
                    '"error": "拆解任务失败：planning failed: bad json"}\n')
        serve.EVENTS_PATH = events
        serve._watch_run("r1", _FakeProc(1), events, {"cancel_requested": False})
        serve._runs_load()
        assert serve._runs[0]["status"] == "crashed", serve._runs
        text = open(events, encoding="utf-8").read()
        assert text.count('"type": "run_crashed"') == 1, text
    finally:
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_rerun_attempt_still_gets_its_own_terminal_event():
    """Guard for the dedupe above: POST /rerun appends to the SAME
    events.jsonl, so the previous attempt's run_cancelled / run_crashed /
    run_finished sit in the tail. None of them may stand in for this
    attempt's own ending, or the dashboard keeps showing the rerun as
    running forever (the run_finished case was wrong even before the
    dedupe: a crashed rerun was recorded with attempt 1's verdict)."""
    import threading
    import time

    # (how attempt 1 ended, attempt 2 cancelled?, attempt 2 exit, index status,
    #  event attempt 2 must add, expected count of that event type)
    cases = (("run_cancelled", True, -15, "cancelled", "run_cancelled", 2),
             ("run_crashed", False, 1, "crashed", "run_crashed", 2),
             ("run_finished", False, 1, "crashed", "run_crashed", 1))
    for prior, cancel, code, status, event_type, expected in cases:
        s = _snap()
        tmp = tempfile.mkdtemp(prefix="ah_rerun_terminal_")
        release = threading.Event()

        class P:
            def __init__(self, argv, **kw):
                self.pid = 1
                self.returncode = None

            def poll(self):
                return self.returncode

            def wait(self):
                release.wait(5)
                self.returncode = code
                return code

        orig_popen = serve.subprocess.Popen
        try:
            serve._configure(tmp, "default", 8787)
            authorize_local_agents()
            serve._runs_add("aaaa0001", "goal")
            run_dir = os.path.join(serve.RUNS_DIR, "aaaa0001")
            os.makedirs(run_dir, exist_ok=True)
            events = os.path.join(run_dir, "events.jsonl")
            with open(events, "w", encoding="utf-8") as f:   # attempt 1 ended this way
                f.write('{"ts": 1, "id": "a", "type": "plan_created", "tasks": []}\n'
                        f'{{"ts": 2, "id": "b", "type": "{prior}", "error": "old", '
                        '"statuses": {"t1": "done"}}\n')
            serve._adopt_latest_run()
            serve.subprocess.Popen = P
            serve._spawn_main(["--resume"])                   # attempt 2, same files
            serve._run_meta["cancel_flag"]["cancel_requested"] = cancel
            release.set()
            deadline = time.monotonic() + 5
            while serve._runs[-1].get("status") != status and time.monotonic() < deadline:
                time.sleep(0.05)
            text = open(events, encoding="utf-8").read()
            assert text.count(f'"type": "{event_type}"') == expected, \
                f"after a {prior} attempt, attempt 2's {event_type} was swallowed:\n{text}"
            assert serve._runs[-1].get("status") == status, (prior, serve._runs[-1])
        finally:
            release.set()
            serve.subprocess.Popen = orig_popen
            _restore(s)
            shutil.rmtree(tmp, ignore_errors=True)
    print("PASS: test_rerun_attempt_still_gets_its_own_terminal_event")


def test_watch_run_without_cancel_flag_still_detects_real_crash():
    """Regression guard: adding cancel_flag must not weaken the existing
    crash detection for a run nobody asked to cancel."""
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_watchcrash_")
    try:
        serve._configure(tmp, "default", 8787)
        authorize_local_agents()
        serve._runs_add("r1", "boom")
        events = os.path.join(serve.RUNS_DIR, "r1", "events.jsonl")
        os.makedirs(os.path.dirname(events), exist_ok=True)
        with open(events, "w", encoding="utf-8") as f:
            f.write("")
        serve.EVENTS_PATH = events
        serve._watch_run("r1", _FakeProc(1), events, {"cancel_requested": False})
        serve._runs_load()
        assert serve._runs[0]["status"] == "crashed", serve._runs
        text = open(events, encoding="utf-8").read()
        assert '"type": "run_crashed"' in text, text
    finally:
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_cancel_kills_whole_process_tree()
    test_cancel_kills_descendant_that_started_its_own_process_group()
    test_cancel_without_running_returns_409()
    test_windows_cancel_does_not_hide_failed_taskkill()
    test_watch_run_cancel_flag_takes_priority_over_crash_detection()
    test_watch_run_does_not_duplicate_engine_run_cancelled()
    test_watch_run_keeps_the_engines_own_crash_reason()
    test_rerun_attempt_still_gets_its_own_terminal_event()
    test_watch_run_without_cancel_flag_still_detects_real_crash()
    print("\nAll cancel-run tests passed.")
