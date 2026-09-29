"""ClaudeCodeExecutor.run_resumed() + session_id extraction, for the deep
collaboration mode MVP. No framework -- plain assert, exit 1 on failure.
Subprocess is faked; zero real LLM calls.

Run: python tests/test_claude_code_resume.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.executors import ClaudeCodeExecutor
from hub.models import Task, TaskType


class _FakeProc:
    def __init__(self, stdout: bytes, returncode: int = 0):
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, b""


def _patch_subprocess(stdout_json: dict, returncode: int = 0):
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["argv"] = args
        return _FakeProc(json.dumps(stdout_json).encode("utf-8"), returncode)

    asyncio.create_subprocess_exec = fake_exec
    return captured


def test_run_extracts_session_id_on_success():
    original = asyncio.create_subprocess_exec
    _patch_subprocess({
        "type": "result", "is_error": False, "result": "hello",
        "total_cost_usd": 0.05, "session_id": "sess-abc-123",
    })
    try:
        task = Task(id="t1", title="x", type=TaskType.CODING, prompt="p")
        result = asyncio.run(
            ClaudeCodeExecutor(model="sonnet", workdir=".", stream=False).run(task, ""))
    finally:
        asyncio.create_subprocess_exec = original

    assert result.ok is True
    assert result.session_id == "sess-abc-123", \
        f"expected session_id extracted, got {result.session_id}"
    print("PASS: test_run_extracts_session_id_on_success")


def test_run_resumed_passes_resume_flag_and_session_id():
    original = asyncio.create_subprocess_exec
    captured = _patch_subprocess({
        "type": "result", "is_error": False, "result": "follow-up reply",
        "total_cost_usd": 0.01, "session_id": "sess-abc-123",
    })
    try:
        executor = ClaudeCodeExecutor(model="sonnet", allowed_tools="Read", workdir=".")
        result = asyncio.run(executor.run_resumed("what about X?", "sess-abc-123"))
    finally:
        asyncio.create_subprocess_exec = original

    assert result.ok is True
    assert result.output == "follow-up reply"
    argv = captured["argv"]
    assert "--resume" in argv, f"expected --resume flag, got {argv}"
    idx = argv.index("--resume")
    assert argv[idx + 1] == "sess-abc-123", f"expected session id after --resume, got {argv}"
    assert "-p" in argv and "what about X?" in argv, f"expected the message as -p arg, got {argv}"
    print("PASS: test_run_resumed_passes_resume_flag_and_session_id")


def test_run_resumed_error_classification_matches_run():
    """run_resumed must reuse the exact same success/failure classification
    as the regular run() path (both go through _exec_json_cmd now)."""
    original = asyncio.create_subprocess_exec
    _patch_subprocess({
        "type": "result", "is_error": True, "api_error_status": 429,
        "duration_api_ms": 0, "result": "API Error: Usage credits required",
        "total_cost_usd": 0.0, "session_id": "sess-abc-123",
    })
    try:
        executor = ClaudeCodeExecutor(model="opus", workdir=".")
        result = asyncio.run(executor.run_resumed("continue", "sess-abc-123"))
    finally:
        asyncio.create_subprocess_exec = original

    assert result.ok is False
    assert result.error_kind == "unavailable", f"expected unavailable, got {result.error_kind}"
    print("PASS: test_run_resumed_error_classification_matches_run")


def test_run_resumed_timeout_is_transient():
    class _NeverProc:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(999)

    async def fake_exec_timeout(*args, **kwargs):
        return _NeverProc()

    original = asyncio.create_subprocess_exec
    asyncio.create_subprocess_exec = fake_exec_timeout
    try:
        executor = ClaudeCodeExecutor(model="sonnet", workdir=".", timeout=0.05)
        result = asyncio.run(executor.run_resumed("continue", "sess-abc-123"))
    finally:
        asyncio.create_subprocess_exec = original

    assert result.ok is False
    assert result.error_kind == "transient"
    print("PASS: test_run_resumed_timeout_is_transient")


if __name__ == "__main__":
    test_run_extracts_session_id_on_success()
    test_run_resumed_passes_resume_flag_and_session_id()
    test_run_resumed_error_classification_matches_run()
    test_run_resumed_timeout_is_transient()
    print("\nAll claude-code-resume tests passed.")
