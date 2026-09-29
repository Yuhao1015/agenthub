"""Engine <-> Summarizer integration tests. No framework -- plain assert,
exit 1 on failure. All routers/executors are fakes; zero real LLM calls.

Run: python tests/test_engine_summarizer.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.blackboard import Blackboard
from hub.engine import Engine
from hub.executors import ExecResult
from hub.models import EventBus, Plan, Task, TaskStatus, TaskType

_LONG_OUTPUT = "x" * 700   # > SUMMARIZE_TRIGGER_LEN (600)
_SHORT_OUTPUT = "short output, no summary needed"  # <= 600 chars


class _MainExecutor:
    """Executes the real (non-summarize) task."""

    def __init__(self, output, cost_usd=0.05):
        self.output = output
        self.cost_usd = cost_usd
        self.calls = 0

    async def run(self, task, context, on_event=None):
        self.calls += 1
        return ExecResult(ok=True, output=self.output, cost_usd=self.cost_usd)


class _SummarizerExecutor:
    """Executes the LIGHT/easy summarizer pseudo-task."""

    def __init__(self, ok=True, output="a real compressed summary",
                 cost_usd=0.02, cost_credits=0.3):
        self.ok = ok
        self.output = output
        self.cost_usd = cost_usd
        self.cost_credits = cost_credits
        self.calls = 0

    async def run(self, task, context):
        self.calls += 1
        if not self.ok:
            return ExecResult(ok=False, error="broken", error_kind="task_error",
                              cost_usd=self.cost_usd, cost_credits=self.cost_credits)
        return ExecResult(ok=True, output=self.output, cost_usd=self.cost_usd,
                          cost_credits=self.cost_credits)


class _RaisingSummarizerExecutor:
    """Simulates a subprocess spawn failure (e.g. Windows argv-length OSError) --
    raises instead of returning an ExecResult."""

    def __init__(self):
        self.calls = 0

    async def run(self, task, context):
        self.calls += 1
        raise OSError("mock: command line too long")


class _FakeRouter:
    budget = {"max_usd_per_run": 1e9, "max_attempts_per_task": 2}

    def __init__(self, main_executor, summarizer_executor):
        self._main = main_executor
        self._summarizer = summarizer_executor
        self._circuit_open = set()

    def chain_for(self, task):
        if task.type == TaskType.LIGHT and task.difficulty == "easy":
            names = [("cc-hy3", self._summarizer)]
        else:
            names = [("main-agent", self._main)]
        return [(n, e) for n, e in names if n not in self._circuit_open]

    def animal(self, name):
        return "cat"

    def open_circuit(self, name):
        self._circuit_open.add(name)

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
    if os.path.exists(events_path):
        os.remove(events_path)
    if os.path.exists(board_path):
        os.remove(board_path)


def test_task_with_dependent_and_long_output_gets_real_summary():
    events_path, board_path = _fresh_paths("sum1")
    bus = EventBus(events_path)
    board = Blackboard(board_path, on_write=lambda e: None)
    plan = Plan(goal="test", tasks={})
    task_a = Task(id="a", title="produce", type=TaskType.CODING, prompt="p")
    task_b = Task(id="b", title="consume", type=TaskType.CODING, prompt="p",
                  depends_on=["a"])
    plan.tasks["a"] = task_a
    plan.tasks["b"] = task_b

    main_ex = _MainExecutor(_LONG_OUTPUT)
    summ_ex = _SummarizerExecutor(ok=True, output="the real summary")
    router = _FakeRouter(main_ex, summ_ex)
    engine = Engine(plan, router, bus, board, max_parallel=1)

    asyncio.run(engine._run_task(task_a))
    bus.close()

    assert task_a.status == TaskStatus.DONE
    assert summ_ex.calls == 1, "dependent + long output must trigger the summarizer"
    assert board.get("tasks/a/summary") == "the real summary"
    assert task_a.summary_cost_usd == 0.02
    assert task_a.summary_cost_credits == 0.3
    assert round(engine.total_cost, 4) == 0.07, "engine total must include summary cost"
    assert round(engine.total_credits, 4) == 0.3

    _cleanup(events_path, board_path)
    print("PASS: test_task_with_dependent_and_long_output_gets_real_summary")


def test_task_without_dependents_skips_summarizer():
    events_path, board_path = _fresh_paths("sum2")
    bus = EventBus(events_path)
    board = Blackboard(board_path, on_write=lambda e: None)
    plan = Plan(goal="test", tasks={})
    task_a = Task(id="a", title="produce", type=TaskType.CODING, prompt="p")
    plan.tasks["a"] = task_a   # nobody depends on "a"

    main_ex = _MainExecutor(_LONG_OUTPUT)
    summ_ex = _SummarizerExecutor()
    router = _FakeRouter(main_ex, summ_ex)
    engine = Engine(plan, router, bus, board, max_parallel=1)

    asyncio.run(engine._run_task(task_a))
    bus.close()

    assert task_a.status == TaskStatus.DONE
    assert summ_ex.calls == 0, "no dependents -> summarizer must never be called"
    assert board.get("tasks/a/summary") is None
    assert task_a.summary_cost_usd == 0.0

    _cleanup(events_path, board_path)
    print("PASS: test_task_without_dependents_skips_summarizer")


def test_short_output_skips_summarizer():
    events_path, board_path = _fresh_paths("sum3")
    bus = EventBus(events_path)
    board = Blackboard(board_path, on_write=lambda e: None)
    plan = Plan(goal="test", tasks={})
    task_a = Task(id="a", title="produce", type=TaskType.CODING, prompt="p")
    task_b = Task(id="b", title="consume", type=TaskType.CODING, prompt="p",
                  depends_on=["a"])
    plan.tasks["a"] = task_a
    plan.tasks["b"] = task_b

    main_ex = _MainExecutor(_SHORT_OUTPUT)
    summ_ex = _SummarizerExecutor()
    router = _FakeRouter(main_ex, summ_ex)
    engine = Engine(plan, router, bus, board, max_parallel=1)

    asyncio.run(engine._run_task(task_a))
    bus.close()

    assert summ_ex.calls == 0, "short output -> summarizer must never be called"
    assert board.get("tasks/a/summary") is None

    _cleanup(events_path, board_path)
    print("PASS: test_short_output_skips_summarizer")


def test_summarizer_failure_falls_back_gracefully():
    events_path, board_path = _fresh_paths("sum4")
    bus = EventBus(events_path)
    board = Blackboard(board_path, on_write=lambda e: None)
    plan = Plan(goal="test", tasks={})
    task_a = Task(id="a", title="produce", type=TaskType.CODING, prompt="p")
    task_b = Task(id="b", title="consume", type=TaskType.CODING, prompt="p",
                  depends_on=["a"])
    plan.tasks["a"] = task_a
    plan.tasks["b"] = task_b

    main_ex = _MainExecutor(_LONG_OUTPUT)
    summ_ex = _SummarizerExecutor(ok=False)   # entire economy chain fails
    router = _FakeRouter(main_ex, summ_ex)
    engine = Engine(plan, router, bus, board, max_parallel=1)

    asyncio.run(engine._run_task(task_a))
    bus.close()

    assert task_a.status == TaskStatus.DONE, "task must succeed even if summarizer fails"
    assert board.get("tasks/a/summary") is None, "no placeholder summary on failure"
    assert board.get("tasks/a/output") == _LONG_OUTPUT, "raw output fallback still there"

    with open(events_path, encoding="utf-8") as f:
        events = [json.loads(l) for l in f if l.strip()]
    summ_events = [e for e in events if e["type"] == "summary_generated"]
    assert len(summ_events) == 1
    assert summ_events[0]["ok"] is False
    assert summ_events[0]["cost_usd"] == 0.02

    _cleanup(events_path, board_path)
    print("PASS: test_summarizer_failure_falls_back_gracefully")


def test_summarizer_exception_does_not_crash_task():
    events_path, board_path = _fresh_paths("sum8")
    bus = EventBus(events_path)
    board = Blackboard(board_path, on_write=lambda e: None)
    plan = Plan(goal="test", tasks={})
    task_a = Task(id="a", title="produce", type=TaskType.CODING, prompt="p")
    task_b = Task(id="b", title="consume", type=TaskType.CODING, prompt="p",
                  depends_on=["a"])
    plan.tasks["a"] = task_a
    plan.tasks["b"] = task_b

    main_ex = _MainExecutor(_LONG_OUTPUT)
    summ_ex = _RaisingSummarizerExecutor()
    router = _FakeRouter(main_ex, summ_ex)
    engine = Engine(plan, router, bus, board, max_parallel=1)

    asyncio.run(engine._run_task(task_a))
    bus.close()

    assert task_a.status == TaskStatus.DONE, \
        "a raised exception from the summarizer sub-call must not crash the task"
    assert board.get("tasks/a/summary") is None
    assert board.get("tasks/a/output") == _LONG_OUTPUT

    with open(events_path, encoding="utf-8") as f:
        events = [json.loads(l) for l in f if l.strip()]
    assert any(e["type"] == "task_completed" for e in events), \
        "task_completed must still be emitted even when the summarizer sub-call raises"
    summ_events = [e for e in events if e["type"] == "summary_generated"]
    assert len(summ_events) == 1
    assert summ_events[0]["ok"] is False

    _cleanup(events_path, board_path)
    print("PASS: test_summarizer_exception_does_not_crash_task")


def test_summarizer_circuit_open_emits_event():
    events_path, board_path = _fresh_paths("sum9")
    bus = EventBus(events_path)
    board = Blackboard(board_path, on_write=lambda e: None)
    plan = Plan(goal="test", tasks={})
    task_a = Task(id="a", title="produce", type=TaskType.CODING, prompt="p")
    task_b = Task(id="b", title="consume", type=TaskType.CODING, prompt="p",
                  depends_on=["a"])
    plan.tasks["a"] = task_a
    plan.tasks["b"] = task_b

    main_ex = _MainExecutor(_LONG_OUTPUT)

    class _UnavailableThenOkExecutor:
        def __init__(self):
            self.calls = 0

        async def run(self, task, context):
            self.calls += 1
            return ExecResult(ok=False, error="credits required",
                              error_kind="unavailable", cost_usd=0.0)

    summ_ex = _UnavailableThenOkExecutor()
    router = _FakeRouter(main_ex, summ_ex)
    engine = Engine(plan, router, bus, board, max_parallel=1)

    asyncio.run(engine._run_task(task_a))
    bus.close()

    assert "cc-hy3" in router._circuit_open

    with open(events_path, encoding="utf-8") as f:
        events = [json.loads(l) for l in f if l.strip()]
    circuit_events = [e for e in events if e["type"] == "agent_circuit_open"
                      and e.get("agent") == "cc-hy3"]
    assert len(circuit_events) == 1, \
        "the summarizer's circuit-open must be visible as an agent_circuit_open event"
    assert circuit_events[0]["task_id"] == "a"

    _cleanup(events_path, board_path)
    print("PASS: test_summarizer_circuit_open_emits_event")


def test_summary_generated_event_fields_on_success():
    events_path, board_path = _fresh_paths("sum5")
    bus = EventBus(events_path)
    board = Blackboard(board_path, on_write=lambda e: None)
    plan = Plan(goal="test", tasks={})
    task_a = Task(id="a", title="produce", type=TaskType.CODING, prompt="p")
    task_b = Task(id="b", title="consume", type=TaskType.CODING, prompt="p",
                  depends_on=["a"])
    plan.tasks["a"] = task_a
    plan.tasks["b"] = task_b

    main_ex = _MainExecutor(_LONG_OUTPUT)
    summ_ex = _SummarizerExecutor(ok=True, output="short summary text")
    router = _FakeRouter(main_ex, summ_ex)
    engine = Engine(plan, router, bus, board, max_parallel=1)

    asyncio.run(engine._run_task(task_a))
    bus.close()

    with open(events_path, encoding="utf-8") as f:
        events = [json.loads(l) for l in f if l.strip()]
    summ_events = [e for e in events if e["type"] == "summary_generated"]
    assert len(summ_events) == 1
    e = summ_events[0]
    assert e["task_id"] == "a"
    assert e["agent"] == "cc-hy3"
    assert e["ok"] is True
    assert e["cost_usd"] == 0.02
    assert e["cost_credits"] == 0.3
    assert e["output_len"] == len(_LONG_OUTPUT)
    assert e["summary_len"] == len("short summary text")

    _cleanup(events_path, board_path)
    print("PASS: test_summary_generated_event_fields_on_success")


def test_summarize_disabled_skips_everything():
    events_path, board_path = _fresh_paths("sum6")
    bus = EventBus(events_path)
    board = Blackboard(board_path, on_write=lambda e: None)
    plan = Plan(goal="test", tasks={})
    task_a = Task(id="a", title="produce", type=TaskType.CODING, prompt="p")
    task_b = Task(id="b", title="consume", type=TaskType.CODING, prompt="p",
                  depends_on=["a"])
    plan.tasks["a"] = task_a
    plan.tasks["b"] = task_b

    main_ex = _MainExecutor(_LONG_OUTPUT)
    summ_ex = _SummarizerExecutor()
    router = _FakeRouter(main_ex, summ_ex)
    engine = Engine(plan, router, bus, board, max_parallel=1, summarize_enabled=False)

    asyncio.run(engine._run_task(task_a))
    bus.close()

    assert summ_ex.calls == 0
    assert board.get("tasks/a/summary") is None

    with open(events_path, encoding="utf-8") as f:
        events = [json.loads(l) for l in f if l.strip()]
    assert not any(e["type"] == "summary_generated" for e in events)

    _cleanup(events_path, board_path)
    print("PASS: test_summarize_disabled_skips_everything")


def test_task_cost_and_summary_cost_tracked_separately():
    events_path, board_path = _fresh_paths("sum7")
    bus = EventBus(events_path)
    board = Blackboard(board_path, on_write=lambda e: None)
    plan = Plan(goal="test", tasks={})
    task_a = Task(id="a", title="produce", type=TaskType.CODING, prompt="p")
    task_b = Task(id="b", title="consume", type=TaskType.CODING, prompt="p",
                  depends_on=["a"])
    plan.tasks["a"] = task_a
    plan.tasks["b"] = task_b

    main_ex = _MainExecutor(_LONG_OUTPUT, cost_usd=0.05)
    summ_ex = _SummarizerExecutor(ok=True, cost_usd=0.02, cost_credits=0.3)
    router = _FakeRouter(main_ex, summ_ex)
    engine = Engine(plan, router, bus, board, max_parallel=1)

    asyncio.run(engine._run_task(task_a))
    bus.close()

    assert task_a.cost_usd == 0.05, "task's own cost must NOT include summary cost"
    assert task_a.summary_cost_usd == 0.02
    assert round(engine.total_cost, 4) == 0.07, "engine total must be the sum of both"

    _cleanup(events_path, board_path)
    print("PASS: test_task_cost_and_summary_cost_tracked_separately")


if __name__ == "__main__":
    test_task_with_dependent_and_long_output_gets_real_summary()
    test_task_without_dependents_skips_summarizer()
    test_short_output_skips_summarizer()
    test_summarizer_failure_falls_back_gracefully()
    test_summarizer_exception_does_not_crash_task()
    test_summarizer_circuit_open_emits_event()
    test_summary_generated_event_fields_on_success()
    test_summarize_disabled_skips_everything()
    test_task_cost_and_summary_cost_tracked_separately()
    print("\nAll engine-summarizer integration tests passed.")
