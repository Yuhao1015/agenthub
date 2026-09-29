"""Streaming thinking: on_event callback wiring from Executor -> Engine ->
agent_thinking events. No framework -- plain assert, exit 1 on failure.
Zero real subprocess/LLM calls unless explicitly noted.

Run: python tests/test_agent_thinking.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.executors import ExecResult
from hub.models import Task, TaskType


def test_engine_wires_on_event_to_agent_thinking_events():
    """Engine must pass a working on_event callback into executor.run();
    calling it must emit agent_thinking with the right task_id/agent/detail,
    in the order the executor called it."""
    from hub.blackboard import Blackboard
    from hub.engine import Engine
    from hub.models import EventBus, Plan

    class _ChattyExecutor:
        async def run(self, task, context, on_event=None):
            if on_event:
                on_event("正在思考…")
                on_event("Read(CLAUDE.md)")
            return ExecResult(ok=True, output="done", cost_usd=0.01)

    class _FakeRouter:
        budget = {"max_usd_per_run": 2.0, "max_attempts_per_task": 2}

        def chain_for(self, task):
            return [("cc-sonnet", _ChattyExecutor())]

        def animal(self, name):
            return "corgi"

        def is_paid_api(self, name):
            return False

    events_path = os.path.join(os.path.dirname(__file__), "_scratch_events_thinking.jsonl")
    board_path = os.path.join(os.path.dirname(__file__), "_scratch_board_thinking.jsonl")
    for p in (events_path, board_path):
        if os.path.exists(p):
            os.remove(p)

    bus = EventBus(events_path)
    board = Blackboard(board_path, on_write=lambda e: None)
    plan = Plan(goal="test", tasks={})
    task = Task(id="t1", title="x", type=TaskType.CODING, prompt="p")
    plan.tasks[task.id] = task

    engine = Engine(plan, _FakeRouter(), bus, board, max_parallel=1)
    asyncio.run(engine._run_task(task))
    bus.close()

    with open(events_path, encoding="utf-8") as f:
        events = [json.loads(l) for l in f if l.strip()]
    thinking = [e for e in events if e["type"] == "agent_thinking"]
    assert len(thinking) == 2, f"expected 2 agent_thinking events, got {len(thinking)}"
    assert thinking[0]["task_id"] == "t1" and thinking[0]["agent"] == "cc-sonnet"
    assert thinking[0]["detail"] == "正在思考…"
    assert thinking[1]["detail"] == "Read(CLAUDE.md)"

    os.remove(events_path)
    if os.path.exists(board_path):
        os.remove(board_path)
    print("PASS: test_engine_wires_on_event_to_agent_thinking_events")


def test_executor_run_without_on_event_still_works():
    """Calling .run(task, context) the old (positional, no on_event) way
    must not raise -- on_event defaults to None. Regression guard for
    every executor that hasn't been touched yet (build_executor() default
    kind, or callers like hub/manager.py that don't pass on_event)."""
    from hub.executors import MockExecutor

    task = Task(id="t1", title="x", type=TaskType.CODING, prompt="p")
    result = asyncio.run(MockExecutor(delay=0).run(task, ""))
    assert result.ok is True
    print("PASS: test_executor_run_without_on_event_still_works")


class _FakeStreamProc:
    """Fake asyncio subprocess for streaming executors: readline() pops
    queued lines one at a time, an empty bytes signals EOF -- mirrors real
    asyncio.StreamReader.readline() behavior closely enough for the
    line-by-line parsing loop under test."""

    def __init__(self, lines: list, returncode: int = 0, stderr: bytes = b""):
        self._lines = list(lines)
        self.returncode = returncode
        self._stderr = stderr
        self.stdout = self
        self.stderr = self

    async def readline(self):
        if not self._lines:
            return b""
        return self._lines.pop(0)

    async def read(self):
        return self._stderr

    async def wait(self):
        return self.returncode

    def kill(self):
        pass


def _patch_stream_subprocess(lines: list, returncode: int = 0, stderr: bytes = b""):
    """lines: list of dicts (each becomes one NDJSON line) or raw bytes."""
    byte_lines = [
        (json.dumps(l).encode("utf-8") + b"\n") if isinstance(l, dict) else l
        for l in lines
    ]

    async def fake_exec(*args, **kwargs):
        return _FakeStreamProc(byte_lines, returncode, stderr)
    asyncio.create_subprocess_exec = fake_exec


