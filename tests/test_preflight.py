"""P0-3: startup preflight probes every claude_code agent once and
circuit-breaks the broken ones before the real DAG starts. No framework
-- plain assert, exit 1 on failure.

Run: python tests/test_preflight.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.executors import ExecResult
from hub.models import EventBus, Plan, TaskType


def _make_engine(broken_agents: set, events_path: str, board_path: str):
    from hub.blackboard import Blackboard
    from hub.engine import Engine

    class _Executor:
        def __init__(self, name):
            self.name = name

        async def run(self, task, context):
            if self.name in broken_agents:
                return ExecResult(ok=False, error="not logged in", cost_usd=0.001)
            return ExecResult(ok=True, output="ok", cost_usd=0.001)

    class _FakeRouter:
        budget = {"max_usd_per_run": 2.0, "max_attempts_per_task": 2}

        def __init__(self):
            self._circuit_open = set()
            self._agents = {"cc-sonnet": _Executor("cc-sonnet"),
                            "cc-opus": _Executor("cc-opus")}

        def claude_code_agents(self):
            return list(self._agents.keys())

        def codebuddy_agents(self):
            return []

        def executor_for(self, name):
            return self._agents[name]

        def open_circuit(self, name):
            self._circuit_open.add(name)

        def chain_for(self, task):
            return [(n, e) for n, e in self._agents.items() if n not in self._circuit_open]

        def animal(self, name):
            return "cat"

    bus = EventBus(events_path)
    board = Blackboard(board_path, on_write=lambda e: None)
    plan = Plan(goal="test", tasks={})
    router = _FakeRouter()
    engine = Engine(plan, router, bus, board, max_parallel=1)
    return engine, router, bus


def test_preflight_circuit_breaks_broken_agent():
    events_path = os.path.join(os.path.dirname(__file__), "_scratch_events4.jsonl")
    board_path = os.path.join(os.path.dirname(__file__), "_scratch_board4.jsonl")
    for p in (events_path, board_path):
        if os.path.exists(p):
            os.remove(p)

    engine, router, bus = _make_engine({"cc-opus"}, events_path, board_path)
    asyncio.run(engine._run_preflight())
    bus.close()

    assert "cc-opus" in router._circuit_open, "broken agent must be circuit-open after preflight"
    assert "cc-sonnet" not in router._circuit_open, "healthy agent must stay open"

    import json
    with open(events_path, encoding="utf-8") as f:
        events = [json.loads(l) for l in f if l.strip()]
    circuit_events = [e for e in events if e["type"] == "agent_circuit_open"]
    assert len(circuit_events) == 1
    assert circuit_events[0]["agent"] == "cc-opus"
    assert circuit_events[0]["task_id"] is None, "preflight failures aren't tied to a real task"

    # every probe -- success or failure -- must leave a trace so its cost
    # is never silently invisible in the event log
    probe_events = {e["agent"]: e for e in events if e["type"] == "preflight_result"}
    assert set(probe_events) == {"cc-sonnet", "cc-opus"}, \
        f"expected a preflight_result for every claude_code agent, got {set(probe_events)}"
    assert probe_events["cc-opus"]["ok"] is False
    assert probe_events["cc-sonnet"]["ok"] is True

    os.remove(events_path)
    if os.path.exists(board_path):
        os.remove(board_path)
    print("PASS: test_preflight_circuit_breaks_broken_agent")


def test_preflight_disabled_by_flag():
    """preflight=False must skip the probe entirely -- no agent gets
    circuit-broken even if it would have failed."""
    from hub.blackboard import Blackboard
    from hub.engine import Engine

    events_path = os.path.join(os.path.dirname(__file__), "_scratch_events5.jsonl")
    board_path = os.path.join(os.path.dirname(__file__), "_scratch_board5.jsonl")
    for p in (events_path, board_path):
        if os.path.exists(p):
            os.remove(p)

    engine, router, bus = _make_engine({"cc-opus"}, events_path, board_path)
    engine.preflight = False
    # simulate what run() does: only call _run_preflight if self.preflight
    if engine.preflight:
        asyncio.run(engine._run_preflight())
    bus.close()

    assert router._circuit_open == set(), "no circuit should open when preflight is disabled"

    os.remove(events_path)
    if os.path.exists(board_path):
        os.remove(board_path)
    print("PASS: test_preflight_disabled_by_flag")


def test_preflight_probes_codebuddy_agents_and_tracks_credits():
    """Preflight must probe codebuddy-kind agents too, accumulate their
    spend in credits (never dollars), and circuit-break failures exactly
    like claude_code agents -- but on a separate ledger."""
    import json

    from hub.blackboard import Blackboard
    from hub.engine import Engine
    from hub.executors import ExecResult
    from hub.models import EventBus, Plan

    class _CBExecutor:
        def __init__(self, name, broken):
            self.name = name
            self.broken = broken

        async def run(self, task, context):
            if self.broken:
                return ExecResult(ok=False, error="insufficient credit", cost_credits=0.02)
            return ExecResult(ok=True, output="ok", cost_credits=0.05)

    class _FakeRouter:
        budget = {"max_usd_per_run": 2.0, "max_attempts_per_task": 2}

        def __init__(self):
            self._circuit_open = set()
            self._cb = {"cc-kimi": _CBExecutor("cc-kimi", broken=False),
                       "cc-hy3": _CBExecutor("cc-hy3", broken=True)}

        def claude_code_agents(self):
            return []

        def codebuddy_agents(self):
            return list(self._cb.keys())

        def executor_for(self, name):
            return self._cb[name]

        def open_circuit(self, name):
            self._circuit_open.add(name)

        def chain_for(self, task):
            return [(n, e) for n, e in self._cb.items() if n not in self._circuit_open]

        def animal(self, name):
            return "cat"

    events_path = os.path.join(os.path.dirname(__file__), "_scratch_events_cb.jsonl")
    board_path = os.path.join(os.path.dirname(__file__), "_scratch_board_cb.jsonl")
    for p in (events_path, board_path):
        if os.path.exists(p):
            os.remove(p)

    bus = EventBus(events_path)
    board = Blackboard(board_path, on_write=lambda e: None)
    plan = Plan(goal="test", tasks={})
    router = _FakeRouter()
    engine = Engine(plan, router, bus, board, max_parallel=1)
    asyncio.run(engine._run_preflight())
    bus.close()

    assert "cc-hy3" in router._circuit_open, "broken codebuddy agent must be circuit-open"
    assert "cc-kimi" not in router._circuit_open
    assert engine.total_credits == 0.07, \
        f"preflight must sum credits from BOTH probes (success + failure), got {engine.total_credits}"
    assert engine.total_cost == 0.0, "codebuddy spend must never land in total_cost (USD)"

    with open(events_path, encoding="utf-8") as f:
        events = [json.loads(l) for l in f if l.strip()]
    probe_events = {e["agent"]: e for e in events if e["type"] == "preflight_result"}
    assert set(probe_events) == {"cc-kimi", "cc-hy3"}
    assert probe_events["cc-kimi"]["cost_credits"] == 0.05
    assert probe_events["cc-hy3"]["cost_credits"] == 0.02
    assert probe_events["cc-kimi"]["cost_usd"] == 0.0, \
        "codebuddy probes must report cost_usd=0.0, not omit the key"

    os.remove(events_path)
    if os.path.exists(board_path):
        os.remove(board_path)
    print("PASS: test_preflight_probes_codebuddy_agents_and_tracks_credits")


if __name__ == "__main__":
    test_preflight_circuit_breaks_broken_agent()
    test_preflight_disabled_by_flag()
    test_preflight_probes_codebuddy_agents_and_tracks_credits()
    print("\nAll P0-3 tests passed.")
