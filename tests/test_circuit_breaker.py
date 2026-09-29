"""P0-2: error_kind classification, transient retry, and the circuit
breaker. No framework -- plain assert, exit 1 on failure.

Run: python tests/test_circuit_breaker.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.executors import ExecResult
from hub.models import EventBus, Plan, Task, TaskType
from hub.router import Router


def test_classification_unavailable_vs_transient():
    """Real error shapes captured from acceptance testing: the
    '429 + duration_api_ms==0 + credits required' case is structural,
    a genuine mid-stream 429 with output already flowing is not."""
    from hub.executors import _UNAVAILABLE_MARKERS

    unavailable_text = "API Error: Usage credits required for 1M context"
    assert any(m in unavailable_text.lower() for m in _UNAVAILABLE_MARKERS), \
        "known structural error text must match an unavailable marker"
    print("PASS: test_classification_unavailable_vs_transient")


def test_router_circuit_breaker_skips_agent():
    """Once open_circuit() is called for an agent, chain_for() must
    exclude it for the rest of this Router's lifetime (= this run)."""
    router = Router("hub/capability_matrix.yaml", workdir=".", mock_all=True)
    task = Task(id="t1", title="x", type=TaskType.PLANNING, prompt="p")

    before = [name for name, _ in router.chain_for(task)]
    assert "cc-opus" in before, f"expected cc-opus in planning chain, got {before}"

    router.open_circuit("cc-opus")
    after = [name for name, _ in router.chain_for(task)]
    assert "cc-opus" not in after, f"cc-opus should be filtered out, got {after}"
    assert "cc-sonnet" in after, "the rest of the chain must still work"
    print("PASS: test_router_circuit_breaker_skips_agent")


def test_engine_opens_circuit_on_unavailable():
    """An 'unavailable' failure must (a) not be retried, (b) open the
    circuit on the router, (c) emit agent_circuit_open, (d) let the next
    agent in the chain pick up the task."""
    from hub.blackboard import Blackboard
    from hub.engine import Engine

    calls = {"broken": 0, "backup": 0}

    class _BrokenExecutor:
        async def run(self, task, context, on_event=None):
            calls["broken"] += 1
            return ExecResult(ok=False, error="Usage credits required for 1M context",
                              error_kind="unavailable")

    class _BackupExecutor:
        async def run(self, task, context, on_event=None):
            calls["backup"] += 1
            return ExecResult(ok=True, output="done", cost_usd=0.01)

    class _FakeRouter:
        budget = {"max_usd_per_run": 2.0, "max_attempts_per_task": 2}

        def __init__(self):
            self._circuit_open = set()

        def chain_for(self, task):
            chain = [("broken-agent", _BrokenExecutor()), ("backup-agent", _BackupExecutor())]
            return [(n, e) for n, e in chain if n not in self._circuit_open]

        def animal(self, name):
            return "cat"

        def open_circuit(self, name):
            self._circuit_open.add(name)

        def is_paid_api(self, name):
            return False

    events_path = os.path.join(os.path.dirname(__file__), "_scratch_events2.jsonl")
    board_path = os.path.join(os.path.dirname(__file__), "_scratch_board2.jsonl")
    for p in (events_path, board_path):
        if os.path.exists(p):
            os.remove(p)

    bus = EventBus(events_path)
    board = Blackboard(board_path, on_write=lambda e: None)
    plan = Plan(goal="test", tasks={})
    task = Task(id="t1", title="x", type=TaskType.CODING, prompt="p")
    plan.tasks[task.id] = task
    router = _FakeRouter()

    engine = Engine(plan, router, bus, board, max_parallel=1)
    asyncio.run(engine._run_task(task))
    bus.close()

    assert calls["broken"] == 1, f"broken agent should be tried exactly once, got {calls['broken']}"
    assert calls["backup"] == 1, f"backup agent should pick up the task, got {calls['backup']}"
    assert task.status.value == "done"
    assert "broken-agent" in router._circuit_open, "circuit must be open after unavailable failure"

    import json
    with open(events_path, encoding="utf-8") as f:
        types = [json.loads(l)["type"] for l in f if l.strip()]
    assert "agent_circuit_open" in types, f"expected agent_circuit_open event, got {types}"

    os.remove(events_path)
    if os.path.exists(board_path):
        os.remove(board_path)
    print("PASS: test_engine_opens_circuit_on_unavailable")


def test_engine_retries_transient_without_consuming_chain_slot():
    """A 'transient' failure must retry the SAME agent (not move to the
    next in the chain) and must not show up as a second chain attempt."""
    from hub.blackboard import Blackboard
    from hub.engine import Engine

    calls = {"flaky": 0}

    class _FlakyExecutor:
        async def run(self, task, context, on_event=None):
            calls["flaky"] += 1
            if calls["flaky"] < 2:
                return ExecResult(ok=False, error="connection reset", error_kind="transient")
            return ExecResult(ok=True, output="done", cost_usd=0.02)

    class _FakeRouter:
        budget = {"max_usd_per_run": 2.0, "max_attempts_per_task": 2}

        def __init__(self):
            self._circuit_open = set()

        def chain_for(self, task):
            return [("flaky-agent", _FlakyExecutor())]

        def animal(self, name):
            return "cat"

        def open_circuit(self, name):
            self._circuit_open.add(name)

        def is_paid_api(self, name):
            return False

    events_path = os.path.join(os.path.dirname(__file__), "_scratch_events3.jsonl")
    board_path = os.path.join(os.path.dirname(__file__), "_scratch_board3.jsonl")
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

    assert calls["flaky"] == 2, f"expected 1 retry (2 total calls), got {calls['flaky']}"
    assert task.status.value == "done"
    assert task.attempts == 1, f"transient retry must not consume a chain slot, got attempts={task.attempts}"

    os.remove(events_path)
    if os.path.exists(board_path):
        os.remove(board_path)
    print("PASS: test_engine_retries_transient_without_consuming_chain_slot")


if __name__ == "__main__":
    test_classification_unavailable_vs_transient()
    test_router_circuit_breaker_skips_agent()
    test_engine_opens_circuit_on_unavailable()
    test_engine_retries_transient_without_consuming_chain_slot()
    print("\nAll P0-2 tests passed.")
