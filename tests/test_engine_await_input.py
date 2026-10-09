"""Reactive AWAITING_INPUT loop (deep collaboration mode for RUNNING tasks).

A claude_code task only ever pauses for follow-up messages if the
blackboard inbox already has >=1 entry the instant its turn finishes --
this is what keeps normal DAG runs (nobody messaging anything) exactly as
fast as before this feature existed. No framework -- plain assert, exit 1
on failure. Zero real LLM calls (subprocess faked).

Run: python tests/test_engine_await_input.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hub.engine as engine_mod
from hub.blackboard import Blackboard
from hub.engine import Engine
from hub.executors import ClaudeCodeExecutor
from hub.models import EventBus, Plan, Task, TaskStatus, TaskType


class _FakeRouter:
    def __init__(self, executor, timeout_sec=None):
        self._executor = executor
        self.budget = {"max_usd_per_run": 1e9, "max_attempts_per_task": 2}
        if timeout_sec is not None:
            self.budget["awaiting_input_timeout_sec"] = timeout_sec

    def chain_for(self, task):
        return [("test-agent", self._executor)]

    def animal(self, name):
        return "cat"

    def is_paid_api(self, name):
        return False


def _fresh_paths(tag):
    events_path = os.path.join(os.path.dirname(__file__), f"_scratch_events_{tag}.jsonl")
    board_path = os.path.join(os.path.dirname(__file__), f"_scratch_board_{tag}.jsonl")
    for p in (events_path, board_path):
        if os.path.exists(p):
            os.remove(p)
    return events_path, board_path


def _cleanup(events_path, board_path):
    for p in (events_path, board_path):
        if os.path.exists(p):
            os.remove(p)


def _script_subprocess(responses: list):
    """Each successive asyncio.create_subprocess_exec call (first turn,
    then each run_resumed call) pops the next response off the front."""
    queue = list(responses)

    async def fake_exec(*args, **kwargs):
        class _Proc:
            returncode = 0

            async def communicate(self_inner, input=None):
                payload = queue.pop(0)
                return json.dumps(payload).encode("utf-8"), b""
        return _Proc()
    asyncio.create_subprocess_exec = fake_exec


def _events(events_path):
    with open(events_path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def test_no_inbox_message_finalizes_immediately_unchanged():
    """The overwhelming common case: nobody messaged this task while it
    ran -- must behave exactly like before this feature existed, no
    AWAITING_INPUT detour, no new events."""
    events_path, board_path = _fresh_paths("await1")
    bus = EventBus(events_path)
    board = Blackboard(board_path, on_write=lambda e: None)
    plan = Plan(goal="test", tasks={})
    task = Task(id="t1", title="x", type=TaskType.CODING, prompt="p")
    plan.tasks["t1"] = task

    executor = ClaudeCodeExecutor(model="opus", workdir=".", stream=False)
    router = _FakeRouter(executor)
    engine = Engine(plan, router, bus, board, max_parallel=1)

    original = asyncio.create_subprocess_exec
    _script_subprocess([{
        "type": "result", "is_error": False, "result": "done",
        "total_cost_usd": 0.02, "session_id": "sess-1",
        "usage": {"input_tokens": 5},
    }])
    try:
        asyncio.run(engine._run_task(task))
    finally:
        asyncio.create_subprocess_exec = original
    bus.close()

    assert task.status == TaskStatus.DONE
    assert board.get("tasks/t1/status") == "done"
    types = [e["type"] for e in _events(events_path)]
    assert "task_awaiting_input" not in types, types
    assert "task_message_replied" not in types, types
    assert "task_auto_finished" not in types, types

    _cleanup(events_path, board_path)
    print("PASS: test_no_inbox_message_finalizes_immediately_unchanged")


def test_queued_message_consumed_then_finish_flag_closes_it_out():
    """A message queued before the first turn finishes (simulating
    serve.py writing it mid-run) must be picked up reactively, answered
    via a resumed turn, and -- once inbox_finish is set -- finalized
    without waiting out the idle timeout."""
    events_path, board_path = _fresh_paths("await2")
    bus = EventBus(events_path)
    board = Blackboard(board_path, on_write=lambda e: None)
    plan = Plan(goal="test", tasks={})
    task = Task(id="t1", title="x", type=TaskType.CODING, prompt="p")
    plan.tasks["t1"] = task

    # Simulate a message having arrived (via serve.py) while this task
    # was still mid-first-turn.
    board.put("tasks/t1/inbox/0", "what about edge cases?",
             author="user", task_id="t1", kind="message")
    board.put("tasks/t1/inbox_finish", True, author="user", task_id="t1", kind="fact")

    executor = ClaudeCodeExecutor(model="opus", workdir=".", stream=False)
    router = _FakeRouter(executor)
    engine = Engine(plan, router, bus, board, max_parallel=1)

    original = asyncio.create_subprocess_exec
    _script_subprocess([
        {"type": "result", "is_error": False, "result": "first turn done",
         "total_cost_usd": 0.02, "session_id": "sess-1", "usage": {"input_tokens": 5}},
        {"type": "result", "is_error": False, "result": "covered the edge cases",
         "total_cost_usd": 0.01, "session_id": "sess-1", "usage": {"input_tokens": 8}},
    ])
    try:
        asyncio.run(engine._run_task(task))
    finally:
        asyncio.create_subprocess_exec = original
    bus.close()

    assert task.status == TaskStatus.DONE
    assert task.result == "covered the edge cases"
    assert board.get("tasks/t1/status") == "done"
    assert abs(task.cost_usd - 0.03) < 1e-9, task.cost_usd

    evts = _events(events_path)
    types = [e["type"] for e in evts]
    assert "task_awaiting_input" in types, types
    assert "task_auto_finished" not in types, types
    replied = [e for e in evts if e["type"] == "task_message_replied"]
    assert len(replied) == 1, replied
    assert replied[0]["reply"] == "covered the edge cases"

    _cleanup(events_path, board_path)
    print("PASS: test_queued_message_consumed_then_finish_flag_closes_it_out")


def test_multiple_queued_messages_consumed_in_order():
    events_path, board_path = _fresh_paths("await3")
    bus = EventBus(events_path)
    board = Blackboard(board_path, on_write=lambda e: None)
    plan = Plan(goal="test", tasks={})
    task = Task(id="t1", title="x", type=TaskType.CODING, prompt="p")
    plan.tasks["t1"] = task

    board.put("tasks/t1/inbox/0", "question one", author="user", task_id="t1", kind="message")
    board.put("tasks/t1/inbox/1", "question two", author="user", task_id="t1", kind="message")
    board.put("tasks/t1/inbox_finish", True, author="user", task_id="t1", kind="fact")

    executor = ClaudeCodeExecutor(model="opus", workdir=".", stream=False)
    router = _FakeRouter(executor)
    engine = Engine(plan, router, bus, board, max_parallel=1)

    original = asyncio.create_subprocess_exec
    _script_subprocess([
        {"type": "result", "is_error": False, "result": "first turn done",
         "total_cost_usd": 0.0, "session_id": "sess-1"},
        {"type": "result", "is_error": False, "result": "answer one",
         "total_cost_usd": 0.0, "session_id": "sess-1"},
        {"type": "result", "is_error": False, "result": "answer two",
         "total_cost_usd": 0.0, "session_id": "sess-1"},
    ])
    try:
        asyncio.run(engine._run_task(task))
    finally:
        asyncio.create_subprocess_exec = original
    bus.close()

    replied = [e for e in _events(events_path) if e["type"] == "task_message_replied"]
    assert [e["reply"] for e in replied] == ["answer one", "answer two"], replied
    assert task.result == "answer two"

    _cleanup(events_path, board_path)
    print("PASS: test_multiple_queued_messages_consumed_in_order")


def test_idle_timeout_auto_finishes_and_notifies():
    """No finish signal and no further message: after
    awaiting_input_timeout_sec of silence, the task must auto-finalize to
    done AND emit task_auto_finished so the user is told why the
    conversation closed itself out."""
    events_path, board_path = _fresh_paths("await4")
    bus = EventBus(events_path)
    board = Blackboard(board_path, on_write=lambda e: None)
    plan = Plan(goal="test", tasks={})
    task = Task(id="t1", title="x", type=TaskType.CODING, prompt="p")
    plan.tasks["t1"] = task

    board.put("tasks/t1/inbox/0", "one question", author="user", task_id="t1", kind="message")
    # deliberately no inbox_finish -- must time out on its own

    executor = ClaudeCodeExecutor(model="opus", workdir=".", stream=False)
    router = _FakeRouter(executor, timeout_sec=0.06)
    engine = Engine(plan, router, bus, board, max_parallel=1)

    original_poll = engine_mod.AWAITING_INPUT_POLL_INTERVAL
    engine_mod.AWAITING_INPUT_POLL_INTERVAL = 0.02
    original_exec = asyncio.create_subprocess_exec
    _script_subprocess([
        {"type": "result", "is_error": False, "result": "first turn done",
         "total_cost_usd": 0.0, "session_id": "sess-1"},
        {"type": "result", "is_error": False, "result": "answered the one question",
         "total_cost_usd": 0.0, "session_id": "sess-1"},
    ])
    try:
        asyncio.run(engine._run_task(task))
    finally:
        asyncio.create_subprocess_exec = original_exec
        engine_mod.AWAITING_INPUT_POLL_INTERVAL = original_poll
    bus.close()

    assert task.status == TaskStatus.DONE
    assert board.get("tasks/t1/status") == "done"
    types = [e["type"] for e in _events(events_path)]
    assert "task_auto_finished" in types, types

    _cleanup(events_path, board_path)
    print("PASS: test_idle_timeout_auto_finishes_and_notifies")


def test_semaphore_released_while_awaiting_input():
    """The whole point of the reactive loop: while idle-waiting for a
    reply, the concurrency slot must be free for OTHER tasks, not held
    hostage by this one. Two tasks, max_parallel=1 -- if task A holds the
    slot the entire time it's awaiting input, task B could never start
    until A finishes; assert B actually got to run concurrently."""
    events_path, board_path = _fresh_paths("await5")
    bus = EventBus(events_path)
    board = Blackboard(board_path, on_write=lambda e: None)
    plan = Plan(goal="test", tasks={})
    task_a = Task(id="a", title="a", type=TaskType.CODING, prompt="p")
    task_b = Task(id="b", title="b", type=TaskType.CODING, prompt="p")
    plan.tasks["a"] = task_a
    plan.tasks["b"] = task_b

    board.put("tasks/a/inbox/0", "keep talking", author="user", task_id="a", kind="message")

    executor = ClaudeCodeExecutor(model="opus", workdir=".", stream=False)
    router = _FakeRouter(executor, timeout_sec=0.05)
    engine = Engine(plan, router, bus, board, max_parallel=1)

    original_poll = engine_mod.AWAITING_INPUT_POLL_INTERVAL
    engine_mod.AWAITING_INPUT_POLL_INTERVAL = 0.02
    original_exec = asyncio.create_subprocess_exec
    _script_subprocess([
        {"type": "result", "is_error": False, "result": "a first turn",
         "total_cost_usd": 0.0, "session_id": "sess-a"},
        {"type": "result", "is_error": False, "result": "a resumed reply",
         "total_cost_usd": 0.0, "session_id": "sess-a"},
        {"type": "result", "is_error": False, "result": "b done",
         "total_cost_usd": 0.0, "session_id": "sess-b"},
    ])
    async def _run_both():
        await asyncio.gather(engine._run_task(task_a), engine._run_task(task_b))

    try:
        asyncio.run(asyncio.wait_for(_run_both(), timeout=5.0))
    finally:
        asyncio.create_subprocess_exec = original_exec
        engine_mod.AWAITING_INPUT_POLL_INTERVAL = original_poll
    bus.close()

    assert task_a.status == TaskStatus.DONE
    assert task_b.status == TaskStatus.DONE, \
        "task B must have been able to run while A was awaiting input, not starved"

    _cleanup(events_path, board_path)
    print("PASS: test_semaphore_released_while_awaiting_input")


if __name__ == "__main__":
    test_no_inbox_message_finalizes_immediately_unchanged()
    test_queued_message_consumed_then_finish_flag_closes_it_out()
    test_multiple_queued_messages_consumed_in_order()
    test_idle_timeout_auto_finishes_and_notifies()
    test_semaphore_released_while_awaiting_input()
    print("\nAll engine await-input tests passed.")
