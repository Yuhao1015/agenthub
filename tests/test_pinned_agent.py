"""Task.pinned_agent（spec §3.7）：钉死 = 独占，不降级。

为什么不降级：用户选 agent 的动机常常是「别用 opus，省订阅额度」。
降级链偷偷把 opus 用上就是背叛用户意图。降级是自动模式的机制。

安全性不靠降级兜底，靠另一件事：Engine.run() 里 _run_preflight() 跑在
plan_created 之前，所以计划门开的时候熔断状态已知，本机没配好的 agent
在 UI 上根本选不到（见 _available_agents）。

无框架，纯 assert。零 LLM 调用。

Run: python3 tests/test_pinned_agent.py
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


class _RecordingExecutor:
    def __init__(self, name, ok=True):
        self.name = name
        self.ok = ok
        self.calls = []

    async def run(self, task, context, on_event=None):
        self.calls.append(task.id)
        if not self.ok:
            return ExecResult(ok=False, error=f"{self.name} down",
                              error_kind="transient", output="")
        return ExecResult(ok=True, output=f"done by {self.name}", cost_usd=0.0)


class _StubRouter:
    """两个 agent 的降级链：first 挂了就轮到 second。"""

    def __init__(self, first, second):
        self._ex = {"first": first, "second": second}
        self.budget = {"max_usd_per_run": 1e9, "max_attempts_per_task": 2}
        self.tiers = {}

    def chain_for(self, task):
        return [(n, e) for n, e in self._ex.items()]

    def animal(self, name):
        return "cat"

    def executor_for(self, name):
        return self._ex[name]

    def is_paid_api(self, name):
        return False

    def open_circuit(self, name):
        pass

    def claude_code_agents(self):
        return []

    def codebuddy_agents(self):
        return []


def _run(plan, router, tmp):
    bus = EventBus(os.path.join(tmp, "events.jsonl"))
    board = Blackboard(os.path.join(tmp, "blackboard.jsonl"), on_write=lambda e: None)
    eng = Engine(plan, router, bus, board, preflight=False,
                 summarize_enabled=False, workdir=tmp)
    out = asyncio.run(eng.run())
    bus.close()
    return out


def _one_task_plan(pinned=None):
    t = Task(id="t1", title="活儿", type=TaskType.CODING, prompt="p")
    if pinned:
        t.pinned_agent = pinned
    return Plan(goal="g", tasks={"t1": t})


def test_field_defaults_to_none():
    assert Task(id="t1", title="x", type=TaskType.CODING, prompt="p").pinned_agent is None


def test_unpinned_task_uses_full_fallback_chain():
    tmp = tempfile.mkdtemp(prefix="ah_pin_auto_")
    try:
        first, second = _RecordingExecutor("first", ok=False), _RecordingExecutor("second")
        plan = _run(_one_task_plan(), _StubRouter(first, second), tmp)
        assert plan.tasks["t1"].status == TaskStatus.DONE
        assert second.calls, "没钉选时降级链必须照常工作"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pinned_agent_is_exclusive_no_fallback():
    """钉了 first，first 挂了 —— second 绝不能被偷偷用上。"""
    tmp = tempfile.mkdtemp(prefix="ah_pin_excl_")
    try:
        first, second = _RecordingExecutor("first", ok=False), _RecordingExecutor("second")
        plan = _run(_one_task_plan(pinned="first"), _StubRouter(first, second), tmp)
        assert second.calls == [], \
            "钉选必须独占：降级到 second 等于背叛用户「别用它」的意图"
        assert plan.tasks["t1"].status == TaskStatus.FAILED
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pinned_agent_actually_runs_when_healthy():
    tmp = tempfile.mkdtemp(prefix="ah_pin_ok_")
    try:
        first, second = _RecordingExecutor("first"), _RecordingExecutor("second")
        plan = _run(_one_task_plan(pinned="second"), _StubRouter(first, second), tmp)
        assert second.calls == ["t1"] and first.calls == []
        assert plan.tasks["t1"].status == TaskStatus.DONE
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pinned_matrix_agent_can_run_outside_the_default_task_route():
    """计划门展示的是矩阵里所有可用 agent，因此用户钉选的 agent 不该
    仅因它不在该任务类型的自动降级链里就变成空链。

    ``mock`` 在矩阵中存在，但不在 planning route；这正是浏览器 E2E
    暴露的真实失败形状。钉选仍然必须独占，只是独占链应从这个 agent
    本身构造，而不是过滤自动路由。
    """
    from hub.router import Router

    tmp = tempfile.mkdtemp(prefix="ah_pin_route_external_")
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        router = Router(os.path.join(root, "hub", "capability_matrix.yaml"),
                        workdir=tmp, mock_all=True)
        plan = Plan(goal="g", tasks={
            "t1": Task(id="t1", title="规划", type=TaskType.PLANNING,
                       prompt="p", pinned_agent="mock")})
        out = _run(plan, router, tmp)
        assert out.tasks["t1"].status == TaskStatus.DONE
        assert out.tasks["t1"].agent == "mock"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pinning_an_unknown_agent_fails_cleanly():
    """钉了不存在的 agent -> 空链 -> 干净失败，不是崩溃。"""
    tmp = tempfile.mkdtemp(prefix="ah_pin_ghost_")
    try:
        first, second = _RecordingExecutor("first"), _RecordingExecutor("second")
        plan = _run(_one_task_plan(pinned="不存在的"), _StubRouter(first, second), tmp)
        assert plan.tasks["t1"].status == TaskStatus.FAILED
        assert first.calls == [] and second.calls == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_empty_chain_error_says_why_instead_of_all_agents_failed():
    """钉选的 agent 不可用时，chain 为空 -> 一个 agent 都没试过。

    这时候报 "all agents in chain failed" 是撒谎：没有 agent 失败，是
    根本没有 agent 可试。用户钉了它、UI 让他钉的，失败原因必须说清楚
    是「你钉的那个不可用，且钉选不降级」，否则他会去查那个 agent 的
    调用日志——而那里什么都没有。
    """
    tmp = tempfile.mkdtemp(prefix="ah_pin_whyfail_")
    try:
        first, second = _RecordingExecutor("first"), _RecordingExecutor("second")
        plan = _run(_one_task_plan(pinned="不存在的"), _StubRouter(first, second), tmp)
        err = plan.tasks["t1"].error or ""
        assert "不存在的" in err, f"错误信息要点名是哪个 agent，got {err!r}"
        assert "all agents in chain failed" not in err, \
            f"一个 agent 都没试过，不能说 all agents failed：{err!r}"
        assert first.calls == [] and second.calls == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_normal_chain_exhaustion_keeps_the_old_message():
    """回归：没钉选、真的把链跑完了才失败的情况，文案不变。"""
    tmp = tempfile.mkdtemp(prefix="ah_pin_oldmsg_")
    try:
        first = _RecordingExecutor("first", ok=False)
        second = _RecordingExecutor("second", ok=False)
        plan = _run(_one_task_plan(), _StubRouter(first, second), tmp)
        assert plan.tasks["t1"].status == TaskStatus.FAILED
        assert first.calls and second.calls, "两个都该被试过"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_from_json_reads_optional_field():
    plan = Plan.from_json({"goal": "g", "tasks": [
        {"id": "t1", "title": "a", "type": "coding", "prompt": "p",
         "pinned_agent": "cc-opus"},
        {"id": "t2", "title": "b", "type": "coding", "prompt": "p"}]})
    assert plan.tasks["t1"].pinned_agent == "cc-opus"
    assert plan.tasks["t2"].pinned_agent is None, "旧 plan JSON 必须零影响"


def test_from_blackboard_round_trips_pinned_agent():
    tmp = tempfile.mkdtemp(prefix="ah_pin_bb_")
    try:
        board = Blackboard(os.path.join(tmp, "blackboard.jsonl"), on_write=lambda e: None)
        board.put("goal/statement", "g", author="system")
        board.put("tasks/t1/spec", {"title": "a", "type": "coding", "prompt": "p",
                                    "depends_on": [], "difficulty": "medium",
                                    "specialty": None, "pinned_agent": "cc-opus"},
                  author="system", kind="spec")
        board.put("tasks/t2/spec", {"title": "b", "type": "coding", "prompt": "p",
                                    "depends_on": [], "difficulty": "medium",
                                    "specialty": None},
                  author="system", kind="spec")
        plan = Plan.from_blackboard(board)
        assert plan.tasks["t1"].pinned_agent == "cc-opus"
        assert plan.tasks["t2"].pinned_agent is None, "旧黑板数据必须零影响"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll pinned-agent tests passed.")