# Condensed but structurally real fixture -- shape confirmed via a live probe
# 2026-07-23 (claude -p ... --output-format stream-json --verbose
# --include-partial-messages against sonnet, reading CLAUDE.md then
# summarizing). stream_event/system/user lines are included to prove the
# parser correctly ignores them; only the three "assistant" lines and the
# final "result" line should produce observable effects.
_CLAUDE_STREAM_LINES = [
    {"type": "system", "subtype": "init", "cwd": ".", "session_id": "s1"},
    {"type": "stream_event", "event": {"type": "message_start",
     "message": {"model": "claude-sonnet-5", "content": []}}},
    {"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
     "content_block": {"type": "thinking", "thinking": "", "signature": ""}}},
    {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
     "delta": {"type": "signature_delta", "signature": "abc123"}}},
    {"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "", "signature": "abc123"}]}},
    {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
    {"type": "stream_event", "event": {"type": "content_block_start", "index": 1,
     "content_block": {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}}},
    {"type": "stream_event", "event": {"type": "content_block_delta", "index": 1,
     "delta": {"type": "input_json_delta", "partial_json": "{\"file_path\": \"CLAUDE.md\"}"}}},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "CLAUDE.md"}}]}},
    {"type": "stream_event", "event": {"type": "content_block_stop", "index": 1}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
     "content": "# CLAUDE.md\n..."}]}},
    {"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
     "content_block": {"type": "text", "text": ""}}},
    {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
     "delta": {"type": "text_delta", "text": "Section 1 is about the project."}}},
    {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Section 1 is about the project."}]}},
    {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
    {"type": "result", "subtype": "success", "is_error": False,
     "result": "Section 1 is about the project.", "total_cost_usd": 0.0523,
     "usage": {"input_tokens": 100, "output_tokens": 20}},
]


def test_claude_streaming_reports_thinking_tool_and_text():
    """Only the 3 'assistant' snapshot lines should produce on_event calls
    -- one generic placeholder for the empty-text thinking block, one
    tool-use summary, one text preview. All the interleaved stream_event/
    system/user lines must be silently ignored."""
    from hub.executors import ClaudeCodeExecutor

    original = asyncio.create_subprocess_exec
    _patch_stream_subprocess(_CLAUDE_STREAM_LINES)
    calls = []
    try:
        task = Task(id="t1", title="x", type=TaskType.ANALYSIS, prompt="p")
        executor = ClaudeCodeExecutor(model="sonnet", workdir=".")
        result = asyncio.run(executor.run(task, "", on_event=calls.append))
    finally:
        asyncio.create_subprocess_exec = original

    assert calls == ["正在思考…", "Read(CLAUDE.md)", "Section 1 is about the project."], \
        f"unexpected on_event sequence: {calls}"
    assert result.ok is True
    assert result.output == "Section 1 is about the project."
    assert result.cost_usd == 0.0523
    assert result.usage == {"input_tokens": 100, "output_tokens": 20}
    print("PASS: test_claude_streaming_reports_thinking_tool_and_text")


def test_claude_streaming_error_classified_same_as_blocking():
    """The streaming path's final classification must match the blocking
    path's for the exact same error shape (structural 429 -> unavailable)."""
    from hub.executors import ClaudeCodeExecutor

    original = asyncio.create_subprocess_exec
    _patch_stream_subprocess([
        {"type": "result", "subtype": "error_during_execution", "is_error": True,
         "api_error_status": 429, "duration_api_ms": 0,
         "result": "API Error: Usage credits required for 1M context",
         "total_cost_usd": 0.0173},
    ])
    try:
        task = Task(id="t1", title="x", type=TaskType.PLANNING, prompt="p")
        executor = ClaudeCodeExecutor(model="opus", workdir=".")
        result = asyncio.run(executor.run(task, ""))
    finally:
        asyncio.create_subprocess_exec = original

    assert result.ok is False
    assert result.error_kind == "unavailable", \
        f"structural 429 (duration_api_ms=0) must classify unavailable, got {result.error_kind}"
    assert result.cost_usd == 0.0173
    print("PASS: test_claude_streaming_error_classified_same_as_blocking")


def test_claude_streaming_on_event_none_does_not_crash():
    """Not passing on_event at all (the old 2-positional-arg calling
    convention) must still work under the new streaming default."""
    from hub.executors import ClaudeCodeExecutor

    original = asyncio.create_subprocess_exec
    _patch_stream_subprocess(_CLAUDE_STREAM_LINES)
    try:
        task = Task(id="t1", title="x", type=TaskType.ANALYSIS, prompt="p")
        result = asyncio.run(ClaudeCodeExecutor(model="sonnet", workdir=".").run(task, ""))
    finally:
        asyncio.create_subprocess_exec = original

    assert result.ok is True
    print("PASS: test_claude_streaming_on_event_none_does_not_crash")


def test_claude_streaming_malformed_line_skipped_not_fatal():
    """A line that isn't valid JSON (or isn't a dict) must be skipped, not
    crash the task -- the final result line after it must still be used."""
    from hub.executors import ClaudeCodeExecutor

    original = asyncio.create_subprocess_exec
    _patch_stream_subprocess([
        b"not json at all\n",
        json.dumps(["also", "not", "a", "dict"]).encode() + b"\n",
        {"type": "result", "subtype": "success", "is_error": False,
         "result": "fine", "total_cost_usd": 0.01},
    ])
    try:
        task = Task(id="t1", title="x", type=TaskType.CODING, prompt="p")
        result = asyncio.run(ClaudeCodeExecutor(model="sonnet", workdir=".").run(task, ""))
    finally:
        asyncio.create_subprocess_exec = original

    assert result.ok is True
    assert result.output == "fine"
    print("PASS: test_claude_streaming_malformed_line_skipped_not_fatal")


def test_claude_no_result_line_is_task_error():
    """If the process exits without ever emitting a 'result' line (crash
    mid-stream), that must be a task_error, not a silent success."""
    from hub.executors import ClaudeCodeExecutor

    original = asyncio.create_subprocess_exec
    _patch_stream_subprocess([
        {"type": "system", "subtype": "init"},
    ], stderr=b"claude crashed before finishing")
    try:
        task = Task(id="t1", title="x", type=TaskType.CODING, prompt="p")
        result = asyncio.run(ClaudeCodeExecutor(model="sonnet", workdir=".").run(task, ""))
    finally:
        asyncio.create_subprocess_exec = original

    assert result.ok is False
    assert result.error_kind == "task_error"
    assert "crashed" in result.error
    print("PASS: test_claude_no_result_line_is_task_error")


def test_claude_streaming_oversized_line_is_task_error_not_crash():
    """A line bigger than the StreamReader buffer must degrade to a
    graceful task_error, never crash the task with an uncaught
    LimitOverrunError."""
    from hub.executors import ClaudeCodeExecutor

    class _OverrunProc:
        def __init__(self):
            self.returncode = 0
            self.stdout = self
            self.stderr = self

        async def readline(self):
            raise asyncio.LimitOverrunError("line too long", 999)

        async def read(self):
            return b""

        async def wait(self):
            return self.returncode

        def kill(self):
            pass

    original = asyncio.create_subprocess_exec
    async def fake_exec(*args, **kwargs):
        return _OverrunProc()
    asyncio.create_subprocess_exec = fake_exec
    try:
        task = Task(id="t1", title="x", type=TaskType.CODING, prompt="p")
        result = asyncio.run(ClaudeCodeExecutor(model="sonnet", workdir=".").run(task, ""))
    finally:
        asyncio.create_subprocess_exec = original

    assert result.ok is False
    assert result.error_kind == "task_error"
    print("PASS: test_claude_streaming_oversized_line_is_task_error_not_crash")


# Condensed but structurally real fixture -- shape confirmed via a live
# probe 2026-07-23 against the hy3 model (workbuddy's free tier, 0 credit
# spent). Unlike claude, the thinking block carries real readable text.
_CODEBUDDY_STREAM_LINES = [
    {"type": "system", "subtype": "init", "session_id": "s1"},
    {"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
     "content_block": {"type": "thinking", "thinking": "", "signature": ""}}},
    {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
     "delta": {"type": "thinking_delta", "thinking": "The user wants a short answer."}}},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "The user wants a short answer.", "signature": ""}],
     "providerData": {"model": "hy3", "rawUsage": {"credit": 0.03}}}},
    {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
    {"type": "stream_event", "event": {"type": "content_block_start", "index": 1,
     "content_block": {"type": "text", "text": ""}}},
    {"type": "stream_event", "event": {"type": "content_block_delta", "index": 1,
     "delta": {"type": "text_delta", "text": "A DAG is a directed acyclic graph."}}},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "A DAG is a directed acyclic graph."}],
     "providerData": {"model": "hy3", "rawUsage": {"credit": 0}}}},
    {"type": "stream_event", "event": {"type": "content_block_stop", "index": 1}},
    {"type": "result", "subtype": "success", "is_error": False,
     "result": "A DAG is a directed acyclic graph.", "total_cost_usd": 0},
]


