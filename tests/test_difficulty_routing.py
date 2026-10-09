"""Difficulty-tiered routing + CodeBuddy executor tests. No framework --
plain assert, exit 1 on failure. Zero real LLM/subprocess calls unless
explicitly noted.

Run: python tests/test_difficulty_routing.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.models import Plan


def test_plan_from_json_reads_difficulty_and_specialty():
    data = {"goal": "g", "tasks": [
        {"id": "t1", "title": "x", "type": "writing", "prompt": "p",
         "difficulty": "easy", "specialty": "frontend"},
    ]}
    plan = Plan.from_json(data)
    t = plan.tasks["t1"]
    assert t.difficulty == "easy"
    assert t.specialty == "frontend"
    print("PASS: test_plan_from_json_reads_difficulty_and_specialty")


def test_plan_from_json_defaults_difficulty_and_specialty():
    """Old plan JSON without these fields must still load -- defaults to
    medium difficulty, no specialty preference. Regression guard for
    example_plan.json and any hand-written plan predating this feature."""
    data = {"goal": "g", "tasks": [
        {"id": "t1", "title": "x", "type": "writing", "prompt": "p"},
    ]}
    plan = Plan.from_json(data)
    t = plan.tasks["t1"]
    assert t.difficulty == "medium"
    assert t.specialty is None
    print("PASS: test_plan_from_json_defaults_difficulty_and_specialty")


def test_easy_task_routes_to_economy_tier():
    from hub.models import Task, TaskType
    from hub.router import Router

    router = Router("hub/capability_matrix.yaml", workdir=".", mock_all=True)
    task = Task(id="t1", title="x", type=TaskType.WRITING, prompt="p", difficulty="easy")
    names = [n for n, _ in router.chain_for(task)]
    assert names == ["cc-hy3", "cc-deepseek-cb", "cc-haiku"], f"unexpected economy chain: {names}"
    print("PASS: test_easy_task_routes_to_economy_tier")


def test_easy_tasks_and_summaries_survive_without_workbuddy():
    """B5 (2026-09-25): the economy tier was all codebuddy. Without WorkBuddy
    (its auto-detect only knows the macOS install) preflight circuit-breaks
    both, chain_for() comes back empty, and every easy writing/analysis/
    light task -- plus every summary, which hub/summarizer.py routes through
    this same tier as an easy LIGHT task -- fails outright."""
    from hub.models import Task, TaskType
    from hub.router import Router

    router = Router("hub/capability_matrix.yaml", workdir=".")
    for name in router.codebuddy_agents():        # what preflight does on such a machine
        router.open_circuit(name)
    for task_type in (TaskType.WRITING, TaskType.ANALYSIS, TaskType.LIGHT):
        task = Task(id="t1", title="x", type=task_type, prompt="p", difficulty="easy")
        names = [n for n, _ in router.chain_for(task)]
        assert names, f"{task_type.value}/easy has no agent left without WorkBuddy"
        assert all(router.kind(n) == "claude_code" for n in names), names
    print("PASS: test_easy_tasks_and_summaries_survive_without_workbuddy")


def test_medium_and_hard_tasks_route_to_mid_tier():
    from hub.models import Task, TaskType
    from hub.router import Router

    router = Router("hub/capability_matrix.yaml", workdir=".", mock_all=True)
    for difficulty in ("medium", "hard"):
        task = Task(id="t1", title="x", type=TaskType.ANALYSIS, prompt="p",
                    difficulty=difficulty)
        names = [n for n, _ in router.chain_for(task)]
        assert names == ["cc-sonnet", "cc-kimi", "cc-minimax"], \
            f"difficulty={difficulty}: unexpected mid chain: {names}"
    print("PASS: test_medium_and_hard_tasks_route_to_mid_tier")


def test_specialty_match_sorted_to_front_stably():
    from hub.models import Task, TaskType
    from hub.router import Router

    router = Router("hub/capability_matrix.yaml", workdir=".", mock_all=True)
    task = Task(id="t1", title="x", type=TaskType.LIGHT, prompt="p",
               difficulty="hard", specialty="frontend")
    names = [n for n, _ in router.chain_for(task)]
    assert names == ["cc-kimi", "cc-sonnet", "cc-minimax"], \
        f"cc-kimi (specialty=frontend) must be sorted first, got {names}"
    print("PASS: test_specialty_match_sorted_to_front_stably")


def test_no_specialty_preserves_configured_order():
    from hub.models import Task, TaskType
    from hub.router import Router

    router = Router("hub/capability_matrix.yaml", workdir=".", mock_all=True)
    task = Task(id="t1", title="x", type=TaskType.LIGHT, prompt="p", difficulty="hard")
    names = [n for n, _ in router.chain_for(task)]
    assert names == ["cc-sonnet", "cc-kimi", "cc-minimax"], \
        f"no specialty preference must keep yaml order, got {names}"
    print("PASS: test_no_specialty_preserves_configured_order")


def test_planning_review_coding_image_routes_ignore_difficulty():
    """Even if a non-tiered task type is stamped with an easy/hard
    difficulty, it must keep using its fixed routes: chain -- difficulty
    only affects writing/analysis/light. This is the easiest thing to
    accidentally break when adding the tiered branch."""
    from hub.models import Task, TaskType
    from hub.router import Router

    router = Router("hub/capability_matrix.yaml", workdir=".", mock_all=True)
    cases = [
        (TaskType.PLANNING, ["cc-opus", "cc-sonnet", "codex-local", "cc-glm-strong"]),
        (TaskType.REVIEW, ["cc-sonnet", "codex-local", "cc-glm-strong"]),
        (TaskType.CODING, ["cc-sonnet", "codex-local", "or-deepseek", "or-glm", "dsh-pro"]),
        (TaskType.IMAGE, ["mock"]),
    ]
    for task_type, expected in cases:
        task = Task(id="t1", title="x", type=task_type, prompt="p", difficulty="easy")
        names = [n for n, _ in router.chain_for(task)]
        assert names == expected, \
            f"{task_type}: difficulty must not affect fixed route, got {names}"
    print("PASS: test_planning_review_coding_image_routes_ignore_difficulty")


def test_manager_route_uses_cc_kimi_fallback():
    """The manager route's second candidate changed from the (balance-less)
    or-deepseek to cc-kimi as part of this feature."""
    from hub.models import Task, TaskType
    from hub.router import Router

    router = Router("hub/capability_matrix.yaml", workdir=".", mock_all=True)
    task = Task(id="m", title="clarify", type=TaskType.MANAGER, prompt="p")
    names = [n for n, _ in router.chain_for(task)]
    assert names == ["cc-sonnet-mgr", "cc-kimi"], f"unexpected manager chain: {names}"
    print("PASS: test_manager_route_uses_cc_kimi_fallback")


def test_codebuddy_agents_lists_only_codebuddy_kind():
    """codebuddy_agents() must reflect each agent's real declared kind --
    unlike chain_for()'s route-list assertions above, this needs a REAL
    (non-mock_all) Router, because mock_all=True overwrites every agent's
    recorded kind to "mock" (see hub/router.py's mock_all branch). This
    is safe: executor constructors only store config, they never touch
    the network or spawn a process until .run() is called -- and by this
    task build_executor() knows how to construct a CodeBuddyExecutor, so
    a real (non-mocked) Router can be built without any real subprocess."""
    from hub.router import Router

    router = Router("hub/capability_matrix.yaml", workdir=".", mock_all=False)
    names = set(router.codebuddy_agents())
    assert names == {"cc-glm-strong", "cc-kimi", "cc-minimax", "cc-hy3", "cc-deepseek-cb"}, \
        f"unexpected codebuddy agent set: {names}"
    print("PASS: test_codebuddy_agents_lists_only_codebuddy_kind")


def test_engine_accumulates_task_cost_credits():
    """A successful codebuddy-kind task run must add to Task.cost_credits,
    Engine.total_credits, and the task_completed event -- mirroring how
    cost_usd already works, but on a separate ledger that's never mixed in."""
    import asyncio
    import json

    from hub.blackboard import Blackboard
    from hub.engine import Engine
    from hub.executors import ExecResult
    from hub.models import EventBus, Plan, Task, TaskType

    class _CreditExecutor:
        async def run(self, task, context, on_event=None):
            return ExecResult(ok=True, output="done", cost_credits=0.12)

    class _FakeRouter:
        budget = {"max_usd_per_run": 2.0, "max_attempts_per_task": 2}

        def chain_for(self, task):
            return [("cc-hy3", _CreditExecutor())]

        def animal(self, name):
            return "cat"

        def is_paid_api(self, name):
            return False

    events_path = os.path.join(os.path.dirname(__file__), "_scratch_events_credit.jsonl")
    board_path = os.path.join(os.path.dirname(__file__), "_scratch_board_credit.jsonl")
    for p in (events_path, board_path):
        if os.path.exists(p):
            os.remove(p)

    bus = EventBus(events_path)
    board = Blackboard(board_path, on_write=lambda e: None)
    plan = Plan(goal="test", tasks={})
    task = Task(id="t1", title="x", type=TaskType.WRITING, prompt="p", difficulty="easy")
    plan.tasks[task.id] = task

    engine = Engine(plan, _FakeRouter(), bus, board, max_parallel=1)
    asyncio.run(engine._run_task(task))
    bus.close()

    assert task.cost_credits == 0.12
    assert engine.total_credits == 0.12
    assert task.cost_usd == 0.0

    with open(events_path, encoding="utf-8") as f:
        events = [json.loads(l) for l in f if l.strip()]
    completed = [e for e in events if e["type"] == "task_completed"][0]
    assert completed["cost_credits"] == 0.12
    assert completed["cost_usd"] == 0.0

    os.remove(events_path)
    if os.path.exists(board_path):
        os.remove(board_path)
    print("PASS: test_engine_accumulates_task_cost_credits")


if __name__ == "__main__":
    test_plan_from_json_reads_difficulty_and_specialty()
    test_plan_from_json_defaults_difficulty_and_specialty()
    test_easy_task_routes_to_economy_tier()
    test_easy_tasks_and_summaries_survive_without_workbuddy()
    test_medium_and_hard_tasks_route_to_mid_tier()
    test_specialty_match_sorted_to_front_stably()
    test_no_specialty_preserves_configured_order()
    test_planning_review_coding_image_routes_ignore_difficulty()
    test_manager_route_uses_cc_kimi_fallback()
    test_codebuddy_agents_lists_only_codebuddy_kind()
    test_engine_accumulates_task_cost_credits()
    print("\nAll difficulty-routing tests passed.")
