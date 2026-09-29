"""Budget redefinition: max_usd_per_run gates ONLY pay-as-you-go (openrouter)
spend. Subscription agents (claude_code) must run unbounded. No framework
-- plain assert, exit 1 on failure.

Run: python tests/test_paid_budget.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.executors import ExecResult
from hub.models import EventBus, Plan, Task, TaskType


def _make_router(paid_kinds: dict):
    """paid_kinds: {agent_name: is_paid_bool}"""
    class _FakeRouter:
        budget = {"max_usd_per_run": 1.0, "max_attempts_per_task": 2}

        def __init__(self):
            self._circuit_open = set()

        def is_paid_api(self, name):
            return paid_kinds.get(name, False)

        def open_circuit(self, name):
            self._circuit_open.add(name)

        def animal(self, name):
            return "cat"

    return _FakeRouter()


def test_subscription_agent_never_budget_gated():
    """A claude_code-style (non-paid) agent must keep running even after
    total spend blows way past max_usd_per_run -- only paid agents are
    gated."""
    from hub.blackboard import Blackboard
    from hub.engine import Engine

    class _ExpensiveSubscriptionExecutor:
        async def run(self, task, context, on_event=None):
            return ExecResult(ok=True, output="done", cost_usd=5.0)  # way over the $1 budget

    router = _make_router({"cc-sonnet": False})
    router.chain_for = lambda task: [("cc-sonnet", _ExpensiveSubscriptionExecutor())]

    events_path = os.path.join(os.path.dirname(__file__), "_scratch_events6.jsonl")
    board_path = os.path.join(os.path.dirname(__file__), "_scratch_board6.jsonl")
    for p in (events_path, board_path):
        if os.path.exists(p):
            os.remove(p)

    bus = EventBus(events_path)
    board = Blackboard(board_path, on_write=lambda e: None)
    plan = Plan(goal="test", tasks={})

    # run two tasks back to back through the same expensive subscription agent
    for i in range(2):
        task = Task(id=f"t{i}", title="x", type=TaskType.CODING, prompt="p")
        plan.tasks[task.id] = task
        engine = Engine(plan, router, bus, board, max_parallel=1)
        engine.total_cost = 5.0 * i  # simulate accumulated spend from prior tasks
        engine.paid_cost = 0.0
        asyncio.run(engine._run_task(task))
        assert task.status.value == "done", \
            f"task {i} should complete even with total_cost={engine.total_cost} >> budget"

    bus.close()
    os.remove(events_path)
    if os.path.exists(board_path):
        os.remove(board_path)
    print("PASS: test_subscription_agent_never_budget_gated")


def test_paid_agent_gated_and_circuit_broken_on_budget():
    """A paid (openrouter-style) agent must be skipped -- without ever being
    invoked -- once paid_cost >= budget, and get circuit-broken so later
    tasks don't re-check it."""
    from hub.blackboard import Blackboard
    from hub.engine import Engine

    calls = {"paid": 0, "fallback": 0}

    class _PaidExecutor:
        async def run(self, task, context, on_event=None):
            calls["paid"] += 1
            return ExecResult(ok=True, output="paid result", cost_usd=0.5)

    class _FallbackExecutor:
        async def run(self, task, context, on_event=None):
            calls["fallback"] += 1
            return ExecResult(ok=True, output="fallback result", cost_usd=0.0)

    router = _make_router({"or-cheap": True, "cc-sonnet": False})
    router.chain_for = lambda task: [("or-cheap", _PaidExecutor()),
                                     ("cc-sonnet", _FallbackExecutor())]

    events_path = os.path.join(os.path.dirname(__file__), "_scratch_events7.jsonl")
    board_path = os.path.join(os.path.dirname(__file__), "_scratch_board7.jsonl")
    for p in (events_path, board_path):
        if os.path.exists(p):
            os.remove(p)

    bus = EventBus(events_path)
    board = Blackboard(board_path, on_write=lambda e: None)
    plan = Plan(goal="test", tasks={})
    task = Task(id="t1", title="x", type=TaskType.CODING, prompt="p")
    plan.tasks[task.id] = task

    engine = Engine(plan, router, bus, board, max_parallel=1)
    engine.paid_cost = 1.0  # already at the $1.0 budget cap before this task starts
    asyncio.run(engine._run_task(task))
    bus.close()

    assert calls["paid"] == 0, "budget-exhausted paid agent must never actually be invoked"
    assert calls["fallback"] == 1, "the non-paid fallback must still run"
    assert task.status.value == "done"
    assert "or-cheap" in router._circuit_open, "paid agent must be circuit-broken once budget is spent"

    with open(events_path, encoding="utf-8") as f:
        events = [json.loads(l) for l in f if l.strip()]
    failed = [e for e in events if e["type"] == "agent_failed" and e["agent"] == "or-cheap"]
    assert len(failed) == 1
    assert failed[0]["error"] == "paid API budget exceeded"
    assert failed[0]["cost_usd"] == 0.0, "the skipped attempt itself must cost nothing"

    os.remove(events_path)
    if os.path.exists(board_path):
        os.remove(board_path)
    print("PASS: test_paid_agent_gated_and_circuit_broken_on_budget")


if __name__ == "__main__":
    test_subscription_agent_never_budget_gated()
    test_paid_agent_gated_and_circuit_broken_on_budget()
    print("\nAll paid-budget tests passed.")