def test_codebuddy_streaming_reports_real_thinking_text():
    """Unlike claude, codebuddy's thinking block has real text -- the
    on_event summary must show a preview of it, not a generic placeholder.
    Credit extraction must reuse _extract_codebuddy_credit's existing
    first-assistant-message-wins behavior (documented, not something this
    task changes) -- the fixture's first assistant message carries
    credit=0.03, so that's the value that must come out."""
    from hub.executors import CodeBuddyExecutor

    original = asyncio.create_subprocess_exec
    _patch_stream_subprocess(_CODEBUDDY_STREAM_LINES)
    calls = []
    try:
        task = Task(id="t1", title="x", type=TaskType.WRITING, prompt="p")
        executor = CodeBuddyExecutor(node_path="/fake/node", script_path="/fake/cb.js",
                                     model="hy3", workdir=".")
        result = asyncio.run(executor.run(task, "", on_event=calls.append))
    finally:
        asyncio.create_subprocess_exec = original

    assert calls == ["The user wants a short answer.", "A DAG is a directed acyclic graph."], \
        f"unexpected on_event sequence: {calls}"
    assert result.ok is True
    assert result.output == "A DAG is a directed acyclic graph."
    assert result.cost_credits == 0.03, \
        f"expected first assistant message's credit (0.03), got {result.cost_credits}"
    print("PASS: test_codebuddy_streaming_reports_real_thinking_text")


