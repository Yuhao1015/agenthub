"""Summarizer module tests. No framework -- plain assert, exit 1 on failure.
All routers/executors are fakes; zero real LLM calls.

Run: python tests/test_summarizer.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.executors import ExecResult
from hub.models import TaskType


class _ScriptedExecutor:
    """Returns each ExecResult in order; raises IndexError if called more
    times than scripted (a bug in the test, not the code under test)."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    async def run(self, task, context):
        result = self.results[self.calls]
        self.calls += 1
        return result


class _FakeRouter:
    """chain_for() ignores the task and always returns the same scripted
    chain -- summarize() always builds a LIGHT/easy pseudo-task, so a real
    Router would always route it to the same economy chain too."""

    def __init__(self, chain):
        self._chain = chain  # list[(name, executor)]
        self._circuit_open = set()

    def chain_for(self, task):
        assert task.type == TaskType.LIGHT, f"expected LIGHT, got {task.type}"
        assert task.difficulty == "easy", f"expected easy, got {task.difficulty}"
        return [(n, e) for n, e in self._chain if n not in self._circuit_open]

    def open_circuit(self, name):
        self._circuit_open.add(name)


class _NoRouteRouter:
    """Simulates a misconfigured capability_matrix.yaml with no economy
    tier -- the real Router.chain_for() raises ValueError in this case."""

    def chain_for(self, task):
        raise ValueError("no route for task type 'light' (difficulty=easy)")

    def open_circuit(self, name):
        pass


def test_prompt_includes_title_and_output():
    from hub.summarizer import summarize

    captured = {}

    class _CapturingExecutor:
        async def run(self, task, context):
            captured["prompt"] = task.prompt
            captured["type"] = task.type
            captured["difficulty"] = task.difficulty
            return ExecResult(ok=True, output="a short summary", cost_usd=0.0)

    router = _FakeRouter([("cc-hy3", _CapturingExecutor())])
    text, agent, cost_usd, cost_credits = asyncio.run(
        summarize("the original long output text here", "scrape HN to CSV", router))

    assert text == "a short summary"
    assert agent == "cc-hy3"
    assert "scrape HN to CSV" in captured["prompt"]
    assert "the original long output text here" in captured["prompt"]
    assert captured["type"] == TaskType.LIGHT
    assert captured["difficulty"] == "easy"
    print("PASS: test_prompt_includes_title_and_output")


def test_success_first_agent():
    from hub.summarizer import summarize

    ex = _ScriptedExecutor([ExecResult(ok=True, output="  summary text  ",
                                        cost_usd=0.02, cost_credits=0.5)])
    router = _FakeRouter([("cc-hy3", ex)])
    text, agent, cost_usd, cost_credits = asyncio.run(
        summarize("x" * 700, "title", router))

    assert text == "summary text", f"output must be stripped, got {text!r}"
    assert agent == "cc-hy3"
    assert cost_usd == 0.02
    assert cost_credits == 0.5
    assert ex.calls == 1
    print("PASS: test_success_first_agent")


def test_transient_retries_same_agent_then_succeeds():
    from hub.summarizer import summarize

    ex = _ScriptedExecutor([
        ExecResult(ok=False, error="connection reset", error_kind="transient", cost_usd=0.01),
        ExecResult(ok=True, output="summary after retry", cost_usd=0.02),
    ])
    router = _FakeRouter([("cc-hy3", ex)])
    text, agent, cost_usd, cost_credits = asyncio.run(
        summarize("x" * 700, "title", router))

    assert text == "summary after retry"
    assert agent == "cc-hy3"
    assert ex.calls == 2, "must retry the SAME agent once on transient"
    assert round(cost_usd, 4) == 0.03, f"both attempts' cost must accumulate, got {cost_usd}"
    print("PASS: test_transient_retries_same_agent_then_succeeds")


def test_transient_retry_that_comes_back_unavailable_opens_circuit():
    from hub.summarizer import summarize

    ex1 = _ScriptedExecutor([
        ExecResult(ok=False, error="connection reset", error_kind="transient", cost_usd=0.01),
        ExecResult(ok=False, error="credits required", error_kind="unavailable", cost_usd=0.0),
    ])
    ex2 = _ScriptedExecutor([ExecResult(ok=True, output="from backup", cost_usd=0.01)])
    router = _FakeRouter([("cc-hy3", ex1), ("cc-deepseek-cb", ex2)])
    text, agent, cost_usd, cost_credits = asyncio.run(
        summarize("x" * 700, "title", router))

    assert text == "from backup"
    assert ex1.calls == 2, "transient retry happens once before re-checking error_kind"
    assert "cc-hy3" in router._circuit_open, \
        "the retry's error_kind (unavailable) must still open the circuit"
    print("PASS: test_transient_retry_that_comes_back_unavailable_opens_circuit")


