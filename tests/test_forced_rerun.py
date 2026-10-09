"""Forced rerun: main.py --resume with neither --goal nor --plan
reconstructs the DAG from the blackboard (Plan.from_blackboard) and
Engine's resume-restoration loop leaves STALE tasks PENDING instead of
restoring them as DONE, so the normal DAG loop re-executes exactly those.
Covers the Engine-level mechanism, the main.py CLI path (via amain(),
--mock), and serve.py's POST /rerun endpoint. No framework -- plain
assert, exit 1 on failure. Zero real LLM calls.

Run: python tests/test_forced_rerun.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

# macOS system proxy (e.g. MacPacket) hijacks urllib's localhost requests
# via getproxies(); force no proxy so these tests stay hermetic.
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.blackboard import Blackboard
from hub.engine import Engine
from hub.executors import ExecResult
from hub.models import EventBus, Plan, Task, TaskType


class _RecordingRouter:
    budget = {"max_usd_per_run": 1e9, "max_attempts_per_task": 2}

    def __init__(self):
        self.calls = []

    def chain_for(self, task):
        return [("test-agent", self._Executor(self, task.id))]

    def animal(self, name):
        return "cat"

    def is_paid_api(self, name):
        return False

    class _Executor:
        def __init__(self, outer, task_id):
            self._outer = outer
            self._task_id = task_id

        async def run(self, task, context, on_event=None):
            self._outer.calls.append(task.id)
            return ExecResult(ok=True, output=f"rerun output for {task.id}", cost_usd=0.0)


def _fresh_paths(tag):
    events_path = os.path.join(os.path.dirname(__file__), f"_scratch_events_rerun_{tag}.jsonl")
    board_path = os.path.join(os.path.dirname(__file__), f"_scratch_board_rerun_{tag}.jsonl")
    for p in (events_path, board_path):
        if os.path.exists(p):
            os.remove(p)
    return events_path, board_path


def _cleanup(*paths):
    for p in paths:
        if os.path.exists(p):
            os.remove(p)


def test_engine_resume_reruns_only_stale_tasks():
    events_path, board_path = _fresh_paths("engine")
    bus = EventBus(events_path)
    board = Blackboard(board_path, on_write=lambda e: None)

    board.put("tasks/a/output", "old A output", author="test-agent", task_id="a", kind="output")
    board.put("tasks/a/status", "done", author="system", task_id="a")
    board.put("tasks/b/output", "old B output", author="test-agent", task_id="b", kind="output")
    board.put("tasks/b/status", "stale", author="system", task_id="b")

    plan = Plan(goal="test", tasks={})
    plan.tasks["a"] = Task(id="a", title="A", type=TaskType.CODING, prompt="p", depends_on=[])
    plan.tasks["b"] = Task(id="b", title="B", type=TaskType.CODING, prompt="p", depends_on=["a"])

    router = _RecordingRouter()
    engine = Engine(plan, router, bus, board, max_parallel=2, resume=True, preflight=False)
    asyncio.run(engine.run())
    bus.close()

    assert router.calls == ["b"], f"only the STALE task should re-execute, got {router.calls}"
    assert plan.tasks["a"].status.value == "done"
    assert plan.tasks["b"].status.value == "done", \
        "b must finish DONE after rerunning, not stay stale forever"
    assert board.get("tasks/a/output") == "old A output", "restored task must not be touched"
    assert board.get("tasks/b/output") == "rerun output for b", \
        "rerun must overwrite the stale task's output"
    assert board.get("tasks/b/status") == "done", \
        "status must self-heal from stale back to done once the rerun finishes"

    _cleanup(events_path, board_path)
    print("PASS: test_engine_resume_reruns_only_stale_tasks")


def test_plan_created_says_whether_this_is_a_rerun():
    """Bug B6 support: the dashboard used `restored` being non-empty as its
    only "this is a rerun, merge into the same session" signal. Rerunning a
    run whose first task failed restores nothing, so it showed up as a
    duplicate session. plan_created now carries `resumed` explicitly."""
    for resume in (True, False):
        events_path, board_path = _fresh_paths(f"resumed_{resume}")
        bus = EventBus(events_path)
        board = Blackboard(board_path, on_write=lambda e: None)
        plan = Plan(goal="test", tasks={
            "a": Task(id="a", title="A", type=TaskType.CODING, prompt="p")})
        # nothing on the board: a run whose only task failed restores nothing
        engine = Engine(plan, _RecordingRouter(), bus, board, resume=resume,
                        preflight=False)
        asyncio.run(engine.run())
        bus.close()
        with open(events_path, encoding="utf-8") as f:
            created = [json.loads(l) for l in f if '"plan_created"' in l][0]
        assert created["restored"] == [], created
        assert created.get("resumed") is resume, created
        _cleanup(events_path, board_path)
    print("PASS: test_plan_created_says_whether_this_is_a_rerun")


def test_amain_resume_without_goal_or_plan_reruns_stale(tmp_path=None):
    """End-to-end through main.py's actual CLI parsing + Plan.from_blackboard
    reconstruction, using --mock so MockExecutor stands in for every agent."""
    import main

    tmp = tempfile.mkdtemp()
    try:
        board_path = os.path.join(tmp, "blackboard.jsonl")
        events_path = os.path.join(tmp, "events.jsonl")

        plan_path = os.path.join(tmp, "plan.json")
        with open(plan_path, "w", encoding="utf-8") as f:
            json.dump({"goal": "g", "tasks": [
                {"id": "a", "title": "A", "type": "coding", "prompt": "p", "depends_on": []},
                {"id": "b", "title": "B", "type": "coding", "prompt": "p", "depends_on": ["a"]},
            ]}, f)

        argv = ["main.py", "--plan", plan_path, "--mock",
                "--events", events_path, "--board", board_path]
        old_argv = sys.argv
        sys.argv = argv
        try:
            rc = asyncio.run(main.amain())
        finally:
            sys.argv = old_argv
        assert rc == 0

        board = Blackboard(board_path, on_write=None)
        assert board.get("tasks/a/status") == "done"
        assert board.get("tasks/b/status") == "done"

        # simulate a deep-collaboration follow-up marking b stale (this is
        # normally serve.py + hub/conversation.py's job -- here we just
        # write the same blackboard fact directly, since the point of this
        # test is main.py's rerun path, not staleness detection itself)
        board.put("tasks/b/status", "stale", author="system", task_id="b")

        events_before = os.path.getsize(events_path)

        argv = ["main.py", "--resume", "--mock",
                "--events", events_path, "--board", board_path]
        sys.argv = argv
        try:
            rc = asyncio.run(main.amain())
        finally:
            sys.argv = old_argv
        assert rc == 0, "rerun must succeed with no --goal/--plan given"

        board = Blackboard(board_path, on_write=None)
        assert board.get("tasks/a/status") == "done"
        assert board.get("tasks/b/status") == "done"

        # MockExecutor's output is a deterministic function of the title,
        # so an unchanged string doesn't prove nothing reran -- check the
        # actual proof: which tasks got a fresh task_started event in THIS
        # rerun (only appended after events_before).
        with open(events_path, encoding="utf-8") as f:
            f.seek(events_before)
            new_events = [json.loads(l) for l in f if l.strip()]
        started = {e["task_id"] for e in new_events if e["type"] == "task_started"}
        assert started == {"b"}, f"only b (STALE) should re-execute, got {started}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS: test_amain_resume_without_goal_or_plan_reruns_stale")


def test_amain_resume_without_board_fails_cleanly():
    import main

    tmp = tempfile.mkdtemp()
    try:
        board_path = os.path.join(tmp, "blackboard.jsonl")  # never created
        events_path = os.path.join(tmp, "events.jsonl")
        argv = ["main.py", "--resume", "--mock",
                "--events", events_path, "--board", board_path]
        old_argv = sys.argv
        sys.argv = argv
        try:
            rc = asyncio.run(main.amain())
        finally:
            sys.argv = old_argv
        assert rc == 1, "must fail cleanly (not crash) with nothing to resume from"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS: test_amain_resume_without_board_fails_cleanly")


# ---------- serve.py POST /rerun ----------

def _post(port, path, obj):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(obj).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, port


def test_serve_rerun_spawns_main_with_resume_flag():
    import serve

    board_path = os.path.join(os.path.dirname(__file__), "_scratch_board_rerun_serve.jsonl")
    Blackboard(board_path, on_write=None).put("tasks/a/spec", {"title": "x"}, author="system")

    captured = {}

    def fake_spawn(argv_tail):
        captured["argv_tail"] = argv_tail
        return 4242

    original_board_path = serve.BOARD_PATH
    original_spawn = serve._spawn_main
    original_current = serve._current
    serve.BOARD_PATH = board_path
    serve._spawn_main = fake_spawn
    serve._current = None

    srv, port = _serve(serve.Handler)
    try:
        code, r = _post(port, "/rerun", {})
        assert code == 200, f"got {code} {r}"
        assert r == {"ok": True, "pid": 4242}
        assert captured["argv_tail"] == ["--resume", "--gate-mode=auto",
                                         "--assign-mode=auto"]
    finally:
        srv.shutdown()
        serve.BOARD_PATH = original_board_path
        serve._spawn_main = original_spawn
        serve._current = original_current
        _cleanup(board_path)
    print("PASS: test_serve_rerun_spawns_main_with_resume_flag")


def test_serve_rerun_of_a_mock_run_stays_mock():
    """Found by the 2026-09-25 browser check: /rerun rebuilt the argv from
    scratch and dropped --mock, so rerunning the zero-cost demo started a
    REAL run -- real preflight probes, then real agents for the rerun tasks.
    The dashboard's 重跑 buttons (bug B6) put that one click away. A mock
    run's rerun must stay mock, including after a server restart."""
    import serve

    captured = []

    class P:
        def __init__(self, argv, **kw):
            captured.append(argv)
            self.pid = 4242
            self.returncode = 0

        def poll(self):
            return 0            # already exited: /rerun must not see a busy run

        def wait(self):
            return 0

    saved = {name: getattr(serve, name) for name in (
        "BOARD_PATH", "EVENTS_PATH", "_current", "_run_meta", "_runs", "DATA_DIR",
        "RUNTIME_DIR", "RUNS_DIR", "INDEX_PATH", "CONFIG_PATH", "MERGED_MATRIX_PATH",
        "PREFLIGHT_PATH", "CREDENTIALS_PATH", "SCHEDULES_PATH", "SESSIONS_DIR",
        "ACTIVE_SESSION_PATH", "WORKDIR", "MATRIX")}
    tmp = tempfile.mkdtemp(prefix="ah_mock_rerun_")
    original_popen = serve.subprocess.Popen
    srv, port = _serve(serve.Handler)
    try:
        serve._configure(tmp, "default", port)
        serve.subprocess.Popen = P
        for mock, tail in ((True, ["--plan=example_plan.json", "--mock"]),
                           (False, ["--plan=example_plan.json"])):
            if not mock:
                from test_gate_serve import authorize_local_agents
                authorize_local_agents()
            serve._spawn_main(tail)
            Blackboard(serve.BOARD_PATH, on_write=None).put(
                "tasks/a/spec", {"title": "x"}, author="system")
            for restarted in (False, True):
                if restarted:           # what a restarted serve.py knows
                    serve._runs_load()
                    serve._adopt_latest_run()
                code, r = _post(port, "/rerun", {})
                assert code == 200, (code, r)
                assert "--resume" in captured[-1]
                assert ("--mock" in captured[-1]) is mock, \
                    f"mock={mock} restarted={restarted}: {captured[-1]}"
    finally:
        srv.shutdown()
        serve.subprocess.Popen = original_popen
        for name, value in saved.items():
            setattr(serve, name, value)
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS: test_serve_rerun_of_a_mock_run_stays_mock")