def test_codebuddy_streaming_empty_thinking_falls_back_to_placeholder():
    """If codebuddy's thinking block DOES come back with empty text (some
    models might not support it), the placeholder must still be shown --
    same safety net as claude's, not a crash or a silent skip."""
    from hub.executors import CodeBuddyExecutor

    original = asyncio.create_subprocess_exec
    _patch_stream_subprocess([
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "", "signature": ""}],
         "providerData": {"rawUsage": {"credit": 0}}}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "ok",
         "total_cost_usd": 0},
    ])
    calls = []
    try:
        task = Task(id="t1", title="x", type=TaskType.WRITING, prompt="p")
        executor = CodeBuddyExecutor(node_path="/fake/node", script_path="/fake/cb.js",
                                     model="glm-5.2", workdir=".")
        result = asyncio.run(executor.run(task, "", on_event=calls.append))
    finally:
        asyncio.create_subprocess_exec = original

    assert calls == ["正在思考…"]
    assert result.ok is True
    print("PASS: test_codebuddy_streaming_empty_thinking_falls_back_to_placeholder")


def test_codebuddy_streaming_error_classified_same_as_blocking():
    """Same is_error/marker-word classification as the blocking path, now
    fed from the tail 'result' line instead of messages[-1]."""
    from hub.executors import CodeBuddyExecutor

    original = asyncio.create_subprocess_exec
    _patch_stream_subprocess([
        {"type": "result", "subtype": "error", "is_error": True,
         "result": "Request rejected: insufficient credit balance", "total_cost_usd": 0},
    ])
    try:
        task = Task(id="t1", title="x", type=TaskType.WRITING, prompt="p")
        executor = CodeBuddyExecutor(node_path="/fake/node", script_path="/fake/cb.js",
                                     model="glm-5.2", workdir=".")
        result = asyncio.run(executor.run(task, ""))
    finally:
        asyncio.create_subprocess_exec = original

    assert result.ok is False
    assert result.error_kind == "unavailable", \
        f"'insufficient credit' must classify unavailable, got {result.error_kind}"
    print("PASS: test_codebuddy_streaming_error_classified_same_as_blocking")


