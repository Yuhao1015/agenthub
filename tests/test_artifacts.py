"""Real file deliverables: workdir snapshot diff -> tasks/{id}/files ->
GET /file preview. No framework -- plain assert, exit 1 on failure.

Run: python3 tests/test_artifacts.py
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

# macOS system proxy hijacks urllib's localhost requests via getproxies();
# force no proxy so these stay hermetic (same reason as test_serve_chat.py)
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.artifacts import changed_files, snapshot
from hub.blackboard import Blackboard
from hub.engine import Engine
from hub.executors import ExecResult
from hub.models import EventBus, Plan, Task, TaskType


class _WritingExecutor:
    """Stands in for a real coding agent: writes a file, returns text."""

    def __init__(self, workdir, relpath, body="hello"):
        self.workdir, self.relpath, self.body = workdir, relpath, body

    async def run(self, task, context, on_event=None):
        full = os.path.join(self.workdir, self.relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(self.body)
        return ExecResult(ok=True, output="done")


class _FakeRouter:
    budget = {"max_usd_per_run": 2.0, "max_attempts_per_task": 1}

    def __init__(self, executor):
        self._executor = executor

    def chain_for(self, task):
        return [("fake-agent", self._executor)]

    def animal(self, name):
        return "cat"

    def is_paid_api(self, name):
        return False


def _run_one_task(workdir, relpath, body="hello"):
    """Run a single task through the real Engine in a temp workdir.
    Returns (events, board)."""
    events_path = os.path.join(workdir, "_events.jsonl")
    board_path = os.path.join(workdir, "_board.jsonl")
    bus = EventBus(events_path)
    board = Blackboard(board_path, on_write=lambda e: None)
    task = Task(id="t1", title="write a file", type=TaskType.CODING, prompt="p")
    plan = Plan(goal="test", tasks={task.id: task})
    engine = Engine(plan, _FakeRouter(_WritingExecutor(workdir, relpath, body)),
                    bus, board, max_parallel=1, preflight=False, workdir=workdir,
                    summarize_enabled=False)
    asyncio.run(engine.run())
    bus.close()
    with open(events_path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()], board


def test_snapshot_skips_hidden_and_excluded():
    tmp = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmp, ".git"))
        os.makedirs(os.path.join(tmp, "node_modules"))
        for rel in ("keep.txt", ".hidden", "events.jsonl",
                    ".git/config", "node_modules/lib.js"):
            with open(os.path.join(tmp, rel), "w") as f:
                f.write("x")
        snap = snapshot(tmp, exclude=(os.path.join(tmp, "events.jsonl"),))
        assert set(snap) == {"keep.txt"}, f"unexpected snapshot contents: {sorted(snap)}"
    finally:
        shutil.rmtree(tmp)
    print("PASS: test_snapshot_skips_hidden_and_excluded")


def test_changed_files_detects_new_and_modified():
    before = {"a.txt": (1, 10), "b.txt": (1, 10)}
    after = {"a.txt": (1, 10), "b.txt": (2, 11), "c.txt": (3, 5)}
    got = changed_files(before, after)
    assert [f["path"] for f in got] == ["b.txt", "c.txt"], got
    assert got[1]["size"] == 5, got
    # an untracked (None) snapshot means tracking was off, never a guess
    assert changed_files(None, after) == []
    assert changed_files(before, None) == []
    print("PASS: test_changed_files_detects_new_and_modified")


def test_task_completed_carries_files_and_board_records_them():
    tmp = tempfile.mkdtemp()
    try:
        events, board = _run_one_task(tmp, "out/index.html", "<h1>hi</h1>")
        completed = [e for e in events if e["type"] == "task_completed"]
        assert len(completed) == 1, completed
        paths = [f["path"] for f in completed[0].get("files", [])]
        assert paths == ["out/index.html"], f"expected the written file, got {paths}"
        assert completed[0]["files"][0]["size"] == len("<h1>hi</h1>")
        recorded = board.get("tasks/t1/files")
        assert recorded == completed[0]["files"], (recorded, completed[0]["files"])
        assert board.get("run/workdir") == os.path.realpath(tmp), board.get("run/workdir")
    finally:
        shutil.rmtree(tmp)
    print("PASS: test_task_completed_carries_files_and_board_records_them")


def test_engine_runtime_artifacts_are_not_deliverables():
    """events.jsonl/blackboard.jsonl change during every task -- if they
    leaked into the diff, every single task would claim them."""
    tmp = tempfile.mkdtemp()
    try:
        events, _ = _run_one_task(tmp, "note.txt")
        completed = [e for e in events if e["type"] == "task_completed"][0]
        paths = [f["path"] for f in completed["files"]]
        assert paths == ["note.txt"], f"runtime artifacts leaked in: {paths}"
    finally:
        shutil.rmtree(tmp)
    print("PASS: test_engine_runtime_artifacts_are_not_deliverables")


def _get(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as r:
            return r.status, r.read(), r.headers.get("Content-Type")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type")


def test_serve_file_endpoint_whitelist():
    import serve

    tmp = tempfile.mkdtemp()
    try:
        _run_one_task(tmp, "out/index.html", "<h1>hi</h1>")
        original_board = serve.BOARD_PATH
        serve.BOARD_PATH = os.path.join(tmp, "_board.jsonl")
        srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            code, body, ctype = _get(port, "/file?task=t1&path=out/index.html")
            assert code == 200, (code, body)
            assert body == b"<h1>hi</h1>", body
            assert "html" in (ctype or ""), ctype

            # a real file in the workdir that this task never produced
            with open(os.path.join(tmp, "secret.txt"), "w") as f:
                f.write("nope")
            code, _, _ = _get(port, "/file?task=t1&path=secret.txt")
            assert code == 404, f"unrecorded file must not be servable, got {code}"

            # classic traversal, and traversal dressed up as a recorded path
            for probe in ("../../etc/passwd", "out/../../etc/passwd", ""):
                code, _, _ = _get(
                    port, "/file?task=t1&path=" + urllib.parse.quote(probe))
                assert code == 404, f"traversal probe {probe!r} returned {code}"

            code, _, _ = _get(port, "/file?task=nope&path=out/index.html")
            assert code == 404, "unknown task must 404"
        finally:
            srv.shutdown()
            serve.BOARD_PATH = original_board
    finally:
        shutil.rmtree(tmp)
    print("PASS: test_serve_file_endpoint_whitelist")


if __name__ == "__main__":
    test_snapshot_skips_hidden_and_excluded()
    test_changed_files_detects_new_and_modified()
    test_task_completed_carries_files_and_board_records_them()
    test_engine_runtime_artifacts_are_not_deliverables()
    test_serve_file_endpoint_whitelist()
    print("\nAll artifact tests passed.")
