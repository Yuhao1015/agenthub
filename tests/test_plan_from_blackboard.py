"""Plan.from_blackboard() -- reconstructs a DAG from tasks/*/spec entries
already on the board (written by every run regardless of --plan/--goal).
This is what forced rerun (main.py --resume with neither --goal nor
--plan) uses, since goal-driven runs never persist their Planner-generated
plan to a reusable file. No framework -- plain assert, exit 1 on failure.

Run: python tests/test_plan_from_blackboard.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.blackboard import Blackboard
from hub.models import Plan, TaskType


def _fresh_board():
    tmp = tempfile.mkdtemp()
    return Blackboard(os.path.join(tmp, "board.jsonl"), on_write=lambda e: None)


def test_reconstructs_goal_tasks_and_dependencies():
    board = _fresh_board()
    board.put("goal/statement", "build a thing", author="system", kind="goal")
    board.put("tasks/a/spec",
              {"title": "Task A", "type": "coding", "prompt": "do A", "depends_on": []},
              author="system", kind="spec")
    board.put("tasks/b/spec",
              {"title": "Task B", "type": "review", "prompt": "review A", "depends_on": ["a"]},
              author="system", kind="spec")

    plan = Plan.from_blackboard(board)

    assert plan.goal == "build a thing"
    assert set(plan.tasks) == {"a", "b"}
    assert plan.tasks["a"].type == TaskType.CODING
    assert plan.tasks["a"].depends_on == []
    assert plan.tasks["b"].type == TaskType.REVIEW
    assert plan.tasks["b"].depends_on == ["a"]
    assert plan.tasks["b"].prompt == "review A"
    print("PASS: test_reconstructs_goal_tasks_and_dependencies")


def test_reconstructs_difficulty_and_specialty():
    board = _fresh_board()
    board.put("tasks/a/spec",
              {"title": "Task A", "type": "light", "prompt": "p", "depends_on": [],
               "difficulty": "easy", "specialty": "frontend"},
              author="system", kind="spec")

    plan = Plan.from_blackboard(board)

    assert plan.tasks["a"].difficulty == "easy"
    assert plan.tasks["a"].specialty == "frontend"
    print("PASS: test_reconstructs_difficulty_and_specialty")


def test_missing_difficulty_specialty_defaults_gracefully():
    """Specs written before this feature (or by a version that forgot the
    fields) must still reconstruct -- default to medium/None, same as
    Plan.from_json's own defaulting."""
    board = _fresh_board()
    board.put("tasks/a/spec",
              {"title": "Task A", "type": "coding", "prompt": "p", "depends_on": []},
              author="system", kind="spec")

    plan = Plan.from_blackboard(board)

    assert plan.tasks["a"].difficulty == "medium"
    assert plan.tasks["a"].specialty is None
    print("PASS: test_missing_difficulty_specialty_defaults_gracefully")


def test_empty_board_yields_empty_plan():
    board = _fresh_board()
    plan = Plan.from_blackboard(board)
    assert plan.tasks == {}
    assert plan.goal == ""
    print("PASS: test_empty_board_yields_empty_plan")


def test_ignores_non_spec_keys_under_tasks_prefix():
    """tasks/{id}/output, tasks/{id}/status etc. share the tasks/ prefix
    with spec -- only /spec-suffixed keys may become a Task."""
    board = _fresh_board()
    board.put("tasks/a/spec",
              {"title": "Task A", "type": "coding", "prompt": "p", "depends_on": []},
              author="system", kind="spec")
    board.put("tasks/a/output", "the deliverable", author="test-agent", task_id="a")
    board.put("tasks/a/status", "done", author="system", task_id="a")

    plan = Plan.from_blackboard(board)

    assert set(plan.tasks) == {"a"}
    print("PASS: test_ignores_non_spec_keys_under_tasks_prefix")


if __name__ == "__main__":
    test_reconstructs_goal_tasks_and_dependencies()
    test_reconstructs_difficulty_and_specialty()
    test_missing_difficulty_specialty_defaults_gracefully()
    test_empty_board_yields_empty_plan()
    test_ignores_non_spec_keys_under_tasks_prefix()
    print("\nAll Plan.from_blackboard tests passed.")