def test_codebuddy_no_result_line_is_task_error():
    from hub.executors import CodeBuddyExecutor

    original = asyncio.create_subprocess_exec
    _patch_stream_subprocess([{"type": "system", "subtype": "init"}],
                             stderr=b"codebuddy crashed")
    try:
        task = Task(id="t1", title="x", type=TaskType.WRITING, prompt="p")
        executor = CodeBuddyExecutor(node_path="/fake/node", script_path="/fake/cb.js",
                                     model="glm-5.2", workdir=".")
        result = asyncio.run(executor.run(task, ""))
    finally:
        asyncio.create_subprocess_exec = original

    assert result.ok is False
    assert result.error_kind == "task_error"
    assert "crashed" in result.error
    print("PASS: test_codebuddy_no_result_line_is_task_error")


class _FakeOpenAIStream:
    """Fake AsyncChatCompletionStream: async-iterates queued content.delta
    events, then get_final_completion() returns a canned completion-shaped
    object (only .model_dump() is used by the code under test, so a tiny
    stand-in object is enough)."""

    class _Event:
        def __init__(self, delta):
            self.type = "content.delta"
            self.delta = delta

    class _FinalCompletion:
        def __init__(self, data):
            self._data = data

        def model_dump(self):
            return self._data

    def __init__(self, deltas: list, final_data: dict):
        self._deltas = deltas
        self._final_data = final_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        self._iter = iter(self._deltas)
        return self

    async def __anext__(self):
        try:
            return self._Event(next(self._iter))
        except StopIteration:
            raise StopAsyncIteration

    async def get_final_completion(self):
        return self._FinalCompletion(self._final_data)


def test_openrouter_streaming_batches_text_and_extracts_cost():
    """Text deltas must be batched (not forwarded one at a time) and the
    final ExecResult must come from get_final_completion()'s usage.cost,
    matching the old httpx implementation's field name.

    Saves/restores OPENROUTER_API_KEY rather than deleting it -- if the
    user has it set as a real persistent env var (per this feature's own
    setup instructions), unconditionally deleting it would wipe it for
    the rest of this terminal session."""
    from hub.executors import OpenRouterExecutor

    original_key = os.environ.get("OPENROUTER_API_KEY")
    os.environ["OPENROUTER_API_KEY"] = "test-key"
    deltas = ["Hello", " world", ", this", " is", " a", " streamed", " reply."]
    final_data = {
        "choices": [{"message": {"content": "".join(deltas)}}],
        "usage": {"cost": 0.0007, "prompt_tokens": 10, "completion_tokens": 8},
    }
    calls = []

    class _FakeCompletions:
        def stream(self, **kwargs):
            return _FakeOpenAIStream(deltas, final_data)

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    original_client_cls = None
    import hub.executors as executors_mod
    original_client_cls = executors_mod.AsyncOpenAI
    executors_mod.AsyncOpenAI = lambda **kwargs: _FakeClient()
    try:
        task = Task(id="t1", title="x", type=TaskType.WRITING, prompt="p")
        executor = OpenRouterExecutor(model="nvidia/nemotron-3-ultra-550b-a55b:free")
        result = asyncio.run(executor.run(task, "", on_event=calls.append))
    finally:
        executors_mod.AsyncOpenAI = original_client_cls
        if original_key is None:
            os.environ.pop("OPENROUTER_API_KEY", None)
        else:
            os.environ["OPENROUTER_API_KEY"] = original_key

    assert result.ok is True
    assert result.output == "Hello world, this is a streamed reply."
    assert result.cost_usd == 0.0007
    # total text is 38 chars, under the 100-char batching threshold, so it
    # must all flush as exactly one on_event call at stream end (the
    # in-loop `len(buffer) >= 100` flush never triggers for this fixture).
    assert calls == ["Hello world, this is a streamed reply."], \
        f"expected the full text flushed as one end-of-stream batch, got {calls}"
    print("PASS: test_openrouter_streaming_batches_text_and_extracts_cost")


