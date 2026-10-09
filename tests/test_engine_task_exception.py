"""Bug B1 (docs/decisions/gaps-2026-09-25.md): an unexpected exception inside
one task must not leave that task RUNNING forever while the run still ends
with run_finished and exit code 0.

Before the fix the exception was swallowed inside the asyncio Task ("Task
exception was never retrieved"): no task_failed event, the task stayed
"running", its dependents stayed "pending", and serve.py's watchdog recorded
the run as done.

Run: python tests/test_engine_task_exception.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.blackboard import Blackboard
from hub.engine import Engine
from hub.executors import ExecResult
from hub.models import EventBus, Plan, Task, TaskType


class _OkExecutor:
    async def run(self, task, context, on_event=None):
        return ExecResult(ok=True, output=f"out {task.id}")


class _RaisingExecutor:
    """Stands in for any failure the executors don't classify, e.g. an
    OSError from the OS refusing an oversized command line."""
    async def run(self, task, context, on_event=None):
        raise OSError("simulated spawn failure")


class _FakeRouter:
    budget = {"max_usd_per_run": 2.0, "max_attempts_per_task": 2}

    def __init__(self, chains):
        self._chains = chains          # task type value -> [(name, executor)]

    def chain_for(self, task):
        chain = self._chains.get(task.type.value)
        if not chain:
            # same behaviour as the real Router for an emptied route
            raise ValueError(f"no route for task type '{task.type.value}'")
        return chain

    def animal(self, name):
        return "cat"

    def open_circuit(self, name):
        pass

    def is_paid_api(self, name):
        return False


def _run(chains):
    """Plan: a -> b (b depends on a), plus an independent c that succeeds."""
    tmp = tempfile.mkdtemp(prefix="ah_b1_")
    events_path = os.path.join(tmp, "events.jsonl")
    bus = EventBus(events_path)
    board = Blackboard(os.path.join(tmp, "board.jsonl"), on_write=lambda e: None)
    plan = Plan(goal="g", tasks={
        "a": Task(id="a", title="a", type=TaskType.CODING, prompt="p"),
        "b": Task(id="b", title="b", type=TaskType.REVIEW, prompt="p", depends_on=["a"]),
        "c": Task(id="c", title="c", type=TaskType.REVIEW, prompt="p"),
    })
    engine = Engine(plan, _FakeRouter(chains), bus, board, preflight=False, workdir=tmp)
    asyncio.run(engine.run())
    bus.close()
    with open(events_path, encoding="utf-8") as f:
        events = [json.loads(line) for line in f if line.strip()]
    return plan, events


def _check_failed_not_stuck(plan, events):
    statuses = {t.id: t.status.value for t in plan.tasks.values()}
    assert statuses == {"a": "failed", "b": "skipped", "c": "done"}, statuses
    failed = [e for e in events if e["type"] == "task_failed"]
    assert [e["task_id"] for e in failed] == ["a"], failed
    assert failed[0]["error"], "the failure must carry a readable reason"
    finished = [e for e in events if e["type"] == "run_finished"]
    assert len(finished) == 1
    assert finished[0]["statuses"] == {"a": "failed", "b": "skipped", "c": "done"}, \
        finished[0]["statuses"]


def test_executor_exception_fails_the_task():
    plan, events = _run({"coding": [("boom", _RaisingExecutor())],
                         "review": [("ok", _OkExecutor())]})
    _check_failed_not_stuck(plan, events)
    failed = next(e for e in events if e["type"] == "task_failed")
    assert "simulated spawn failure" in failed["error"], failed["error"]
    print("PASS: test_executor_exception_fails_the_task")


def test_missing_route_fails_the_task():
    """An emptied route (the settings page's 'remove from every chain' can
    produce one) raises ValueError from chain_for inside _run_task."""
    plan, events = _run({"review": [("ok", _OkExecutor())]})
    _check_failed_not_stuck(plan, events)
    failed = next(e for e in events if e["type"] == "task_failed")
    assert "no route" in failed["error"], failed["error"]
    print("PASS: test_missing_route_fails_the_task")


if __name__ == "__main__":
    test_executor_exception_fails_the_task()
    test_missing_route_fails_the_task()