def test_serve_rerun_without_board_returns_400():
    import serve

    board_path = os.path.join(os.path.dirname(__file__), "_scratch_board_rerun_missing.jsonl")
    _cleanup(board_path)  # make sure it really doesn't exist

    original_board_path = serve.BOARD_PATH
    original_current = serve._current
    serve.BOARD_PATH = board_path
    serve._current = None

    srv, port = _serve(serve.Handler)
    try:
        code, r = _post(port, "/rerun", {})
        assert code == 400, f"got {code} {r}"
        assert "no existing blackboard" in r["error"]
    finally:
        srv.shutdown()
        serve.BOARD_PATH = original_board_path
        serve._current = original_current
    print("PASS: test_serve_rerun_without_board_returns_400")


def test_serve_rerun_409_when_run_in_progress():
    import serve

    board_path = os.path.join(os.path.dirname(__file__), "_scratch_board_rerun_busy.jsonl")
    Blackboard(board_path, on_write=None).put("tasks/a/spec", {"title": "x"}, author="system")

    import subprocess
    real_sleep_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])

    original_board_path = serve.BOARD_PATH
    original_current = serve._current
    serve.BOARD_PATH = board_path
    serve._current = real_sleep_proc

    srv, port = _serve(serve.Handler)
    try:
        code, r = _post(port, "/rerun", {})
        assert code == 409, f"got {code} {r}"
    finally:
        srv.shutdown()
        serve.BOARD_PATH = original_board_path
        serve._current = original_current
        real_sleep_proc.kill()
        real_sleep_proc.wait()
        _cleanup(board_path)
    print("PASS: test_serve_rerun_409_when_run_in_progress")


if __name__ == "__main__":
    test_engine_resume_reruns_only_stale_tasks()
    test_plan_created_says_whether_this_is_a_rerun()
    test_amain_resume_without_goal_or_plan_reruns_stale()
    test_amain_resume_without_board_fails_cleanly()
    test_serve_rerun_spawns_main_with_resume_flag()
    test_serve_rerun_of_a_mock_run_stays_mock()
    test_serve_rerun_without_board_returns_400()
    test_serve_rerun_409_when_run_in_progress()
    print("\nAll forced-rerun tests passed.")
