"""POST /task/{id}/message. Real Blackboard-backed state (serve.py now
reads tasks/{id}/spec + tasks/{id}/status directly to decide whether to
answer synchronously or just queue -- a bare stub object can't stand in
for that anymore), fully faked Router/continue_task_conversation/LLM.

Run: python tests/test_serve_task_message.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

# macOS system proxy (e.g. MacPacket) hijacks urllib's localhost requests
# via getproxies(); force no proxy so these tests stay hermetic.
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
from hub import conversation
from hub.blackboard import Blackboard
from hub.executors import ExecResult


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


def _serve():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, port


def _fresh_board_path(tag):
    path = os.path.join(os.path.dirname(__file__), f"_scratch_board_msg_{tag}.jsonl")
    if os.path.exists(path):
        os.remove(path)
    return path


def _rm(path):
    """Blackboard only creates the file on its first put() -- a board that
    was constructed but never written to (the unknown-task-id tests) never
    touches disk at all, so cleanup must tolerate that."""
    if os.path.exists(path):
        os.remove(path)


def _seed(board_path, task_id, *, done=False, depends_on=None):
    """Write tasks/{id}/spec (always -- otherwise the endpoint 404s as
    unknown) and, if done, tasks/{id}/status="done" so the handler takes
    the synchronous continue_task_conversation path."""
    board = Blackboard(board_path, on_write=None)
    board.put(f"tasks/{task_id}/spec", {"title": "x", "depends_on": depends_on or []},
             author="system", kind="spec")
    if done:
        board.put(f"tasks/{task_id}/status", "done", author="engine",
                 task_id=task_id, kind="fact")
    return board


def _fresh_events_path(tag):
    path = os.path.join(os.path.dirname(__file__), f"_scratch_events_msg_{tag}.jsonl")
    if os.path.exists(path):
        os.remove(path)
    return path


def test_task_message_success():
    board_path = _fresh_board_path("success")
    _seed(board_path, "t1", done=True)
    captured = {}

    async def fake_continue(task_id, message, router, board):
        captured["args"] = (task_id, message)
        return ExecResult(ok=True, output="here is the answer", cost_usd=0.0173), False

    original_continue = conversation.continue_task_conversation
    original_router = serve.Router
    original_board_path = serve.BOARD_PATH
    conversation.continue_task_conversation = fake_continue
    serve.Router = lambda *a, **k: object()
    serve.BOARD_PATH = board_path

    srv, port = _serve()
    try:
        code, r = _post(port, "/task/t1/message", {"message": "what about X?"})
        assert code == 200, f"got {code} {r}"
        assert r["reply"] == "here is the answer"
        assert abs(r["cost_usd"] - 0.0173) < 1e-9
        assert r["compacted"] is False
        assert captured["args"] == ("t1", "what about X?")
    finally:
        srv.shutdown()
        conversation.continue_task_conversation = original_continue
        serve.Router = original_router
        serve.BOARD_PATH = original_board_path
        _rm(board_path)
    print("PASS: test_task_message_success")


def test_task_message_marks_stale_dependents_and_emits_event():
    """t2 depends on t1 and is already DONE. A follow-up reply to t1 that
    overwrites its output must mark t2 STALE, report it in the response,
    and emit a task_stale event -- the actual point of this whole feature:
    the user finds out a downstream deliverable may now be wrong."""
    board_path = _fresh_board_path("stale")
    events_path = _fresh_events_path("stale")
    _seed(board_path, "t1", done=True)
    _seed(board_path, "t2", done=True, depends_on=["t1"])

    async def fake_continue(task_id, message, router, board):
        return ExecResult(ok=True, output="revised architecture", cost_usd=0.01), False

    original_continue = conversation.continue_task_conversation
    original_router = serve.Router
    original_board_path = serve.BOARD_PATH
    original_events_path = serve.EVENTS_PATH
    conversation.continue_task_conversation = fake_continue
    serve.Router = lambda *a, **k: object()
    serve.BOARD_PATH = board_path
    serve.EVENTS_PATH = events_path

    srv, port = _serve()
    try:
        code, r = _post(port, "/task/t1/message", {"message": "rethink this"})
        assert code == 200, f"got {code} {r}"
        assert r["stale_downstream"] == ["t2"], f"got {r}"

        board = Blackboard(board_path, on_write=None)
        assert board.get("tasks/t2/status") == "stale"

        with open(events_path, encoding="utf-8") as f:
            events = [json.loads(l) for l in f if l.strip()]
        stale_events = [e for e in events if e["type"] == "task_stale"]
        assert len(stale_events) == 1, f"got {events}"
        assert stale_events[0]["task_id"] == "t2"
        assert stale_events[0]["cause_task_id"] == "t1"
    finally:
        srv.shutdown()
        conversation.continue_task_conversation = original_continue
        serve.Router = original_router
        serve.BOARD_PATH = original_board_path
        serve.EVENTS_PATH = original_events_path
        _rm(board_path)
        _rm(events_path)
    print("PASS: test_task_message_marks_stale_dependents_and_emits_event")


def test_task_message_no_stale_dependents_omits_event():
    """The overwhelmingly common case (no downstream, or downstream not
    DONE yet) must not touch events.jsonl at all -- stale_downstream is []
    and no EventBus file gets created."""
    board_path = _fresh_board_path("nostale")
    events_path = _fresh_events_path("nostale")
    _seed(board_path, "t1", done=True)

    async def fake_continue(task_id, message, router, board):
        return ExecResult(ok=True, output="minor clarification", cost_usd=0.01), False

    original_continue = conversation.continue_task_conversation
    original_router = serve.Router
    original_board_path = serve.BOARD_PATH
    original_events_path = serve.EVENTS_PATH
    conversation.continue_task_conversation = fake_continue
    serve.Router = lambda *a, **k: object()
    serve.BOARD_PATH = board_path
    serve.EVENTS_PATH = events_path

    srv, port = _serve()
    try:
        code, r = _post(port, "/task/t1/message", {"message": "what did you mean?"})
        assert code == 200, f"got {code} {r}"
        assert r["stale_downstream"] == []
        assert not os.path.exists(events_path), \
            "no dependents to mark -- must not even open an EventBus"
    finally:
        srv.shutdown()
        conversation.continue_task_conversation = original_continue
        serve.Router = original_router
        serve.BOARD_PATH = original_board_path
        serve.EVENTS_PATH = original_events_path
        _rm(board_path)
        _rm(events_path)
    print("PASS: test_task_message_no_stale_dependents_omits_event")


def test_task_message_unknown_task_returns_404():
    """No tasks/{id}/spec at all -- the task_id was never part of any
    plan. Must 404 WITHOUT ever calling continue_task_conversation."""
    board_path = _fresh_board_path("unknown")
    Blackboard(board_path, on_write=None)  # empty board, nothing seeded

    def fake_continue(*a, **k):
        raise AssertionError("must not be called for an unknown task_id")

    original_continue = conversation.continue_task_conversation
    original_router = serve.Router
    original_board_path = serve.BOARD_PATH
    conversation.continue_task_conversation = fake_continue
    serve.Router = lambda *a, **k: object()
    serve.BOARD_PATH = board_path

    srv, port = _serve()
    try:
        code, r = _post(port, "/task/ghost/message", {"message": "hi"})
        assert code == 404, f"got {code} {r}"
        assert "not found" in r["error"]
    finally:
        srv.shutdown()
        conversation.continue_task_conversation = original_continue
        serve.Router = original_router
        serve.BOARD_PATH = original_board_path
        _rm(board_path)
    print("PASS: test_task_message_unknown_task_returns_404")


def test_task_message_done_task_without_session_still_404():
    """A task that IS done but never ran a claude_code agent (no session
    recorded) -- continue_task_conversation's own ConversationError must
    still surface as a 404 through the synchronous path."""
    board_path = _fresh_board_path("nosession")
    _seed(board_path, "t1", done=True)

    async def fake_continue(task_id, message, router, board):
        raise conversation.ConversationError(f"task '{task_id}' has no recorded session")

    original_continue = conversation.continue_task_conversation
    original_router = serve.Router
    original_board_path = serve.BOARD_PATH
    conversation.continue_task_conversation = fake_continue
    serve.Router = lambda *a, **k: object()
    serve.BOARD_PATH = board_path

    srv, port = _serve()
    try:
        code, r = _post(port, "/task/t1/message", {"message": "hi"})
        assert code == 404, f"got {code} {r}"
        assert "no recorded session" in r["error"]
    finally:
        srv.shutdown()
        conversation.continue_task_conversation = original_continue
        serve.Router = original_router
        serve.BOARD_PATH = original_board_path
        _rm(board_path)
    print("PASS: test_task_message_done_task_without_session_still_404")


def test_task_message_agent_error_returns_502():
    board_path = _fresh_board_path("agenterr")
    _seed(board_path, "t1", done=True)

    async def fake_continue(task_id, message, router, board):
        return ExecResult(ok=False, error="claude -p timed out", error_kind="transient"), False

    original_continue = conversation.continue_task_conversation
    original_router = serve.Router
    original_board_path = serve.BOARD_PATH
    conversation.continue_task_conversation = fake_continue
    serve.Router = lambda *a, **k: object()
    serve.BOARD_PATH = board_path

    srv, port = _serve()
    try:
        code, r = _post(port, "/task/t1/message", {"message": "hi"})
        assert code == 502, f"got {code} {r}"
        assert "timed out" in r["error"]
    finally:
        srv.shutdown()
        conversation.continue_task_conversation = original_continue
        serve.Router = original_router
        serve.BOARD_PATH = original_board_path
        _rm(board_path)
    print("PASS: test_task_message_agent_error_returns_502")


def test_task_message_502_still_surfaces_spent_cost():
    """A compaction pass can succeed (real spend, digest written) right
    before the follow-up fresh-session call fails -- hub/conversation.py
    already sums both calls' cost onto the returned (failed) ExecResult,
    but that's worthless if serve.py drops it on the 502 path."""
    board_path = _fresh_board_path("coston502")
    _seed(board_path, "t1", done=True)

    async def fake_continue(task_id, message, router, board):
        return ExecResult(ok=False, error="fresh session failed after compaction",
                          error_kind="task_error", cost_usd=0.04), True

    original_continue = conversation.continue_task_conversation
    original_router = serve.Router
    original_board_path = serve.BOARD_PATH
    conversation.continue_task_conversation = fake_continue
    serve.Router = lambda *a, **k: object()
    serve.BOARD_PATH = board_path

    srv, port = _serve()
    try:
        code, r = _post(port, "/task/t1/message", {"message": "hi"})
        assert code == 502, f"got {code} {r}"
        assert abs(r["cost_usd"] - 0.04) < 1e-9, \
            f"compaction spend must survive a 502, got {r}"
    finally:
        srv.shutdown()
        conversation.continue_task_conversation = original_continue
        serve.Router = original_router
        serve.BOARD_PATH = original_board_path
        _rm(board_path)
    print("PASS: test_task_message_502_still_surfaces_spent_cost")


