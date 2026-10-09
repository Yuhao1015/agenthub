"""写盘任务门（spec §3.8）。

判据表（hub/router.py agent_can_write，写盘门、写锁、追问拦截共用）：
  claude_code -> allowed_tools 含 Edit/Write/Bash
  codex       -> 一律算写盘
  dsh         -> 一律算写盘
  codebuddy   -> 不算：两条执行路径都带 --tools ""，2026-07-21 实测会关掉全部工具
  openrouter  -> 不算（纯 HTTP 文本补全，不碰盘）
  mock        -> 不算

codex/dsh 是全权 agent，Agent Hub 对它们的工具权限没有任何可见性，
所以保守地全当写盘 —— 而不是假装能判断。

无框架，纯 assert。零 LLM 调用。

Run: python3 tests/test_gate_write_task.py
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


class _Ex:
    def __init__(self, allowed_tools=""):
        self.allowed_tools = allowed_tools
        self.calls = []

    async def run(self, task, context, on_event=None):
        self.calls.append(task.id)
        return ExecResult(ok=True, output="ok", cost_usd=0.0)


class _Router:
    def __init__(self, kind, allowed_tools=""):
        self._kind = kind
        self._ex = _Ex(allowed_tools)
        self.budget = {"max_usd_per_run": 1e9, "max_attempts_per_task": 2}
        self.tiers = {}

    def chain_for(self, task):
        return [("a1", self._ex)]

    def animal(self, n):
        return "cat"

    def executor_for(self, n):
        return self._ex

    def is_paid_api(self, n):
        return False

    def open_circuit(self, n):
        pass

    def claude_code_agents(self):
        return []

    def codebuddy_agents(self):
        return []

    def all_agents(self):
        return ["a1"]

    def circuit_open(self):
        return set()

    def kind(self, n):
        return self._kind

    def specialty(self, n):
        return None

    def tier_of(self, n):
        return None

    def blurb(self, n):
        return ""


def _engine(tmp, router, reply=None, gate_mode="ask"):
    board = Blackboard(os.path.join(tmp, "blackboard.jsonl"), on_write=lambda e: None)
    if gate_mode == "ask":
        # ask 模式下**计划门也会开**（spec §3.9）。这个文件测的是写盘门，
        # 所以先把计划门放行掉 -- 不放的话每个用例都会挂在计划门上，
        # 看起来像「写盘门开错了」，其实是测试少写了一步。
        board.put("run/gate_reply/plan",
                  {"decision": "approve", "gate_token": TEST_GATE_TOKEN},
                  author="user")
    if reply is not None:
        seeded = dict(reply)
        seeded["gate_token"] = TEST_GATE_TOKEN
        board.put("run/gate_reply/task:t1", seeded, author="user")
    bus = EventBus(os.path.join(tmp, "events.jsonl"))
    plan = Plan(goal="g", tasks={
        "t1": Task(id="t1", title="活儿", type=TaskType.CODING, prompt="p")})
    eng = Engine(plan, router, bus, board, preflight=False,
                 summarize_enabled=False, workdir=tmp, gate_mode=gate_mode)
    eng._new_gate_token = lambda: TEST_GATE_TOKEN
    return eng, bus


def _events(tmp):
    with open(os.path.join(tmp, "events.jsonl"), encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def _run_with(kind, allowed_tools="", reply=None, gate_mode="ask"):
    tmp = tempfile.mkdtemp(prefix="ah_wgate_")
    router = _Router(kind, allowed_tools)
    eng, bus = _engine(tmp, router, reply=reply, gate_mode=gate_mode)
    plan = asyncio.run(eng.run())
    bus.close()
    gates = [e for e in _events(tmp) if e["type"] == "gate_opened"
             and e.get("kind") == "task"]
    shutil.rmtree(tmp, ignore_errors=True)
    return plan, router._ex, gates


def test_claude_code_with_write_tools_opens_gate():
    plan, ex, gates = _run_with("claude_code", "Read,Edit,Write,Bash",
                                reply={"decision": "approve"})
    assert len(gates) == 1 and gates[0]["task_id"] == "t1", gates
    assert ex.calls == ["t1"]


def test_claude_code_read_only_does_not_open_gate():
    plan, ex, gates = _run_with("claude_code", "Read")
    assert gates == [], "只读任务不该拦"
    assert ex.calls == ["t1"]


def test_codex_and_dsh_always_gate():
    """全权 agent，工具权限对 Agent Hub 不可见，保守当写盘。"""
    for kind in ("codex", "dsh"):
        plan, ex, gates = _run_with(kind, "", reply={"decision": "approve"})
        assert len(gates) == 1, f"{kind} 应该一律开门，got {gates}"


def test_codebuddy_never_gates():
    """B19 的一半：codebuddy 两条执行路径都带 --tools ""（2026-07-21 实测会关掉
    全部工具），它根本写不了盘，以前却被当成全权 agent 白白弹写盘门。"""
    plan, ex, gates = _run_with("codebuddy", "")
    assert gates == [], gates
    assert ex.calls == ["t1"]


def test_agent_can_write_table():
    from hub.router import agent_can_write
    assert agent_can_write("claude_code", "Read, Edit") is True
    assert agent_can_write("claude_code", ["Read", "Bash"]) is True
    assert agent_can_write("claude_code", "Read") is False
    assert agent_can_write("claude_code", "") is False
    for kind in ("codex", "dsh"):
        assert agent_can_write(kind, "") is True, kind
    for kind in ("codebuddy", "openrouter", "mock", ""):
        assert agent_can_write(kind, "Read,Write,Bash") is False, kind


def test_router_can_write_reads_the_declared_matrix():
    """--mock 换掉了执行器，但判断看矩阵里声明的类型，所以 mock 也能预演写锁。"""
    from hub.router import Router
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    router = Router(os.path.join(root, "hub", "capability_matrix.yaml"),
                    workdir=".", mock_all=True)
    expected = {"cc-sonnet": True, "cc-opus": False, "codex-local": True,
                "cc-glm-strong": False, "dsh-pro": True, "or-deepseek": False}
    got = {name: router.can_write(name) for name in expected}
    assert got == expected, got


def test_openrouter_and_mock_never_gate():
    for kind in ("openrouter", "mock"):
        plan, ex, gates = _run_with(kind, "")
        assert gates == [], f"{kind} 不碰盘，不该开门"
        assert ex.calls == ["t1"]


def test_read_only_first_candidate_still_gates_for_writable_fallback():
    """planning can start with read-only Claude and fall back to Codex."""
    tmp = tempfile.mkdtemp(prefix="ah_wgate_fallback_")

    class MixedRouter(_Router):
        def __init__(self):
            super().__init__("claude_code", "Read")
            self._codex = _Ex()

        def chain_for(self, task):
            return [("readonly", self._ex), ("codex-local", self._codex)]

        def kind(self, name):
            return "codex" if name == "codex-local" else "claude_code"

    try:
        router = MixedRouter()
        eng, bus = _engine(tmp, router)
        assert eng._is_write_task(eng.plan.tasks["t1"]) is True
        bus.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_auto_mode_never_gates_even_for_write_tasks():
    plan, ex, gates = _run_with("claude_code", "Read,Write", gate_mode="auto")
    assert gates == [] and ex.calls == ["t1"]


def test_skip_marks_task_skipped_and_does_not_run_it():
    plan, ex, gates = _run_with("claude_code", "Read,Write",
                                reply={"decision": "skip"})
    assert ex.calls == [], "跳过的任务绝不能真的跑"
    assert plan.tasks["t1"].status == TaskStatus.SKIPPED


def test_cancel_at_task_gate_stops_the_whole_run():
    plan, ex, gates = _run_with("claude_code", "Read,Write",
                                reply={"decision": "cancel"})
    assert ex.calls == []
    assert plan.tasks["t1"].status == TaskStatus.SKIPPED


def test_allowed_tools_parsing_handles_spaces():
    """POST /settings/tools 存的是 "Read, Edit" 这种逗号空格分隔的串。"""
    tmp = tempfile.mkdtemp(prefix="ah_wgate_parse_")
    try:
        eng, bus = _engine(tmp, _Router("claude_code", "Read, Edit, Bash"))
        assert eng._allowed_tools_of("a1") == ["Read", "Edit", "Bash"]
        bus.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_mock_all_still_opens_write_gate():
    """回归：--mock 必须能预演权限流程。

    _allowed_tools_of 早先读的是执行器实例属性，而 mock_all=True 时执行器
    被换成 MockExecutor（没有 allowed_tools）-> 空列表 -> 判成非写盘任务
    -> --mock 下写盘门永远不开。那等于 CLAUDE.md §7 要求的「跑真实链路前
    先 --mock 验证逻辑」对权限流程完全失效。改成从矩阵读声明值后修复。

    这条用**真实的** Router（不是桩），因为踩的坑正是真实 Router 在
    mock_all 下的行为。
    """
    from hub.router import Router
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    router = Router(os.path.join(root, "hub", "capability_matrix.yaml"),
                    workdir=".", mock_all=True)
    tmp = tempfile.mkdtemp(prefix="ah_wgate_mock_")
    try:
        board = Blackboard(os.path.join(tmp, "blackboard.jsonl"),
                           on_write=lambda e: None)
        board.put("run/gate_reply/plan",
                  {"decision": "approve", "gate_token": TEST_GATE_TOKEN},
                  author="user")
        board.put("run/gate_reply/task:t1",
                  {"decision": "approve", "gate_token": TEST_GATE_TOKEN},
                  author="user")
        bus = EventBus(os.path.join(tmp, "events.jsonl"))
        plan = Plan(goal="g", tasks={
            "t1": Task(id="t1", title="写代码", type=TaskType.CODING, prompt="p")})
        eng = Engine(plan, router, bus, board, preflight=False,
                     summarize_enabled=False, workdir=tmp, gate_mode="ask")
        eng._new_gate_token = lambda: TEST_GATE_TOKEN
        # coding 链首是 cc-sonnet（claude_code, allowed_tools 含 Write/Bash）
        assert eng._allowed_tools_of("cc-sonnet") == ["Read", "Edit", "Write", "Bash"], \
            eng._allowed_tools_of("cc-sonnet")
        assert eng._is_write_task(plan.tasks["t1"]) is True, \
            "--mock 下写盘门不开 = 权限流程没法预演"
        asyncio.run(eng.run())
        bus.close()
        gates = [e for e in _events(tmp) if e["type"] == "gate_opened"
                 and e.get("kind") == "task"]
        assert len(gates) == 1, gates
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll write-gate tests passed.")
