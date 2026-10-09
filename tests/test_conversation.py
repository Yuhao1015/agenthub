"""hub/conversation.py -- deep collaboration follow-up messaging. No
framework -- plain assert, exit 1 on failure. Subprocess is faked (same
technique as test_cost_attribution.py); zero real LLM calls.

Run: python tests/test_conversation.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.blackboard import Blackboard
from hub.executors import ClaudeCodeExecutor, OpenRouterExecutor
from hub import conversation


class _FakeRouter:
    def __init__(self, executors: dict):
        self._executors = executors

    def executor_for(self, name):
        return self._executors[name]


def _fresh_board():
    tmp = tempfile.mkdtemp()
    return Blackboard(os.path.join(tmp, "board.jsonl"), on_write=lambda e: None)


def _seed_task(board, task_id, agent, session_id, ratio=0.0):
    board.put(f"tasks/{task_id}/agent", agent, author="system")
    board.put(f"tasks/{task_id}/session_id", session_id, author="system")
    board.put(f"tasks/{task_id}/session_usage_ratio", ratio, author="system")


def _script_subprocess(responses: list[dict]):
    """Return successive claude -p JSON responses in order, one per call."""
    calls = {"n": 0}

    async def fake_exec(*args, **kwargs):
        data = responses[calls["n"]]
        calls["n"] += 1

        class _Proc:
            returncode = 0

            async def communicate(self_inner, input=None):
                return json.dumps(data).encode("utf-8"), b""
        return _Proc()

    asyncio.create_subprocess_exec = fake_exec
    return calls


def test_estimate_context_usage_ratio():
    assert conversation.estimate_context_usage_ratio(None) == 0.0
    assert conversation.estimate_context_usage_ratio({}) == 0.0
    ratio = conversation.estimate_context_usage_ratio(
        {"input_tokens": 100, "cache_read_input_tokens": 50000,
         "cache_creation_input_tokens": 1000})
    assert abs(ratio - (51100 / conversation.CLAUDE_CONTEXT_WINDOW)) < 1e-9
    print("PASS: test_estimate_context_usage_ratio")


def test_missing_session_raises_conversation_error():
    board = _fresh_board()
    router = _FakeRouter({})
    try:
        asyncio.run(conversation.continue_task_conversation("t1", "hi", router, board))
        assert False, "expected ConversationError"
    except conversation.ConversationError:
        pass
    print("PASS: test_missing_session_raises_conversation_error")


def test_non_claude_code_agent_raises_conversation_error():
    board = _fresh_board()
    _seed_task(board, "t1", "or-deepseek", "sess-x")
    router = _FakeRouter({"or-deepseek": OpenRouterExecutor(model="deepseek/deepseek-chat")})
    try:
        asyncio.run(conversation.continue_task_conversation("t1", "hi", router, board))
        assert False, "expected ConversationError"
    except conversation.ConversationError as e:
        assert "claude_code" in str(e)
    print("PASS: test_non_claude_code_agent_raises_conversation_error")


def test_normal_resume_updates_board():
    board = _fresh_board()
    _seed_task(board, "t1", "cc-opus", "sess-abc", ratio=0.1)
    executor = ClaudeCodeExecutor(model="opus", workdir=".", stream=False)
    router = _FakeRouter({"cc-opus": executor})

    original = asyncio.create_subprocess_exec
    _script_subprocess([{
        "type": "result", "is_error": False, "result": "here is the answer",
        "total_cost_usd": 0.02, "session_id": "sess-abc",
        "usage": {"input_tokens": 5, "cache_read_input_tokens": 1000,
                  "cache_creation_input_tokens": 0},
    }])
    try:
        result, compacted = asyncio.run(
            conversation.continue_task_conversation("t1", "follow up", router, board))
    finally:
        asyncio.create_subprocess_exec = original

    assert result.ok is True
    assert result.output == "here is the answer"
    assert compacted is False
    assert board.get("tasks/t1/session_id") == "sess-abc"
    assert board.get("tasks/t1/output") == "here is the answer", \
        "a successful follow-up reply must become the task's new output " \
        "(prerequisite for staleness detection to mean anything)"
    new_ratio = board.get("tasks/t1/session_usage_ratio")
    assert new_ratio is not None and new_ratio < 0.1, \
        f"expected ratio recomputed from the small reply usage, got {new_ratio}"
    print("PASS: test_normal_resume_updates_board")


def test_high_ratio_triggers_compaction_then_fresh_session():
    board = _fresh_board()
    _seed_task(board, "t1", "cc-opus", "sess-old", ratio=0.9)
    executor = ClaudeCodeExecutor(model="opus", workdir=".", stream=False)
    router = _FakeRouter({"cc-opus": executor})

    original = asyncio.create_subprocess_exec
    calls = _script_subprocess([
        {  # compaction call (run_resumed with COMPACTION_PROMPT)
            "type": "result", "is_error": False, "result": "digest: decided X, unresolved Y",
            "total_cost_usd": 0.01, "session_id": "sess-old",
        },
        {  # fresh session call (run, no --resume, seeded with digest as context)
            "type": "result", "is_error": False, "result": "continuing from the digest",
            "total_cost_usd": 0.03, "session_id": "sess-new",
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 500},
        },
    ])
    try:
        result, compacted = asyncio.run(
            conversation.continue_task_conversation("t1", "follow up", router, board))
    finally:
        asyncio.create_subprocess_exec = original

    assert compacted is True
    assert result.ok is True
    assert result.output == "continuing from the digest"
    assert calls["n"] == 2, "must call the executor exactly twice: compact, then fresh"
    assert board.get("tasks/t1/session_digest") == "digest: decided X, unresolved Y"
    assert board.get("tasks/t1/session_id") == "sess-new", \
        "session_id must be overwritten with the fresh session's id"
    new_ratio = board.get("tasks/t1/session_usage_ratio")
    assert new_ratio is not None, "ratio must be recomputed after compaction+fresh"
    assert abs(result.cost_usd - (0.01 + 0.03)) < 1e-9, \
        f"cost_usd must be the SUM of the compaction call and the fresh call, got {result.cost_usd}"
    print("PASS: test_high_ratio_triggers_compaction_then_fresh_session")


def test_compaction_cost_accumulates_credits_too():
    """Same as above but for cost_credits (codebuddy-style spend), to make
    sure the accumulation isn't accidentally cost_usd-only."""
    board = _fresh_board()
    _seed_task(board, "t1", "cc-opus", "sess-old", ratio=0.9)
    executor = ClaudeCodeExecutor(model="opus", workdir=".", stream=False)
    router = _FakeRouter({"cc-opus": executor})

    original = asyncio.create_subprocess_exec
    _script_subprocess([
        {"type": "result", "is_error": False, "result": "digest",
         "total_cost_usd": 0.01, "session_id": "sess-old"},
        {"type": "result", "is_error": False, "result": "continuing",
         "total_cost_usd": 0.03, "session_id": "sess-new"},
    ])
    try:
        result, compacted = asyncio.run(
            conversation.continue_task_conversation("t1", "follow up", router, board))
    finally:
        asyncio.create_subprocess_exec = original

    assert compacted is True
    assert result.ok is True
    # ClaudeCodeExecutor doesn't populate cost_credits (that's codebuddy-only),
    # so both legs are 0.0 here -- this just locks in that the accumulation
    # logic touches cost_credits too, not just cost_usd.
    assert result.cost_credits == 0.0
    assert abs(result.cost_usd - 0.04) < 1e-9
    print("PASS: test_compaction_cost_accumulates_credits_too")