def test_task_message_needs_message():
    srv, port = _serve()
    try:
        code, r = _post(port, "/task/t1/message", {})
        assert code == 400, f"got {code} {r}"
    finally:
        srv.shutdown()
    print("PASS: test_task_message_needs_message")


def test_task_message_empty_task_id_returns_400():
    srv, port = _serve()
    try:
        code, r = _post(port, "/task//message", {"message": "hi"})
        assert code == 400, f"got {code} {r}"
    finally:
        srv.shutdown()
    print("PASS: test_task_message_empty_task_id_returns_400")


def test_task_message_whitespace_task_id_is_handled_safely():
    # A literal unescaped space in a URL path is not valid HTTP: the stdlib
    # http.server request-line parser rejects it with 400 before do_POST is
    # even reached (verified directly with a raw socket), so it never gets
    # near _handle_task_message. Any real client (fetch/XHR/curl) percent-
    # encodes a space as %20, and serve.py never unquotes self.path, so the
    # task_id that actually reaches _handle_task_message is the literal
    # 3-char string "%20" -- truthy, no "/", passes validation. With no
    # tasks/%20/spec ever seeded, this now 404s as unknown, same as any
    # other never-seen task_id.
    board_path = _fresh_board_path("whitespace")
    Blackboard(board_path, on_write=None)

    original_board_path = serve.BOARD_PATH
    serve.BOARD_PATH = board_path

    srv, port = _serve()
    try:
        code, r = _post(port, "/task/%20/message", {"message": "hi"})
        assert code == 404, f"got {code} {r}"
        assert "not found" in r["error"]
    finally:
        srv.shutdown()
        serve.BOARD_PATH = original_board_path
        _rm(board_path)
    print("PASS: test_task_message_whitespace_task_id_is_handled_safely")


