"""Engine must persist tasks/{id}/agent (always) and tasks/{id}/session_id
+ tasks/{id}/session_usage_ratio (claude_code only, when a session_id
came back) to the blackboard on task completion -- this is what lets a
later, separate serve.py request resume the conversation via
hub/conversation.py's continue_task_conversation(). No framework --
plain assert, exit 1 on failure. Zero real LLM calls (subprocess faked).

Run: python tests/test_engine_session_persist.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.blackboard import Blackboard
from hub.engine import Engine
from hub.executors import ClaudeCodeExecutor, ExecResult
from hub.models import EventBus, Plan, Task, TaskType


class _MockLikeExecutor:
    """A non-claude_code executor -- must never get a session_id written."""

    async def run(self, task, context, on_event=None):
        return ExecResult(ok=True, output="mock output", cost_usd=0.0)


class _NonClaudeCodeExecutorWithSessionId:
    """A non-claude_code executor whose result happens to carry a
    session_id -- proves the isinstance(executor, ClaudeCodeExecutor)
    guard is load-bearing, not just result.session_id truthiness."""

    async def run(self, task, context, on_event=None):
        return ExecResult(ok=True, output="mock output", cost_usd=0.0,
                          session_id="should-not-be-persisted")


class _FakeRouter:
    budget = {"max_usd_per_run": 1e9, "max_attempts_per_task": 2}

    def __init__(self, executor):
        self._executor = executor

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


def _script_subprocess(response: dict):
    async def fake_exec(*args, **kwargs):
        class _Proc:
            returncode = 0

            async def communicate(self_inner, input=None):
                return json.dumps(response).encode("utf-8"), b""
        return _Proc()
    asyncio.create_subprocess_exec = fake_exec


def test_claude_code_task_persists_agent_and_session():
    events_path, board_path = _fresh_paths("sess1")
    bus = EventBus(events_path)
    board = Blackboard(board_path, on_write=lambda e: None)
    plan = Plan(goal="test", tasks={})
    task = Task(id="t1", title="x", type=TaskType.CODING, prompt="p")
    plan.tasks["t1"] = task

    executor = ClaudeCodeExecutor(model="opus", workdir=".", stream=False)
    router = _FakeRouter(executor)
    engine = Engine(plan, router, bus, board, max_parallel=1)

    original = asyncio.create_subprocess_exec
    _script_subprocess({
        "type": "result", "is_error": False, "result": "done",
        "total_cost_usd": 0.02, "session_id": "sess-xyz",
        "usage": {"input_tokens": 5, "cache_read_input_tokens": 100,
                  "cache_creation_input_tokens": 0},
    })
    try:
        asyncio.run(engine._run_task(task))
    finally:
        asyncio.create_subprocess_exec = original
    bus.close()

    assert board.get("tasks/t1/agent") == "test-agent"
    assert board.get("tasks/t1/session_id") == "sess-xyz"
    ratio = board.get("tasks/t1/session_usage_ratio")
    assert ratio is not None and ratio > 0.0, f"expected a nonzero ratio, got {ratio}"

    _cleanup(events_path, board_path)
    print("PASS: test_claude_code_task_persists_agent_and_session")


def test_non_claude_code_task_persists_agent_only():
    events_path, board_path = _fresh_paths("sess2")
    bus = EventBus(events_path)
    board = Blackboard(board_path, on_write=lambda e: None)
    plan = Plan(goal="test", tasks={})
    task = Task(id="t1", title="x", type=TaskType.CODING, prompt="p")
    plan.tasks["t1"] = task

    router = _FakeRouter(_MockLikeExecutor())
    engine = Engine(plan, router, bus, board, max_parallel=1)

    asyncio.run(engine._run_task(task))
    bus.close()

    assert board.get("tasks/t1/agent") == "test-agent"
    assert board.get("tasks/t1/session_id") is None, \
        "non-claude_code executors must never get a session_id written"
    assert board.get("tasks/t1/session_usage_ratio") is None

    _cleanup(events_path, board_path)
    print("PASS: test_non_claude_code_task_persists_agent_only")


def test_claude_code_task_without_session_id_writes_no_session_key():
    """Defense-in-depth: even a claude_code executor's result might lack a
    session_id (e.g. the JSON-decode-failure text fallback path never
    sets it) -- must not write a None session_id."""
    events_path, board_path = _fresh_paths("sess3")
    bus = EventBus(events_path)
    board = Blackboard(board_path, on_write=lambda e: None)
    plan = Plan(goal="test", tasks={})
    task = Task(id="t1", title="x", type=TaskType.CODING, prompt="p")
    plan.tasks["t1"] = task

    executor = ClaudeCodeExecutor(model="opus", workdir=".", stream=False)
    router = _FakeRouter(executor)
    engine = Engine(plan, router, bus, board, max_parallel=1)

    original = asyncio.create_subprocess_exec

    async def fake_exec(*args, **kwargs):
        class _Proc:
            returncode = 0

            async def communicate(self_inner, input=None):
                return b"not valid json, plain text fallback", b""
        return _Proc()
    asyncio.create_subprocess_exec = fake_exec
    try:
        asyncio.run(engine._run_task(task))
    finally:
        asyncio.create_subprocess_exec = original
    bus.close()

    assert board.get("tasks/t1/agent") == "test-agent"
    assert board.get("tasks/t1/session_id") is None
    assert board.get("tasks/t1/session_usage_ratio") is None

    _cleanup(events_path, board_path)
    print("PASS: test_claude_code_task_without_session_id_writes_no_session_key")


def test_non_claude_code_executor_with_session_id_still_not_persisted():
    """Guards against a future refactor accidentally dropping the
    isinstance() check and widening session persistence to non-claude_code
    executors just because their result happens to carry a session_id."""
    events_path, board_path = _fresh_paths("sess4")
    bus = EventBus(events_path)
    board = Blackboard(board_path, on_write=lambda e: None)
    plan = Plan(goal="test", tasks={})
    task = Task(id="t1", title="x", type=TaskType.CODING, prompt="p")
    plan.tasks["t1"] = task

    router = _FakeRouter(_NonClaudeCodeExecutorWithSessionId())
    engine = Engine(plan, router, bus, board, max_parallel=1)

    asyncio.run(engine._run_task(task))
    bus.close()

    assert board.get("tasks/t1/agent") == "test-agent"
    assert board.get("tasks/t1/session_id") is None, \
        "isinstance(executor, ClaudeCodeExecutor) must gate this, not just result.session_id truthiness"
    assert board.get("tasks/t1/session_usage_ratio") is None

    _cleanup(events_path, board_path)
    print("PASS: test_non_claude_code_executor_with_session_id_still_not_persisted")


if __name__ == "__main__":
    test_claude_code_task_persists_agent_and_session()
    test_non_claude_code_task_persists_agent_only()
    test_claude_code_task_without_session_id_writes_no_session_key()
    test_non_claude_code_executor_with_session_id_still_not_persisted()
    print("\nAll engine-session-persist tests passed.")