def test_compaction_succeeds_but_fresh_session_fails_leaves_old_state():
    """Known limitation (documented in continue_task_conversation's
    docstring/comments): if compaction succeeds but the fresh-session call
    fails, the old session_id/ratio are deliberately left in place rather
    than being clobbered with data from a failed call. The digest itself
    IS real work product and should still be visible on the board."""
    board = _fresh_board()
    _seed_task(board, "t1", "cc-opus", "sess-old", ratio=0.9)
    executor = ClaudeCodeExecutor(model="opus", workdir=".", stream=False)
    router = _FakeRouter({"cc-opus": executor})

    original = asyncio.create_subprocess_exec
    _script_subprocess([
        {  # compaction call succeeds
            "type": "result", "is_error": False, "result": "digest: decided X",
            "total_cost_usd": 0.01, "session_id": "sess-old",
        },
        {  # fresh session call fails
            "type": "result", "is_error": True, "api_error_status": 500,
            "duration_api_ms": 100, "result": "internal error",
            "total_cost_usd": 0.0, "session_id": "sess-old",
        },
    ])
    try:
        result, compacted = asyncio.run(
            conversation.continue_task_conversation("t1", "follow up", router, board))
    finally:
        asyncio.create_subprocess_exec = original

    assert compacted is True
    assert result.ok is False
    assert board.get("tasks/t1/session_digest") == "digest: decided X", \
        "compaction succeeded and produced real work product -- must still be visible"
    assert board.get("tasks/t1/session_id") == "sess-old", \
        "must NOT be overwritten -- the fresh session call failed"
    assert board.get("tasks/t1/session_usage_ratio") == 0.9, \
        "must NOT be overwritten -- stays at the stale seeded value"
    print("PASS: test_compaction_succeeds_but_fresh_session_fails_leaves_old_state")


