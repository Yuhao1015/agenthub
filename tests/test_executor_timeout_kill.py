"""Bug B3 (docs/decisions/gaps-2026-09-25.md): when a blocking executor call
times out, the child process must be terminated, not just abandoned.

asyncio.wait_for(proc.communicate(), timeout) only stops WAITING. Before the
fix the CLI kept running (and kept editing the workdir) while the engine,
seeing a "transient" timeout, started the same agent again -- two or three
copies of one agent working on one task at once.

Uses a stand-in "CLI" that just sleeps; zero real claude/codebuddy/dsh calls.

Run: python tests/test_executor_timeout_kill.py
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub import executors
from hub.models import Task, TaskType

SLEEPER = os.path.join(tempfile.mkdtemp(prefix="ah_b3_"), "sleeper.py")
with open(SLEEPER, "w", encoding="utf-8") as _f:
    _f.write("import time\ntime.sleep(30)\n")   # ignores any argv it is given


def _force_kill(pid: int) -> None:
    """Test cleanup only: never leave the sleeper behind, even when RED."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _run_and_capture(coro_factory):
    """Run one executor call; return (result, the spawned Process)."""
    spawned = []
    real_exec = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        proc = await real_exec(*args, **kwargs)
        spawned.append(proc)
        return proc

    asyncio.create_subprocess_exec = spy
    try:
        result = asyncio.run(coro_factory())
    finally:
        asyncio.create_subprocess_exec = real_exec
    assert len(spawned) == 1, f"expected exactly one child, got {len(spawned)}"
    return result, spawned[0]


def _assert_timed_out_and_dead(name, result, proc):
    try:
        assert not result.ok and result.error_kind == "transient", \
            f"{name}: expected a transient timeout, got {result}"
        assert proc.returncode is not None, \
            f"{name}: child pid {proc.pid} still running after the timeout"
    finally:
        _force_kill(proc.pid)
    print(f"PASS: {name}")


def _task():
    return Task(id="t", title="t", type=TaskType.CODING, prompt="hi")


def test_claude_blocking_timeout_kills_child():
    original = executors._claude_cmd
    executors._claude_cmd = lambda: [sys.executable, SLEEPER]
    try:
        ex = executors.ClaudeCodeExecutor(timeout=1, stream=False)
        result, proc = _run_and_capture(lambda: ex.run(_task(), ""))
    finally:
        executors._claude_cmd = original
    _assert_timed_out_and_dead("test_claude_blocking_timeout_kills_child", result, proc)


def test_claude_resumed_timeout_kills_child():
    """run_resumed is the deep-collaboration follow-up path; always blocking."""
    original = executors._claude_cmd
    executors._claude_cmd = lambda: [sys.executable, SLEEPER]
    try:
        ex = executors.ClaudeCodeExecutor(timeout=1, stream=True)
        result, proc = _run_and_capture(lambda: ex.run_resumed("more", "sid-1"))
    finally:
        executors._claude_cmd = original
    _assert_timed_out_and_dead("test_claude_resumed_timeout_kills_child", result, proc)


def test_codebuddy_blocking_timeout_kills_child():
    ex = executors.CodeBuddyExecutor(node_path=sys.executable, script_path=SLEEPER,
                                     model="m", timeout=1, stream=False)
    result, proc = _run_and_capture(lambda: ex.run(_task(), ""))
    _assert_timed_out_and_dead("test_codebuddy_blocking_timeout_kills_child", result, proc)


def test_dsh_timeout_kills_child():
    original = executors._dsh_cmd
    executors._dsh_cmd = lambda dsh_path: [sys.executable, SLEEPER]
    try:
        ex = executors.DshExecutor(timeout=1)
        result, proc = _run_and_capture(lambda: ex.run(_task(), ""))
    finally:
        executors._dsh_cmd = original
    _assert_timed_out_and_dead("test_dsh_timeout_kills_child", result, proc)


if __name__ == "__main__":
    test_claude_blocking_timeout_kills_child()
    test_claude_resumed_timeout_kills_child()
    test_codebuddy_blocking_timeout_kills_child()
    test_dsh_timeout_kills_child()