def test_task_message_not_done_yet_queues_without_calling_continue():
    """A task that's known (spec exists) but not yet done -- the live
    Engine coroutine may still own its session, so the endpoint must NOT
    call continue_task_conversation directly; it should just queue the
    message into the blackboard inbox and return immediately."""
    board_path = _fresh_board_path("queue")
    _seed(board_path, "t1", done=False)

    def fake_continue(*a, **k):
        raise AssertionError("must not call continue_task_conversation for a non-done task")

    original_continue = conversation.continue_task_conversation
    original_board_path = serve.BOARD_PATH
    conversation.continue_task_conversation = fake_continue
    serve.BOARD_PATH = board_path

    srv, port = _serve()
    try:
        code, r = _post(port, "/task/t1/message", {"message": "keep going"})
        assert code == 200, f"got {code} {r}"
        assert r == {"queued": True}, r

        board = Blackboard(board_path, on_write=None)
        assert board.get("tasks/t1/inbox/0") == "keep going"
    finally:
        srv.shutdown()
        conversation.continue_task_conversation = original_continue
        serve.BOARD_PATH = original_board_path
        _rm(board_path)
    print("PASS: test_task_message_not_done_yet_queues_without_calling_continue")


def test_task_message_finish_flag_writes_inbox_finish():
    board_path = _fresh_board_path("finish")
    _seed(board_path, "t1", done=False)
    original_board_path = serve.BOARD_PATH
    serve.BOARD_PATH = board_path

    srv, port = _serve()
    try:
        code, r = _post(port, "/task/t1/message", {"message": "", "finish": True})
        assert code == 200, f"got {code} {r}"
        assert r == {"queued": True}, r

        board = Blackboard(board_path, on_write=None)
        assert board.get("tasks/t1/inbox_finish") is True
        assert board.get("tasks/t1/inbox/0") is None, \
            "an empty message must not create an inbox entry"
    finally:
        srv.shutdown()
        serve.BOARD_PATH = original_board_path
        _rm(board_path)
    print("PASS: test_task_message_finish_flag_writes_inbox_finish")