def test_transient_retry_fails_moves_to_next_agent():
    from hub.summarizer import summarize

    ex1 = _ScriptedExecutor([
        ExecResult(ok=False, error="connection reset", error_kind="transient", cost_usd=0.01),
        ExecResult(ok=False, error="still broken", error_kind="task_error", cost_usd=0.01),
    ])
    ex2 = _ScriptedExecutor([ExecResult(ok=True, output="from second agent", cost_usd=0.02)])
    router = _FakeRouter([("cc-hy3", ex1), ("cc-deepseek-cb", ex2)])
    text, agent, cost_usd, cost_credits = asyncio.run(
        summarize("x" * 700, "title", router))

    assert text == "from second agent"
    assert agent == "cc-deepseek-cb"
    assert ex1.calls == 2, "one retry, not more, before giving up on this agent"
    assert ex2.calls == 1
    assert round(cost_usd, 4) == 0.04
    print("PASS: test_transient_retry_fails_moves_to_next_agent")


def test_unavailable_opens_circuit_and_moves_on():
    from hub.summarizer import summarize

    ex1 = _ScriptedExecutor([
        ExecResult(ok=False, error="credits required", error_kind="unavailable", cost_usd=0.0),
    ])
    ex2 = _ScriptedExecutor([ExecResult(ok=True, output="from backup", cost_usd=0.01)])
    router = _FakeRouter([("cc-hy3", ex1), ("cc-deepseek-cb", ex2)])
    text, agent, cost_usd, cost_credits = asyncio.run(
        summarize("x" * 700, "title", router))

    assert text == "from backup"
    assert agent == "cc-deepseek-cb"
    assert ex1.calls == 1, "unavailable must NOT retry the same agent"
    assert "cc-hy3" in router._circuit_open
    print("PASS: test_unavailable_opens_circuit_and_moves_on")


def test_task_error_moves_on_without_retry():
    from hub.summarizer import summarize

    ex1 = _ScriptedExecutor([ExecResult(ok=False, error="garbage output",
                                         error_kind="task_error", cost_usd=0.01)])
    ex2 = _ScriptedExecutor([ExecResult(ok=True, output="from backup", cost_usd=0.01)])
    router = _FakeRouter([("cc-hy3", ex1), ("cc-deepseek-cb", ex2)])
    text, agent, cost_usd, cost_credits = asyncio.run(
        summarize("x" * 700, "title", router))

    assert text == "from backup"
    assert ex1.calls == 1, "task_error must not retry"
    assert "cc-hy3" not in router._circuit_open, "task_error must NOT circuit-break"
    print("PASS: test_task_error_moves_on_without_retry")


def test_all_agents_exhausted_returns_none_with_cost():
    from hub.summarizer import summarize

    ex1 = _ScriptedExecutor([ExecResult(ok=False, error="broken",
                                         error_kind="task_error", cost_usd=0.01)])
    ex2 = _ScriptedExecutor([ExecResult(ok=False, error="also broken",
                                         error_kind="task_error", cost_usd=0.01)])
    router = _FakeRouter([("cc-hy3", ex1), ("cc-deepseek-cb", ex2)])
    text, agent, cost_usd, cost_credits = asyncio.run(
        summarize("x" * 700, "title", router))

    assert text is None
    assert agent is None
    assert round(cost_usd, 4) == 0.02, "cost from failed attempts must still be reported"
    print("PASS: test_all_agents_exhausted_returns_none_with_cost")


def test_no_route_configured_returns_none_gracefully():
    from hub.summarizer import summarize

    text, agent, cost_usd, cost_credits = asyncio.run(
        summarize("x" * 700, "title", _NoRouteRouter()))
    assert text is None and agent is None
    assert cost_usd == 0.0 and cost_credits == 0.0
    print("PASS: test_no_route_configured_returns_none_gracefully")


if __name__ == "__main__":
    test_prompt_includes_title_and_output()
    test_success_first_agent()
    test_transient_retries_same_agent_then_succeeds()
    test_transient_retry_that_comes_back_unavailable_opens_circuit()
    test_transient_retry_fails_moves_to_next_agent()
    test_unavailable_opens_circuit_and_moves_on()
    test_task_error_moves_on_without_retry()
    test_all_agents_exhausted_returns_none_with_cost()
    test_no_route_configured_returns_none_gracefully()
    print("\nAll summarizer tests passed.")
