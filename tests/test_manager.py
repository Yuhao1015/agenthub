"""Manager clarification feature tests. No framework -- plain assert,
exit 1 on failure. All executors are fakes; zero real LLM calls.

Run: python tests/test_manager.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.executors import ExecResult
from hub.models import Task, TaskType


def test_task_type_and_route():
    """TaskType.MANAGER exists and the yaml routes it to
    [cc-sonnet-mgr, cc-kimi] in that order."""
    from hub.router import Router

    assert TaskType.MANAGER.value == "manager"
    router = Router("hub/capability_matrix.yaml", workdir=".", mock_all=True)
    task = Task(id="m", title="clarify", type=TaskType.MANAGER, prompt="p")
    names = [n for n, _ in router.chain_for(task)]
    assert names == ["cc-sonnet-mgr", "cc-kimi"], f"unexpected manager chain: {names}"
    print("PASS: test_task_type_and_route")


def test_should_force_start():
    from hub.manager import should_force_start

    for t in ["开始", "开始吧", "直接开始", "go", "start", "就这样", "别问了，直接开始",
              "“开始”"]:
        assert should_force_start(t), f"expected force-start: {t!r}"
    for t in ["我还想加一个约束", "开始之前我想先说明一下背景",
              "什么时候开始比较好？", "预算大概 500 块"]:
        assert not should_force_start(t), f"false positive: {t!r}"
    print("PASS: test_should_force_start")


def test_is_confirmation():
    from hub.manager import is_confirmation

    for t in ["对", "对的", "没问题", "可以", "确认", "开始", "开始吧",
              "yes", "y", "ok", "confirm", "对，开始吧", "‘ok’"]:
        assert is_confirmation(t), f"expected confirmation: {t!r}"
    for t in ["不对，工期应该是两周", "等等", "再加一条约束：不许用外部库",
              "no", "目标里少写了移动端"]:
        assert not is_confirmation(t), f"false positive: {t!r}"
    print("PASS: test_is_confirmation")


class _CannedExecutor:
    """Returns scripted outputs in order; repeats the last one when the
    script runs out. Records every prompt it was given."""

    def __init__(self, outputs, ok=True, cost=0.01):
        self.outputs = list(outputs)
        self.ok = ok
        self.cost = cost
        self.calls = 0
        self.prompts = []
        self.on_events = []

    async def run(self, task, context, on_event=None):
        # 契约是 run(task, context, on_event=None) —— 桩以前漏了第三个参数
        self.prompts.append(task.prompt)
        self.on_events.append(on_event)
        out = self.outputs[min(self.calls, len(self.outputs) - 1)]
        self.calls += 1
        if not self.ok:
            return ExecResult(ok=False, error=out, cost_usd=self.cost)
        return ExecResult(ok=True, output=out, cost_usd=self.cost)


class _OneAgentRouter:
    def __init__(self, executor, name="fake-manager"):
        self._e = executor
        self._name = name

    def chain_for(self, task):
        return [(self._name, self._e)]


def test_manager_step_parses_asking_and_ready():
    from hub.manager import manager_step

    history = [{"role": "user", "text": "帮我写个爬虫"}]

    json_asking = '{"status": "asking", "question": "抓哪个网站？"}'
    ex = _CannedExecutor([json_asking])
    step = asyncio.run(manager_step(history, _OneAgentRouter(ex), "."))
    assert step["status"] == "asking" and step["question"] == "抓哪个网站？"
    assert step["_cost_usd"] == 0.01

    json_ready = '```json\n{"status": "ready", "goal": "G", "constraints": "C"}\n```'
    ex = _CannedExecutor([json_ready])
    step = asyncio.run(manager_step(history, _OneAgentRouter(ex), "."))
    assert step["status"] == "ready" and step["goal"] == "G" and step["constraints"] == "C", \
        f"fenced JSON must still parse, got {step}"
    print("PASS: test_manager_step_parses_asking_and_ready")


def test_manager_step_plaintext_fallback():
    """Unparseable output (no force_finish) degrades to a plain-text
    follow-up question instead of crashing."""
    from hub.manager import manager_step

    ex = _CannedExecutor(["预算大概是多少呢？"])
    step = asyncio.run(manager_step([{"role": "user", "text": "g"}], _OneAgentRouter(ex), "."))
    assert step["status"] == "asking" and step["question"] == "预算大概是多少呢？"
    print("PASS: test_manager_step_plaintext_fallback")


def test_manager_step_force_finish():
    """force_finish must (a) append the finalize instruction to the prompt,
    (b) retry once on unparseable output, (c) raise if still unparseable."""
    from hub.manager import manager_step

    json_ready = '{"status": "ready", "goal": "G", "constraints": ""}'
    ex = _CannedExecutor(["还没想好", json_ready])
    step = asyncio.run(manager_step([{"role": "user", "text": "g"}],
                                    _OneAgentRouter(ex), ".", force_finish=True))
    assert step["status"] == "ready" and ex.calls == 2, \
        f"expected one retry then success, got calls={ex.calls} step={step}"
    assert "MUST" in ex.prompts[0], "finalize instruction missing from forced prompt"

    ex = _CannedExecutor(["还没想好", "真的还没想好"])
    try:
        asyncio.run(manager_step([{"role": "user", "text": "g"}],
                                 _OneAgentRouter(ex), ".", force_finish=True))
        raise AssertionError("expected RuntimeError after retry exhausted")
    except RuntimeError:
        pass
    print("PASS: test_manager_step_force_finish")


def test_manager_step_raises_when_chain_exhausted():
    from hub.manager import manager_step

    ex = _CannedExecutor(["boom"], ok=False)
    try:
        asyncio.run(manager_step([{"role": "user", "text": "g"}], _OneAgentRouter(ex), "."))
        raise AssertionError("expected RuntimeError when every agent fails")
    except RuntimeError as e:
        assert "boom" in str(e)
    print("PASS: test_manager_step_raises_when_chain_exhausted")


class _TwoAgentRouter:
    """Fake router with a fixed-order chain and a real open_circuit()/
    chain_for() filtering behavior, mirroring hub.router.Router closely
    enough to test circuit-break composition (see test_circuit_breaker.py's
    _FakeRouter for the same pattern)."""

    def __init__(self, agents: dict):
        self._agents = agents
        self._order = list(agents.keys())
        self._circuit_open: set[str] = set()

    def chain_for(self, task):
        return [(n, self._agents[n]) for n in self._order if n not in self._circuit_open]

    def open_circuit(self, name):
        self._circuit_open.add(name)


class _KindExecutor:
    """Fails with a fixed error_kind for its first `fail_times` calls, then
    succeeds. fail_times=99 means "always fails" for this test's purposes."""

    def __init__(self, fail_times, error_kind, ready_goal="G"):
        self.fail_times = fail_times
        self.error_kind = error_kind
        self.ready_goal = ready_goal
        self.calls = 0

    async def run(self, task, context, on_event=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            return ExecResult(ok=False, error="boom", error_kind=self.error_kind, cost_usd=0.0)
        out = json.dumps({"status": "ready", "goal": self.ready_goal, "constraints": ""})
        return ExecResult(ok=True, output=out, cost_usd=0.01)


def test_manager_step_opens_circuit_on_unavailable():
    """An 'unavailable' failure must not be retried and must open the
    circuit on the router before falling through to the next agent."""
    from hub.manager import manager_step

    broken = _KindExecutor(fail_times=99, error_kind="unavailable")
    backup = _KindExecutor(fail_times=0, error_kind="", ready_goal="G2")
    router = _TwoAgentRouter({"broken": broken, "backup": backup})
    step = asyncio.run(manager_step([{"role": "user", "text": "g"}], router, "."))
    assert step["status"] == "ready" and step["goal"] == "G2"
    assert broken.calls == 1, f"unavailable must not be retried, got {broken.calls}"
    assert "broken" in router._circuit_open, "circuit must open on unavailable"
    print("PASS: test_manager_step_opens_circuit_on_unavailable")


def test_manager_step_transient_retries_same_agent():
    """A 'transient' failure that succeeds on the retry must NOT fall
    through to the next agent in the chain."""
    from hub.manager import manager_step

    flaky = _KindExecutor(fail_times=1, error_kind="transient", ready_goal="G")
    backup = _KindExecutor(fail_times=0, error_kind="", ready_goal="SHOULD_NOT_BE_USED")
    router = _TwoAgentRouter({"flaky": flaky, "backup": backup})
    step = asyncio.run(manager_step([{"role": "user", "text": "g"}], router, "."))
    assert step["status"] == "ready" and step["goal"] == "G"
    assert flaky.calls == 2, f"transient should retry the same agent once, got {flaky.calls}"
    assert backup.calls == 0, "must not fall through when the retry succeeds"
    print("PASS: test_manager_step_transient_retries_same_agent")


def test_manager_step_transient_falls_through_after_one_retry():
    """A 'transient' failure that fails again on the retry must fall
    through to the next agent -- exactly one retry, not an infinite loop."""
    from hub.manager import manager_step

    flaky = _KindExecutor(fail_times=99, error_kind="transient")
    backup = _KindExecutor(fail_times=0, error_kind="", ready_goal="G2")
    router = _TwoAgentRouter({"flaky": flaky, "backup": backup})
    step = asyncio.run(manager_step([{"role": "user", "text": "g"}], router, "."))
    assert step["status"] == "ready" and step["goal"] == "G2"
    assert flaky.calls == 2, f"expected exactly one retry (2 calls), got {flaky.calls}"
    assert backup.calls == 1
    print("PASS: test_manager_step_transient_falls_through_after_one_retry")


def test_manager_step_task_error_fallback_unchanged():
    """task_error (and empty error_kind) must keep falling through to the
    next agent on the FIRST failure -- no retry, no circuit-break."""
    from hub.manager import manager_step

    broken = _KindExecutor(fail_times=99, error_kind="task_error")
    backup = _KindExecutor(fail_times=0, error_kind="", ready_goal="G2")
    router = _TwoAgentRouter({"broken": broken, "backup": backup})
    step = asyncio.run(manager_step([{"role": "user", "text": "g"}], router, "."))
    assert step["status"] == "ready" and step["goal"] == "G2"
    assert broken.calls == 1, f"task_error must not be retried, got {broken.calls}"
    assert "broken" not in router._circuit_open, "task_error must not open the circuit"
    print("PASS: test_manager_step_task_error_fallback_unchanged")


def _make_ask(answers):
    """ask_user stub feeding scripted replies; records shown prompts."""
    it = iter(answers)
    shown = []

    async def ask(question):
        shown.append(question)
        return next(it)

    ask.shown = shown
    return ask


def test_loop_happy_path():
    """ask -> answer -> proposal -> confirm -> return (goal, constraints, cost)."""
    from hub.manager import run_clarification_loop

    ex = _CannedExecutor([
        '{"status": "asking", "question": "什么时候要？"}',
        '{"status": "ready", "goal": "G", "constraints": "C"}',
    ])
    ask = _make_ask(["下周前", "对"])
    goal, constraints, cost = asyncio.run(
        run_clarification_loop(ask, _OneAgentRouter(ex), ".", "帮我写个爬虫"))
    assert (goal, constraints) == ("G", "C")
    assert abs(cost - 0.02) < 1e-9, f"cost must sum both turns, got {cost}"
    assert len(ask.shown) == 2 and "G" in ask.shown[1], \
        "the proposal itself must be shown to the user before returning"
    print("PASS: test_loop_happy_path")


def test_loop_force_finish_at_turn_cap():
    """5 questions answered without resolution -> turn 6 is forced to
    finalize -> confirm step still happens."""
    from hub.manager import run_clarification_loop

    asking = '{"status": "asking", "question": "再多说点？"}'
    ex = _CannedExecutor([asking] * 5 + ['{"status": "ready", "goal": "G", "constraints": ""}'])
    ask = _make_ask(["a1", "a2", "a3", "a4", "a5", "对"])
    goal, constraints, cost = asyncio.run(
        run_clarification_loop(ask, _OneAgentRouter(ex), ".", "g"))
    assert goal == "G"
    assert ex.calls == 6
    assert "MUST" in ex.prompts[5], "turn 6 must carry the finalize instruction"
    assert "MUST" not in ex.prompts[4], "turn 5 must NOT yet be forced"
    print("PASS: test_loop_force_finish_at_turn_cap")


def test_loop_force_start_keyword():
    """'开始吧' as an answer skips remaining Q&A but NOT the confirm step."""
    from hub.manager import run_clarification_loop

    ex = _CannedExecutor([
        '{"status": "asking", "question": "预算？"}',
        '{"status": "ready", "goal": "G", "constraints": ""}',
    ])
    ask = _make_ask(["开始吧", "对"])
    goal, _, _ = asyncio.run(run_clarification_loop(ask, _OneAgentRouter(ex), ".", "g"))
    assert goal == "G"
    assert "MUST" in ex.prompts[1], "force-start must trigger the finalize instruction"
    assert len(ask.shown) == 2, "confirm step must still happen after force-start"
    print("PASS: test_loop_force_start_keyword")


def test_loop_correction_then_confirm():
    """proposal -> user correction -> new proposal -> confirm. Only the
    final confirmation returns; the correction must reach the manager."""
    from hub.manager import run_clarification_loop

    ex = _CannedExecutor([
        '{"status": "ready", "goal": "G1", "constraints": "C1"}',
        '{"status": "ready", "goal": "G2", "constraints": "C2"}',
    ])
    ask = _make_ask(["不对，用 Python 别用 Node", "对"])
    goal, constraints, _ = asyncio.run(
        run_clarification_loop(ask, _OneAgentRouter(ex), ".", "g"))
    assert (goal, constraints) == ("G2", "C2"), \
        f"must return the corrected proposal, got {(goal, constraints)}"
    assert "用 Python 别用 Node" in ex.prompts[1], \
        "the correction text must be fed back into the conversation"
    print("PASS: test_loop_correction_then_confirm")


def test_engine_writes_constraints_to_board():
    """Engine(constraints=...) must write goal/constraints right after
    goal/statement; the default (empty) must NOT create the key."""
    from hub.blackboard import Blackboard
    from hub.engine import Engine
    from hub.models import EventBus, Plan

    class _OkExecutor:
        async def run(self, task, context, on_event=None):
            return ExecResult(ok=True, output="done", cost_usd=0.0)

    class _FakeRouter:
        budget = {"max_usd_per_run": 2.0, "max_attempts_per_task": 2}

        def chain_for(self, task):
            return [("fake", _OkExecutor())]

        def animal(self, name):
            return "cat"

        def is_paid_api(self, name):
            return False

        def claude_code_agents(self):
            return []

        def open_circuit(self, name):
            pass

    def run_engine(constraints):
        events_path = os.path.join(os.path.dirname(__file__), "_scratch_events_m.jsonl")
        board_path = os.path.join(os.path.dirname(__file__), "_scratch_board_m.jsonl")
        for p in (events_path, board_path):
            if os.path.exists(p):
                os.remove(p)
        bus = EventBus(events_path)
        board = Blackboard(board_path, on_write=lambda e: None)
        plan = Plan(goal="test", tasks={})
        task = Task(id="t1", title="x", type=TaskType.CODING, prompt="p")
        plan.tasks[task.id] = task
        engine = Engine(plan, _FakeRouter(), bus, board, max_parallel=1,
                        preflight=False, constraints=constraints)
        asyncio.run(engine.run())
        bus.close()
        value = board.get("goal/constraints")
        for p in (events_path, board_path):
            if os.path.exists(p):
                os.remove(p)
        return value

    assert run_engine("不许用外部库") == "不许用外部库"
    assert run_engine("") is None, "empty constraints must not create the key"
    print("PASS: test_engine_writes_constraints_to_board")


def test_main_constraints_flag_end_to_end():
    """main.py --plan example_plan.json --mock --constraints '...' must
    land the text on the blackboard (mock run, zero cost)."""
    import subprocess

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    events_path = os.path.join(root, "tests", "_scratch_events_cli.jsonl")
    board_path = os.path.join(root, "tests", "_scratch_board_cli.jsonl")
    for p in (events_path, board_path):
        if os.path.exists(p):
            os.remove(p)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    p = subprocess.run(
        [sys.executable, "main.py", "--plan", "example_plan.json", "--mock",
         "--constraints", "预算 500 以内",
         "--events", events_path, "--board", board_path],
        cwd=root, capture_output=True, env=env)
    assert p.returncode == 0, f"mock run failed: {p.stderr.decode(errors='replace')[:500]}"
    found = None
    with open(board_path, encoding="utf-8") as f:
        for line in f:
            e = json.loads(line)
            if e["key"] == "goal/constraints":
                found = e
    assert found and found["value"] == "预算 500 以内", f"constraints entry wrong: {found}"
    assert found["author"] == "system"
    for p_ in (events_path, board_path):
        if os.path.exists(p_):
            os.remove(p_)
    print("PASS: test_main_constraints_flag_end_to_end")


def test_main_interactive_manager_unavailable_handled_gracefully():
    """main.py's --interactive branch must catch a RuntimeError from
    run_clarification_loop (manager route exhausted) and print a helpful
    fallback message + exit 1, instead of leaking a raw traceback.

    Unit-tested via monkeypatching hub.manager.run_clarification_loop:
    main.py does a *local* `from hub.manager import run_clarification_loop`
    inside the --interactive branch, so patching the module attribute
    before calling amain() is picked up correctly at call time. This
    avoids needing a mock LLM (which the CLI path has no hook for) while
    still exercising main.py's actual try/except, not a reimplementation
    of it. No subprocess, no real LLM calls."""
    import contextlib
    import io

    import hub.manager as manager_module
    import main as main_module

    async def boom(ask_user, router, workdir, initial_goal, max_turns=5):
        raise RuntimeError("manager unavailable: all agents failed")

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    events_path = os.path.join(root, "tests", "_scratch_events_main_interactive.jsonl")
    if os.path.exists(events_path):
        os.remove(events_path)

    original_loop = manager_module.run_clarification_loop
    original_argv = sys.argv
    manager_module.run_clarification_loop = boom
    sys.argv = ["main.py", "--goal", "帮我写个爬虫", "--interactive", "--events", events_path]
    try:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = asyncio.run(main_module.amain())
        assert rc == 1, f"expected exit code 1, got {rc}"
        assert "经理暂时不可用" in stderr.getvalue(), \
            f"expected the friendly fallback message on stderr, got {stderr.getvalue()!r}"
    finally:
        manager_module.run_clarification_loop = original_loop
        sys.argv = original_argv
        if os.path.exists(events_path):
            os.remove(events_path)
    print("PASS: test_main_interactive_manager_unavailable_handled_gracefully")


def test_interactive_flag_validation():
    """--interactive must be rejected (argparse exit code 2) without --goal
    or when combined with --plan / --constraints / --mock."""
    import subprocess

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bad_combos = [
        ["--interactive"],
        ["--interactive", "--plan", "example_plan.json"],
        ["--goal", "g", "--interactive", "--mock"],
        ["--goal", "g", "--interactive", "--constraints", "c"],
    ]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    for combo in bad_combos:
        p = subprocess.run([sys.executable, "main.py", *combo],
                           cwd=root, capture_output=True, env=env)
        assert p.returncode == 2, \
            f"{combo} should exit 2, got {p.returncode}: {p.stderr.decode(errors='replace')[:200]}"
    print("PASS: test_interactive_flag_validation")


if __name__ == "__main__":
    test_task_type_and_route()
    test_should_force_start()
    test_is_confirmation()
    test_manager_step_parses_asking_and_ready()
    test_manager_step_plaintext_fallback()
    test_manager_step_force_finish()
    test_manager_step_raises_when_chain_exhausted()
    test_manager_step_opens_circuit_on_unavailable()
    test_manager_step_transient_retries_same_agent()
    test_manager_step_transient_falls_through_after_one_retry()
    test_manager_step_task_error_fallback_unchanged()
    test_loop_happy_path()
    test_loop_force_finish_at_turn_cap()
    test_loop_force_start_keyword()
    test_loop_correction_then_confirm()
    test_engine_writes_constraints_to_board()
    test_main_constraints_flag_end_to_end()
    test_main_interactive_manager_unavailable_handled_gracefully()
    test_interactive_flag_validation()
    print("\nAll manager tests passed.")