def test_openrouter_blocking_path_extracts_output_and_cost():
    """--no-streaming's fallback for OpenRouterExecutor is brand-new code
    (rewritten during the openai SDK migration, unlike claude/codebuddy's
    stream=False paths which reuse pre-existing proven code) -- it must
    be exercised by at least one test, not just assumed correct."""
    from hub.executors import OpenRouterExecutor

    original_key = os.environ.get("OPENROUTER_API_KEY")
    os.environ["OPENROUTER_API_KEY"] = "test-key"

    final_data = {
        "choices": [{"message": {"content": "a blocking reply"}}],
        "usage": {"cost": 0.0012, "prompt_tokens": 5, "completion_tokens": 4},
    }

    class _FakeCompletion:
        def model_dump(self):
            return final_data

    class _FakeCompletions:
        async def create(self, **kwargs):
            return _FakeCompletion()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    import hub.executors as executors_mod
    original_client_cls = executors_mod.AsyncOpenAI
    executors_mod.AsyncOpenAI = lambda **kwargs: _FakeClient()
    try:
        task = Task(id="t1", title="x", type=TaskType.WRITING, prompt="p")
        executor = OpenRouterExecutor(model="x", stream=False)
        result = asyncio.run(executor.run(task, ""))
    finally:
        executors_mod.AsyncOpenAI = original_client_cls
        if original_key is None:
            os.environ.pop("OPENROUTER_API_KEY", None)
        else:
            os.environ["OPENROUTER_API_KEY"] = original_key

    assert result.ok is True
    assert result.output == "a blocking reply"
    assert result.cost_usd == 0.0012
    print("PASS: test_openrouter_blocking_path_extracts_output_and_cost")


def test_openrouter_no_api_key_is_unavailable():
    from hub.executors import OpenRouterExecutor

    original_key = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        task = Task(id="t1", title="x", type=TaskType.WRITING, prompt="p")
        result = asyncio.run(OpenRouterExecutor(model="x").run(task, ""))
    finally:
        if original_key is not None:
            os.environ["OPENROUTER_API_KEY"] = original_key
    assert result.ok is False
    assert result.error_kind == "unavailable"
    print("PASS: test_openrouter_no_api_key_is_unavailable")


def test_openrouter_rate_limit_without_retry_after_is_unavailable():
    """Mirrors the old httpx classification: a 429 with no retry-after
    header is treated as structural (unavailable), not transient.

    openai.APIStatusError's __init__ requires a REAL httpx.Response (it
    reads response.request/response.status_code/response.headers directly
    -- see openai/_exceptions.py) -- a lightweight stand-in object would
    raise AttributeError at construction time, before the code under test
    even runs. Build a real httpx.Response instead of faking one."""
    import httpx
    import openai as openai_mod
    from hub.executors import OpenRouterExecutor

    original_key = os.environ.get("OPENROUTER_API_KEY")
    os.environ["OPENROUTER_API_KEY"] = "test-key"

    fake_response = httpx.Response(
        429, headers={},
        request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"))
    err = openai_mod.RateLimitError("rate limited", response=fake_response, body=None)

    class _FakeCompletions:
        def stream(self, **kwargs):
            raise err

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    import hub.executors as executors_mod
    original_client_cls = executors_mod.AsyncOpenAI
    executors_mod.AsyncOpenAI = lambda **kwargs: _FakeClient()
    try:
        task = Task(id="t1", title="x", type=TaskType.WRITING, prompt="p")
        result = asyncio.run(OpenRouterExecutor(model="x").run(task, ""))
    finally:
        executors_mod.AsyncOpenAI = original_client_cls
        if original_key is None:
            os.environ.pop("OPENROUTER_API_KEY", None)
        else:
            os.environ["OPENROUTER_API_KEY"] = original_key

    assert result.ok is False
    assert result.error_kind == "unavailable", \
        f"429 without retry-after must classify unavailable, got {result.error_kind}"
    print("PASS: test_openrouter_rate_limit_without_retry_after_is_unavailable")


if __name__ == "__main__":
    test_engine_wires_on_event_to_agent_thinking_events()
    test_executor_run_without_on_event_still_works()
    test_claude_streaming_reports_thinking_tool_and_text()
    test_claude_streaming_error_classified_same_as_blocking()
    test_claude_streaming_on_event_none_does_not_crash()
    test_claude_streaming_malformed_line_skipped_not_fatal()
    test_claude_no_result_line_is_task_error()
    test_claude_streaming_oversized_line_is_task_error_not_crash()
    test_codebuddy_streaming_reports_real_thinking_text()
    test_codebuddy_streaming_empty_thinking_falls_back_to_placeholder()
    test_codebuddy_streaming_error_classified_same_as_blocking()
    test_codebuddy_no_result_line_is_task_error()
    test_openrouter_streaming_batches_text_and_extracts_cost()
    test_openrouter_blocking_path_extracts_output_and_cost()
    test_openrouter_no_api_key_is_unavailable()
    test_openrouter_rate_limit_without_retry_after_is_unavailable()
    print("\nAll agent_thinking tests passed.")