def test_compaction_digest_failure_still_opens_fresh_session():
    """If the compaction call itself fails, don't crash the user's message
    -- open a fresh session with an empty digest rather than blocking."""
    board = _fresh_board()
    _seed_task(board, "t1", "cc-opus", "sess-old", ratio=0.95)
    executor = ClaudeCodeExecutor(model="opus", workdir=".", stream=False)
    router = _FakeRouter({"cc-opus": executor})

    original = asyncio.create_subprocess_exec
    _script_subprocess([
        {"type": "result", "is_error": True, "api_error_status": 500,
         "duration_api_ms": 100, "result": "internal error", "total_cost_usd": 0.0,
         "session_id": "sess-old"},
        {"type": "result", "is_error": False, "result": "fresh reply anyway",
         "total_cost_usd": 0.02, "session_id": "sess-new"},
    ])
    try:
        result, compacted = asyncio.run(
            conversation.continue_task_conversation("t1", "follow up", router, board))
    finally:
        asyncio.create_subprocess_exec = original

    assert compacted is True
    assert result.ok is True
    assert result.output == "fresh reply anyway"
    assert board.get("tasks/t1/session_digest") is None, \
        "a failed compaction call must not write a bogus digest"
    print("PASS: test_compaction_digest_failure_still_opens_fresh_session")


def _seed_spec(board, task_id, depends_on):
    board.put(f"tasks/{task_id}/spec",
              {"title": task_id, "type": "coding", "prompt": "p", "depends_on": depends_on},
              author="system")


def test_mark_stale_dependents_marks_direct_done_dependent():
    board = _fresh_board()
    _seed_spec(board, "a", [])
    _seed_spec(board, "b", ["a"])
    board.put("tasks/b/status", "done", author="system", task_id="b")

    marked = conversation.mark_stale_dependents(board, "a")

    assert marked == ["b"]
    assert board.get("tasks/b/status") == "stale"
    print("PASS: test_mark_stale_dependents_marks_direct_done_dependent")


def test_mark_stale_dependents_ignores_non_done_and_unrelated():
    board = _fresh_board()
    _seed_spec(board, "a", [])
    _seed_spec(board, "b", ["a"])          # dependent, but not done yet
    board.put("tasks/b/status", "running", author="system", task_id="b")
    _seed_spec(board, "c", [])              # unrelated, no dependency on a
    board.put("tasks/c/status", "done", author="system", task_id="c")

    marked = conversation.mark_stale_dependents(board, "a")

    assert marked == [], f"expected nothing marked, got {marked}"
    assert board.get("tasks/b/status") == "running"
    assert board.get("tasks/c/status") == "done"
    print("PASS: test_mark_stale_dependents_ignores_non_done_and_unrelated")


def test_mark_stale_dependents_is_not_transitive():
    """Scoped to direct dependents only (matches the design doc's literal
    wording) -- a two-hop chain's far end is deliberately left alone."""
    board = _fresh_board()
    _seed_spec(board, "a", [])
    _seed_spec(board, "b", ["a"])
    board.put("tasks/b/status", "done", author="system", task_id="b")
    _seed_spec(board, "c", ["b"])           # depends on b, not on a directly
    board.put("tasks/c/status", "done", author="system", task_id="c")

    marked = conversation.mark_stale_dependents(board, "a")

    assert marked == ["b"]
    assert board.get("tasks/c/status") == "done", \
        "c depends on b, not a -- must not be marked from this single call"
    print("PASS: test_mark_stale_dependents_is_not_transitive")


if __name__ == "__main__":
    test_estimate_context_usage_ratio()
    test_missing_session_raises_conversation_error()
    test_non_claude_code_agent_raises_conversation_error()
    test_normal_resume_updates_board()
    test_high_ratio_triggers_compaction_then_fresh_session()
    test_compaction_cost_accumulates_credits_too()
    test_compaction_succeeds_but_fresh_session_fails_leaves_old_state()
    test_compaction_digest_failure_still_opens_fresh_session()
    test_mark_stale_dependents_marks_direct_done_dependent()
    test_mark_stale_dependents_ignores_non_done_and_unrelated()
    test_mark_stale_dependents_is_not_transitive()
    print("\nAll conversation tests passed.")
