"""计划门（spec §3.3/§3.6/§3.9）。

测试策略：_await_gate 轮询黑板，所以测试**先**把回复写进黑板再起
引擎——第一次 probe.refresh() 就读到，门立刻放行，不用睡也不用起线程，
完全确定性。

无框架，纯 assert。零 LLM 调用。

Run: python3 tests/test_gate_plan.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.blackboard import Blackboard
from hub.engine import Engine
from hub.executors import ExecResult
from hub.models import EventBus, Plan, Task, TaskStatus, TaskType

TEST_GATE_TOKEN = "ffffffffffffffff"


class _OkExecutor:
    def __init__(self):
        self.calls = []

    async def run(self, task, context, on_event=None):
        self.calls.append(task.id)
        return ExecResult(ok=True, output="ok", cost_usd=0.0)


class _StubRouter:
    def __init__(self, ex):
        self._ex = {"first": ex, "second": ex}
        self.budget = {"max_usd_per_run": 1e9, "max_attempts_per_task": 2}
        self.tiers = {"mid": ["first"]}
        self._open = set()

    def chain_for(self, task):
        return [(n, e) for n, e in self._ex.items() if n not in self._open]

    def animal(self, name):
        return "cat"

    def executor_for(self, name):
        return self._ex[name]

    def is_paid_api(self, name):
        return False

    def open_circuit(self, name):
        self._open.add(name)

    def claude_code_agents(self):
        return []

    def codebuddy_agents(self):
        return []

    def all_agents(self):
        return ["first", "second"]

    def circuit_open(self):
        return set(self._open)

    def kind(self, name):
        return "openrouter"

    def specialty(self, name):
        return None

    def tier_of(self, name):
        return "mid" if name == "first" else None

    def blurb(self, name):
        return ""


def _two_task_plan():
    return Plan(goal="g", tasks={
        "t1": Task(id="t1", title="一", type=TaskType.CODING, prompt="p"),
        "t2": Task(id="t2", title="二", type=TaskType.CODING, prompt="p",
                   depends_on=["t1"])})


def _setup(tmp, reply=None, **kw):
    board = Blackboard(os.path.join(tmp, "blackboard.jsonl"), on_write=lambda e: None)
    if reply is not None:
        seeded = dict(reply)
        seeded["gate_token"] = TEST_GATE_TOKEN
        board.put("run/gate_reply/plan", seeded, author="user")
    bus = EventBus(os.path.join(tmp, "events.jsonl"))
    ex = _OkExecutor()
    eng = Engine(_two_task_plan(), _StubRouter(ex), bus, board, preflight=False,
                 summarize_enabled=False, workdir=tmp, **kw)
    eng._new_gate_token = lambda: TEST_GATE_TOKEN
    return eng, bus, ex


def _events(tmp):
    out = []
    with open(os.path.join(tmp, "events.jsonl"), encoding="utf-8") as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out


def test_no_gate_in_auto_mode():
    """默认 auto/auto：一道门都不能开，既有行为完全不变。"""
    tmp = tempfile.mkdtemp(prefix="ah_gate_auto_")
    try:
        eng, bus, ex = _setup(tmp)
        plan = asyncio.run(eng.run())
        bus.close()
        assert [e["type"] for e in _events(tmp) if e["type"].startswith("gate_")] == []
        assert plan.tasks["t1"].status == TaskStatus.DONE
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_manual_assign_opens_plan_gate_even_when_permission_is_auto():
    """spec §3.9 的那个「或」：手动分配强制开计划门。"""
    tmp = tempfile.mkdtemp(prefix="ah_gate_manual_")
    try:
        eng, bus, ex = _setup(tmp, reply={"decision": "approve"},
                              gate_mode="auto", assign_mode="manual")
        asyncio.run(eng.run())
        bus.close()
        opened = [e for e in _events(tmp) if e["type"] == "gate_opened"]
        assert len(opened) == 1 and opened[0]["gate_id"] == "plan", opened
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_approve_applies_overrides_as_pinned_agent():
    tmp = tempfile.mkdtemp(prefix="ah_gate_ovr_")
    try:
        eng, bus, ex = _setup(tmp, gate_mode="ask",
                              reply={"decision": "approve",
                                     "overrides": {"t1": "second", "不存在": "x"}})
        plan = asyncio.run(eng.run())
        bus.close()
        assert plan.tasks["t1"].pinned_agent == "second"
        assert plan.tasks["t2"].pinned_agent is None
        resolved = [e for e in _events(tmp) if e["type"] == "gate_resolved"]
        assert resolved and resolved[0]["decision"] == "approve", resolved
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cancel_skips_everything_and_still_emits_run_finished():
    """驳回不能让 run 悄悄消失：所有 pending 置 SKIPPED，正常走到
    run_finished —— 否则 serve.py 的看门狗会把它误判成 crashed。"""
    tmp = tempfile.mkdtemp(prefix="ah_gate_cancel_")
    try:
        eng, bus, ex = _setup(tmp, gate_mode="ask", reply={"decision": "cancel"})
        plan = asyncio.run(eng.run())
        bus.close()
        assert ex.calls == [], "驳回后不该有任何任务真的跑起来"
        assert all(t.status == TaskStatus.SKIPPED for t in plan.tasks.values())
        types = [e["type"] for e in _events(tmp)]
        assert "run_cancelled" in types and "run_finished" in types, types
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_available_agents_excludes_circuit_open():
    """独占钉选的安全性全靠这条：本机没配好的 agent 在门上就选不到。"""
    tmp = tempfile.mkdtemp(prefix="ah_gate_avail_")
    try:
        eng, bus, ex = _setup(tmp, gate_mode="ask", reply={"decision": "approve"})
        eng.router.open_circuit("second")
        asyncio.run(eng.run())
        bus.close()
        opened = [e for e in _events(tmp) if e["type"] == "gate_opened"][0]
        names = [a["name"] for a in opened["available_agents"]]
        assert names == ["first"], names
        assert opened["available_agents"][0]["kind"] == "openrouter"
        assert opened["available_agents"][0]["tier"] == "mid"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_gate_timeout_cancels_never_approves():
    """超时自动放行 = 权限模式失效。超时只能是取消。"""
    tmp = tempfile.mkdtemp(prefix="ah_gate_timeout_")
    try:
        eng, bus, ex = _setup(tmp, gate_mode="ask")   # 不写回复
        eng.router.budget["gate_timeout_sec"] = 0.01
        plan = asyncio.run(eng.run())
        bus.close()
        assert ex.calls == [], "超时绝不能放行"
        assert all(t.status == TaskStatus.SKIPPED for t in plan.tasks.values())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_stale_reply_from_previous_gate_cannot_authorize_new_gate():
    """resume/rerun 复用 append-only 黑板；旧 plan approval 绝不能被新门
    当成授权。旧 gate 的存在是区分测试预写回复与历史回复的关键。"""
    tmp = tempfile.mkdtemp(prefix="ah_gate_stale_reply_")
    try:
        eng, bus, ex = _setup(tmp, gate_mode="ask")
        eng.board.put("run/gate/plan",
                      {"kind": "plan", "gate_token": "1111111111111111"},
                      author="engine")
        eng.board.put("run/gate_reply/plan",
                      {"decision": "approve", "gate_token": "1111111111111111"},
                      author="user")
        eng.router.budget["gate_timeout_sec"] = 0.01
        plan = asyncio.run(eng.run())
        bus.close()
        events = _events(tmp)
        opened = [e for e in events if e["type"] == "gate_opened"]
        resolved = [e for e in events if e["type"] == "gate_resolved"]
        assert opened and opened[0]["gate_token"] != "1111111111111111", opened
        assert resolved and resolved[0]["decision"] == "cancel", resolved
        assert ex.calls == [], "旧 approve 不能让任何任务开跑"
        assert all(t.status == TaskStatus.SKIPPED for t in plan.tasks.values())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_plan_created_carries_intended_agent():
    """spec §4.7：任何模式下都要看得见路由预计派给谁。"""
    tmp = tempfile.mkdtemp(prefix="ah_gate_intended_")
    try:
        eng, bus, ex = _setup(tmp)
        asyncio.run(eng.run())
        bus.close()
        created = [e for e in _events(tmp) if e["type"] == "plan_created"][0]
        for t in created["tasks"]:
            assert t["intended_agent"] == "first", t
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_event_contract_names_are_documented():
    """CLAUDE.md §2 要求：事件列表以 EventBus 文档字符串为准。"""
    from hub.models import EventBus as EB
    doc = EB.__doc__ or ""
    assert "gate_opened" in doc and "gate_resolved" in doc, \
        "新事件类型没写进 EventBus 文档字符串（那里是事件列表的唯一权威）"


if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll gate-plan tests passed.")
