"""Codex profile, route ordering, gate classification, and preflight wiring.

Run: python3 tests/test_codex_routing.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.blackboard import Blackboard
from hub.engine import Engine
from hub.executors import ExecResult
from hub.models import EventBus, Plan, Task, TaskType
from hub.router import PAID_API_KINDS, Router

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MATRIX = os.path.join(ROOT, "hub", "capability_matrix.yaml")


def test_codex_profile_metadata_and_no_tier():
    router = Router(MATRIX, workdir=".", mock_all=False)
    assert router.kind("codex-local") == "codex"
    assert router.codex_agents() == ["codex-local"]
    assert router.tier_of("codex-local") is None
    assert "codex" not in PAID_API_KINDS
    print("PASS: test_codex_profile_metadata_and_no_tier")


def test_codex_route_order_is_between_claude_and_other_fallbacks():
    with open(MATRIX, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    expected = {
        "planning": ("cc-sonnet", "codex-local", "cc-glm-strong"),
        "coding": ("cc-sonnet", "codex-local", "or-deepseek"),
        "review": ("cc-sonnet", "codex-local", "cc-glm-strong"),
    }
    for route, (before, codex, after) in expected.items():
        names = cfg["routes"][route]
        assert names.index(before) < names.index(codex) < names.index(after), \
            f"bad {route} order: {names}"
    for names in cfg.get("tiers", {}).values():
        assert "codex-local" not in names
    print("PASS: test_codex_route_order_is_between_claude_and_other_fallbacks")


def test_engine_preflight_probes_and_circuit_breaks_codex():
    class _Executor:
        async def run_preflight(self, task, context, on_event=None):
            return ExecResult(ok=False, error="not logged in", error_kind="unavailable",
                              usage={"input_tokens": 1})

        async def run(self, task, context, on_event=None):
            raise AssertionError("Codex preflight must use the read-only runner")

    class _Router:
        budget = {"max_usd_per_run": 2.0, "max_attempts_per_task": 2}

        def __init__(self):
            self.opened = set()
            self.executor = _Executor()

        def claude_code_agents(self): return []
        def codex_agents(self): return ["codex-local"]
        def codebuddy_agents(self): return []
        def executor_for(self, _name): return self.executor
        def open_circuit(self, name): self.opened.add(name)

    tmp = tempfile.mkdtemp(prefix="ah_codex_preflight_")
    try:
        bus = EventBus(os.path.join(tmp, "events.jsonl"))
        board = Blackboard(os.path.join(tmp, "board.jsonl"), on_write=lambda _e: None)
        router = _Router()
        engine = Engine(Plan(goal="g", tasks={}), router, bus, board)
        asyncio.run(engine._run_preflight())
        bus.close()
        assert router.opened == {"codex-local"}
        with open(os.path.join(tmp, "events.jsonl"), encoding="utf-8") as f:
            events = [json.loads(line) for line in f if line.strip()]
        probes = [e for e in events if e["type"] == "preflight_result"]
        assert len(probes) == 1 and probes[0]["agent"] == "codex-local"
        assert probes[0]["cost_usd"] == 0.0
        assert probes[0]["usage"] == {"input_tokens": 1}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS: test_engine_preflight_probes_and_circuit_breaks_codex")


if __name__ == "__main__":
    test_codex_profile_metadata_and_no_tier()
    test_codex_route_order_is_between_claude_and_other_fallbacks()
    test_engine_preflight_probes_and_circuit_breaks_codex()
    print("\nAll Codex routing tests passed.")