def test_task_message_queue_appends_sequential_indices():
    board_path = _fresh_board_path("sequential")
    _seed(board_path, "t1", done=False)
    original_board_path = serve.BOARD_PATH
    serve.BOARD_PATH = board_path

    srv, port = _serve()
    try:
        code1, r1 = _post(port, "/task/t1/message", {"message": "first"})
        code2, r2 = _post(port, "/task/t1/message", {"message": "second"})
        assert code1 == 200 and code2 == 200, (r1, r2)

        board = Blackboard(board_path, on_write=None)
        assert board.get("tasks/t1/inbox/0") == "first"
        assert board.get("tasks/t1/inbox/1") == "second"
    finally:
        srv.shutdown()
        serve.BOARD_PATH = original_board_path
        _rm(board_path)
    print("PASS: test_task_message_queue_appends_sequential_indices")


def test_concurrent_messages_to_same_task_are_serialized():
    """Two concurrent follow-ups to the SAME task_id (double-click send, two
    browser tabs) must not race -- the lock must still serialize access to
    the shared inbox-index computation even in the new queue-only path."""
    board_path = _fresh_board_path("concurrent_same")
    _seed(board_path, "t1", done=False)
    original_board_path = serve.BOARD_PATH
    serve.BOARD_PATH = board_path

    srv, port = _serve()
    results = []
    try:
        def worker(msg):
            results.append(_post(port, "/task/t1/message", {"message": msg}))
        t1 = threading.Thread(target=worker, args=("first",))
        t2 = threading.Thread(target=worker, args=("second",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert all(code == 200 for code, _ in results), f"got {results}"

        board = Blackboard(board_path, on_write=None)
        values = {board.get("tasks/t1/inbox/0"), board.get("tasks/t1/inbox/1")}
        assert values == {"first", "second"}, \
            f"both messages must land at distinct indices, no lost update, got {values}"
    finally:
        srv.shutdown()
        serve.BOARD_PATH = original_board_path
        _rm(board_path)
    print("PASS: test_concurrent_messages_to_same_task_are_serialized")


def test_concurrent_messages_to_different_tasks_run_in_parallel():
    """The per-task_id lock must not become a global lock -- two DIFFERENT
    task_ids' follow-ups should be able to actually overlap in time."""
    board_path = _fresh_board_path("concurrent_diff")
    _seed(board_path, "task-a", done=False)
    _seed(board_path, "task-b", done=False)
    original_board_path = serve.BOARD_PATH
    serve.BOARD_PATH = board_path

    concurrency = {"count": 0, "max": 0}
    guard = threading.Lock()
    original_lock_for_task = serve._lock_for_task

    class _SlowLock:
        """Wraps the real per-task lock but sleeps while held, purely so
        the test has a reliable window to observe overlap -- the real
        queue-write itself is too fast to race deterministically."""
        def __init__(self, real):
            self._real = real

        def __enter__(self):
            self._real.acquire()
            with guard:
                concurrency["count"] += 1
                concurrency["max"] = max(concurrency["max"], concurrency["count"])
            time.sleep(0.2)
            return self

        def __exit__(self, *a):
            with guard:
                concurrency["count"] -= 1
            self._real.release()

    serve._lock_for_task = lambda task_id: _SlowLock(original_lock_for_task(task_id))

    srv, port = _serve()
    results = []
    try:
        def worker(task_id):
            results.append(_post(port, f"/task/{task_id}/message", {"message": "hi"}))
        t1 = threading.Thread(target=worker, args=("task-a",))
        t2 = threading.Thread(target=worker, args=("task-b",))
        t1.start();t2.start()
        t1.join();t2.join()
        assert concurrency["max"] == 2, \
            f"different task_ids must be able to run concurrently, got max={concurrency['max']}"
        assert all(code == 200 for code, _ in results), f"got {results}"
    finally:
        srv.shutdown()
        serve._lock_for_task = original_lock_for_task
        serve.BOARD_PATH = original_board_path
        _rm(board_path)
    print("PASS: test_concurrent_messages_to_different_tasks_run_in_parallel")


if __name__ == "__main__":
    test_task_message_success()
    test_task_message_marks_stale_dependents_and_emits_event()
    test_task_message_no_stale_dependents_omits_event()
    test_task_message_unknown_task_returns_404()
    test_task_message_done_task_without_session_still_404()
    test_task_message_agent_error_returns_502()
    test_task_message_502_still_surfaces_spent_cost()
    test_task_message_needs_message()
    test_task_message_empty_task_id_returns_400()
    test_task_message_whitespace_task_id_is_handled_safely()
    test_task_message_not_done_yet_queues_without_calling_continue()
    test_task_message_finish_flag_writes_inbox_finish()
    test_task_message_queue_appends_sequential_indices()
    test_concurrent_messages_to_same_task_are_serialized()
    test_concurrent_messages_to_different_tasks_run_in_parallel()
    print("\nAll serve /task/{id}/message tests passed.")
