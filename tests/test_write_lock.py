"""会写盘的任务排队（docs/decisions/change-safety-net.md §4）。

等待顺序：任务门 → 写锁 → 并发名额。两个会写的任务从不同时跑；只读任务照样
并行；从只读 agent 降级到会写的，先让出名额再拿写锁；max_parallel 为 1、2、3
都不死锁；codebuddy 不算会写、不排队；没有 kind() 的旧测试桩照旧并行。

无框架，纯 assert。零 LLM 调用。

Run: python tests/test_write_lock.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.blackboard import Blackboard
from hub.engine import Engine
from hub.executors import ExecResult
from hub.models import EventBus, Plan, Task, TaskStatus, TaskType


class _Probe:
    """How many executors -- and how many writers -- ran at the same time, at most."""

    def __init__(self):
        self.running = self.max_running = 0
        self.writers = self.max_writers = 0


class _Ex:
    def __init__(self, probe, writer, ok=True, delay=0.05):
        self.probe, self.writer, self.ok, self.delay = probe, writer, ok, delay

    async def run(self, task, context, on_event=None):
        p = self.probe
        p.running += 1
        p.max_running = max(p.max_running, p.running)
        if self.writer:
            p.writers += 1
            p.max_writers = max(p.max_writers, p.writers)
        try:
            await asyncio.sleep(self.delay)
        finally:
            p.running -= 1
            if self.writer:
                p.writers -= 1
        if self.ok:
            return ExecResult(ok=True, output="ok")
        return ExecResult(ok=False, error="bad output", error_kind="task_error")


class _Router:
    """agents: name -> (kind, allowed_tools, executor); chains: task id -> [names]."""

    def __init__(self, agents, chains):
        self._agents, self._chains = agents, chains
        self.budget = {"max_usd_per_run": 1e9, "max_attempts_per_task": 3}
        self.tiers = {}

    def chain_for(self, task):
        return [(n, self._agents[n][2]) for n in self._chains[task.id]]

    def executor_for(self, n):
        return self._agents[n][2]

    def kind(self, n):
        return self._agents[n][0]

    def allowed_tools(self, n):
        return self._agents[n][1]

    def animal(self, n):
        return "cat"

    def is_paid_api(self, n):
        return False

    def open_circuit(self, n):
        pass

    def claude_code_agents(self):
        return []

    def codebuddy_agents(self):
        return []

    def all_agents(self):
        return list(self._agents)

    def circuit_open(self):
        return set()

    def specialty(self, n):
        return None

    def tier_of(self, n):
        return None

    def blurb(self, n):
        return ""


def _run(agents, chains, parallel=3, router_cls=None):
    tmp = tempfile.mkdtemp(prefix="ah_wlock_")
    try:
        board = Blackboard(os.path.join(tmp, "blackboard.jsonl"), on_write=None)
        bus = EventBus(os.path.join(tmp, "events.jsonl"))
        plan = Plan(goal="g", tasks={tid: Task(id=tid, title=tid, type=TaskType.CODING,
                                               prompt="p") for tid in chains})
        router = (router_cls or _Router)(agents, chains)
        eng = Engine(plan, router, bus, board, max_parallel=parallel, preflight=False,
                     summarize_enabled=False, workdir=tmp)
        plan = asyncio.run(asyncio.wait_for(eng.run(), timeout=20))   # a deadlock -> TimeoutError
        bus.close()
        return plan
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_two_writers_never_overlap():
    p = _Probe()
    agents = {"w1": ("claude_code", "Read,Write", _Ex(p, True)),
              "w2": ("claude_code", "Read,Bash", _Ex(p, True))}
    plan = _run(agents, {"t1": ["w1"], "t2": ["w2"]})
    assert all(t.status == TaskStatus.DONE for t in plan.tasks.values())
    assert p.max_writers == 1, f"writers overlapped: {p.max_writers}"


def test_readers_still_run_alongside_a_writer():
    p = _Probe()
    agents = {"w": ("claude_code", "Read,Write", _Ex(p, True, delay=0.2)),
              "r": ("claude_code", "Read", _Ex(p, False))}
    _run(agents, {"t1": ["w"], "t2": ["r"], "t3": ["r"]})
    assert p.max_writers == 1 and p.max_running == 3, (p.max_writers, p.max_running)


def test_fallback_from_reader_to_writer_waits_for_the_lock():
    """t2 starts on a read-only agent that fails, then falls back to a writer:
    that writer must not run while t1's writer holds the lock."""
    p = _Probe()
    agents = {"w": ("claude_code", "Read,Write", _Ex(p, True, delay=0.2)),
              "bad-reader": ("claude_code", "Read", _Ex(p, False, ok=False, delay=0.01)),
              "w2": ("codex", "", _Ex(p, True))}
    plan = _run(agents, {"t1": ["w"], "t2": ["bad-reader", "w2"]})
    assert plan.tasks["t2"].status == TaskStatus.DONE and plan.tasks["t2"].agent == "w2"
    assert p.max_writers == 1, p.max_writers


def test_no_deadlock_at_any_parallelism():
    for parallel in (1, 2, 3):
        p = _Probe()
        agents = {"w": ("claude_code", "Read,Write", _Ex(p, True)),
                  "r": ("claude_code", "Read", _Ex(p, False)),
                  "bad-reader": ("claude_code", "Read", _Ex(p, False, ok=False, delay=0.01)),
                  "w2": ("dsh", "", _Ex(p, True))}
        chains = {"t1": ["w"], "t2": ["r"], "t3": ["bad-reader", "w2"], "t4": ["w"],
                  "t5": ["bad-reader", "w2"]}
        plan = _run(agents, chains, parallel=parallel)
        assert all(t.status == TaskStatus.DONE for t in plan.tasks.values()), parallel
        assert p.max_writers == 1 and p.max_running <= parallel, (parallel, p.max_running)


def test_codebuddy_does_not_queue():
    p = _Probe()
    agents = {"cb": ("codebuddy", "", _Ex(p, False, delay=0.1))}
    _run(agents, {"t1": ["cb"], "t2": ["cb"]})
    assert p.max_running == 2, "codebuddy runs with --tools \"\": it cannot write, so no lock"


def test_a_router_without_kind_is_treated_as_read_only():
    """Older test stubs have no kind(): they keep running in parallel, exactly
    as before the write lock existed."""
    class NoKind(_Router):
        kind = None
    p = _Probe()
    agents = {"w": ("claude_code", "Read,Write", _Ex(p, True))}
    _run(agents, {"t1": ["w"], "t2": ["w"]}, router_cls=NoKind)
    assert p.max_writers == 2, p.max_writers


if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll write-lock tests passed.")
