"""P0-1: failed attempts must carry their real cost, both in ExecResult
and in the agent_failed event. No framework -- plain assert, exit 1 on
failure.

Run: python tests/test_cost_attribution.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.executors import ClaudeCodeExecutor, ExecResult
from hub.models import EventBus, Task, TaskType


class _FakeProc:
    def __init__(self, stdout: bytes, returncode: int = 0):
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, b""


def _patch_subprocess(monkeypatch_target, stdout_json: dict, returncode: int = 0):
    async def fake_exec(*args, **kwargs):
        return _FakeProc(json.dumps(stdout_json).encode("utf-8"), returncode)
    asyncio.create_subprocess_exec = fake_exec


def test_claude_code_error_carries_real_cost():
    """The '429 Usage credits required' style error has total_cost_usd
    in the JSON body even though is_error=True -- the executor must read
    it instead of defaulting to 0.0."""
    original = asyncio.create_subprocess_exec
    _patch_subprocess(None, {
        "type": "result", "is_error": True, "api_error_status": 429,
        "duration_api_ms": 0,
        "result": "API Error: Usage credits required for 1M context",
        "total_cost_usd": 0.0173,  # synthetic non-zero to prove extraction works
    })
    try:
        task = Task(id="t1", title="x", type=TaskType.PLANNING, prompt="p")
        executor = ClaudeCodeExecutor(model="opus", workdir=".", stream=False)
        result = asyncio.run(executor.run(task, ""))
    finally:
        asyncio.create_subprocess_exec = original

    assert result.ok is False, "expected failure"
    assert result.cost_usd == 0.0173, f"expected cost extracted from JSON, got {result.cost_usd}"
    print("PASS: test_claude_code_error_carries_real_cost")


def test_claude_code_success_unaffected():
    """Regression: the success path must keep working exactly as before."""
    original = asyncio.create_subprocess_exec
    _patch_subprocess(None, {
        "type": "result", "is_error": False,
        "result": "hello", "total_cost_usd": 0.05,
    })
    try:
        task = Task(id="t1", title="x", type=TaskType.CODING, prompt="p")
        executor = ClaudeCodeExecutor(model="sonnet", workdir=".", stream=False)
        result = asyncio.run(executor.run(task, ""))
    finally:
        asyncio.create_subprocess_exec = original

    assert result.ok is True
    assert result.output == "hello"
    assert result.cost_usd == 0.05
    print("PASS: test_claude_code_success_unaffected")


def test_agent_failed_event_carries_cost_usd():
    """Engine must emit agent_failed with the same cost_usd it accumulated,
    not silently drop it."""
    from hub.blackboard import Blackboard
    from hub.engine import Engine
    from hub.models import Plan

    class _FakeFailingExecutor:
        async def run(self, task, context, on_event=None):
            return ExecResult(ok=False, error="boom", cost_usd=0.0173)

    class _FakeRouter:
        budget = {"max_usd_per_run": 2.0, "max_attempts_per_task": 1}

        def chain_for(self, task):
            return [("fake-agent", _FakeFailingExecutor())]

        def animal(self, name):
            return "cat"

        def is_paid_api(self, name):
            return False

    events_path = os.path.join(os.path.dirname(__file__), "_scratch_events.jsonl")
    board_path = os.path.join(os.path.dirname(__file__), "_scratch_board.jsonl")
    for p in (events_path, board_path):
        if os.path.exists(p):
            os.remove(p)

    bus = EventBus(events_path)
    board = Blackboard(board_path, on_write=lambda e: None)
    plan = Plan(goal="test", tasks={})
    task = Task(id="t1", title="x", type=TaskType.CODING, prompt="p")
    plan.tasks[task.id] = task

    engine = Engine(plan, _FakeRouter(), bus, board, max_parallel=1)
    asyncio.run(engine._run_task(task))
    bus.close()

    with open(events_path, encoding="utf-8") as f:
        lines = [json.loads(l) for l in f if l.strip()]
    failed_events = [e for e in lines if e["type"] == "agent_failed"]
    assert len(failed_events) == 1, f"expected 1 agent_failed event, got {len(failed_events)}"
    assert failed_events[0]["cost_usd"] == 0.0173, \
        f"expected cost_usd=0.0173 on agent_failed, got {failed_events[0].get('cost_usd')}"

    os.remove(events_path)
    if os.path.exists(board_path):
        os.remove(board_path)
    print("PASS: test_agent_failed_event_carries_cost_usd")


def test_claude_code_success_extracts_usage():
    """The success path must pass through the raw `usage` dict from
    claude -p's JSON (same field names, no translation)."""
    original = asyncio.create_subprocess_exec
    _patch_subprocess(None, {
        "type": "result", "is_error": False, "result": "hello",
        "total_cost_usd": 0.05,
        "usage": {"input_tokens": 10, "output_tokens": 5,
                  "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0},
    })
    try:
        task = Task(id="t1", title="x", type=TaskType.CODING, prompt="p")
        result = asyncio.run(ClaudeCodeExecutor(model="sonnet", workdir=".", stream=False).run(task, ""))
    finally:
        asyncio.create_subprocess_exec = original

    assert result.ok is True
    assert result.usage == {"input_tokens": 10, "output_tokens": 5,
                            "cache_read_input_tokens": 100,
                            "cache_creation_input_tokens": 0}, \
        f"expected raw usage dict passed through, got {result.usage}"
    print("PASS: test_claude_code_success_extracts_usage")


def test_task_completed_event_usage_field():
    """With a usage dict the task_completed event must carry it verbatim;
    with usage=None the event must NOT contain the key at all (no
    '\"usage\": null' polluting the log)."""
    from hub.blackboard import Blackboard
    from hub.engine import Engine
    from hub.models import Plan

    def run_one(usage):
        class _UsageExecutor:
            async def run(self, task, context, on_event=None):
                return ExecResult(ok=True, output="done", cost_usd=0.01, usage=usage)

        class _FakeRouter:
            budget = {"max_usd_per_run": 2.0, "max_attempts_per_task": 2}

            def chain_for(self, task):
                return [("fake", _UsageExecutor())]

            def animal(self, name):
                return "cat"

            def is_paid_api(self, name):
                return False

        events_path = os.path.join(os.path.dirname(__file__), "_scratch_events_u.jsonl")
        board_path = os.path.join(os.path.dirname(__file__), "_scratch_board_u.jsonl")
        for p in (events_path, board_path):
            if os.path.exists(p):
                os.remove(p)
        bus = EventBus(events_path)
        board = Blackboard(board_path, on_write=lambda e: None)
        plan = Plan(goal="test", tasks={})
        task = Task(id="t1", title="x", type=TaskType.CODING, prompt="p")
        plan.tasks[task.id] = task
        engine = Engine(plan, _FakeRouter(), bus, board, max_parallel=1)
        asyncio.run(engine._run_task(task))
        bus.close()
        with open(events_path, encoding="utf-8") as f:
            events = [json.loads(l) for l in f if l.strip()]
        for p in (events_path, board_path):
            if os.path.exists(p):
                os.remove(p)
        return [e for e in events if e["type"] == "task_completed"][0]

    e1 = run_one({"input_tokens": 7, "output_tokens": 3})
    assert e1["usage"] == {"input_tokens": 7, "output_tokens": 3}, \
        f"expected usage carried verbatim, got {e1.get('usage')}"
    e2 = run_one(None)
    assert "usage" not in e2, f"usage=None must omit the key, got {e2}"
    print("PASS: test_task_completed_event_usage_field")


if __name__ == "__main__":
    test_claude_code_error_carries_real_cost()
    test_claude_code_success_unaffected()
    test_agent_failed_event_carries_cost_usd()
    test_claude_code_success_extracts_usage()
    test_task_completed_event_usage_field()
    print("\nAll P0-1 tests passed.")
