"""CodexExecutor unit tests.  All subprocesses are fake; the one real CLI
probe for this integration lives outside the test suite under /tmp.

Run: python3 tests/test_codex_executor.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub import executors
from hub.models import Task, TaskType


class _FakeStdin:
    def __init__(self):
        self.data = bytearray()

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass


class _FakeProc:
    _next_pid = 47000

    def __init__(self, lines: list[dict | bytes], stderr: bytes = b"",
                 returncode: int = 0):
        self.stdout = asyncio.StreamReader(limit=10 * 1024 * 1024)
        for item in lines:
            raw = json.dumps(item).encode() if isinstance(item, dict) else item
            self.stdout.feed_data(raw + (b"" if raw.endswith(b"\n") else b"\n"))
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()
        self.stdin = _FakeStdin()
        self.returncode = returncode
        self.terminated = False
        self.pid = self._next_pid
        _FakeProc._next_pid += 1

    async def wait(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def send_signal(self, _signal):
        self.returncode = -15


def _task(prompt: str = "do work") -> Task:
    return Task(id="t1", title="x", type=TaskType.CODING, prompt=prompt)


def _run_with(lines, *, stderr=b"", returncode=0, on_event=None, context=""):
    captured = {}
    original = asyncio.create_subprocess_exec

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs
        proc = _FakeProc(lines, stderr=stderr, returncode=returncode)
        captured["proc"] = proc
        return proc

    asyncio.create_subprocess_exec = fake_exec
    try:
        ex = executors.CodexExecutor(codex_path="/bin/codex", workdir="/workspace")
        result = asyncio.run(ex.run(_task(), context, on_event=on_event))
    finally:
        asyncio.create_subprocess_exec = original
    return result, captured


def test_codex_executor_omits_model_for_auto():
    ex = executors.CodexExecutor(model="", workdir="/workspace",
                                codex_path="/bin/codex")
    argv = ex._argv("do work")
    assert argv[:2] == ["/bin/codex", "exec"]
    assert "--json" in argv and "--ephemeral" in argv
    i = argv.index("--sandbox")
    assert argv[i:i + 2] == ["--sandbox", "workspace-write"]
    assert "--skip-git-repo-check" in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert "--model" not in argv
    assert argv[-1] == "-", "dynamic prompt must be read from stdin"
    assert "do work" not in argv
    print("PASS: test_codex_executor_omits_model_for_auto")


def test_codex_executor_passes_explicit_model_only_when_configured():
    argv = executors.CodexExecutor(
        model="gpt-example", codex_path="/bin/codex")._argv("x")
    assert argv[argv.index("--model") + 1] == "gpt-example"
    print("PASS: test_codex_executor_passes_explicit_model_only_when_configured")


def test_jsonl_progress_output_and_raw_usage():
    progress = []
    lines = [
        {"type": "thread.started", "thread_id": "thread-1"},
        b"not json",
        {"type": "item.completed", "item": {
            "id": "i1", "type": "reasoning", "text": "Inspecting files"}},
        {"type": "item.completed", "item": {
            "id": "i2", "type": "command_execution", "command": "git status"}},
        {"type": "item.completed", "item": {
            "id": "i3", "type": "file_change",
            "changes": [{"path": "hub/a.py", "kind": "update"}]}},
        {"type": "item.completed", "item": {
            "id": "i4", "type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {
            "input_tokens": 12, "cached_input_tokens": 3,
            "cache_write_input_tokens": 0, "output_tokens": 4,
            "reasoning_output_tokens": 1}},
    ]
    result, captured = _run_with(lines, on_event=progress.append,
                                 context="upstream result")
    assert result.ok is True
    assert result.output == "done"
    assert result.cost_usd == 0.0
    assert result.usage == lines[-1]["usage"]
    assert progress[0] == "Inspecting files"
    assert any("git status" in p for p in progress)
    assert any("hub/a.py" in p for p in progress)
    assert captured["kwargs"]["cwd"] == "/workspace"
    assert captured["kwargs"].get("start_new_session") is True
    assert captured["argv"][-1] == "-"
    sent = bytes(captured["proc"].stdin.data).decode()
    assert "<context>\nupstream result\n</context>" in sent
    print("PASS: test_jsonl_progress_output_and_raw_usage")


def test_latest_agent_message_wins():
    result, _ = _run_with([
        {"type": "item.completed", "item": {
            "type": "agent_message", "text": "draft"}},
        {"type": "item.completed", "item": {
            "type": "agent_message", "text": "final"}},
        {"type": "turn.completed", "usage": {}},
    ])
    assert result.ok and result.output == "final"
    assert result.usage is None
    print("PASS: test_latest_agent_message_wins")


def test_failure_classification():
    cases = [
        ("Not logged in. Run codex login", "unavailable"),
        ("model not found for this account", "unavailable"),
        ("429 overloaded; try again", "transient"),
        ("connection reset by peer", "transient"),
        ("permission denied while opening ./private.txt", "task_error"),
        ("tool execution failed", "task_error"),
    ]
    for message, expected in cases:
        result, _ = _run_with(
            [{"type": "turn.failed", "error": {"message": message}}],
            returncode=1)
        assert result.ok is False
        assert result.error_kind == expected, (message, result.error_kind)
        assert message in result.error
    print("PASS: test_failure_classification")


def test_zero_exit_without_agent_message_is_error():
    result, _ = _run_with([
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.completed", "usage": {"input_tokens": 1}},
    ])
    assert result.ok is False
    assert result.error_kind == "task_error"
    assert "without an agent message" in result.error
    print("PASS: test_zero_exit_without_agent_message_is_error")


def test_missing_cli_is_unavailable_without_spawning():
    original_which = executors._which
    executors._which = lambda _name: None
    try:
        ex = executors.CodexExecutor()
        result = asyncio.run(ex.run(_task(), ""))
    finally:
        executors._which = original_which
    assert result.ok is False
    assert result.error_kind == "unavailable"
    assert "not found" in result.error
    print("PASS: test_missing_cli_is_unavailable_without_spawning")


def test_factory_builds_codex_executor_without_a_model():
    ex = executors.build_executor({"kind": "codex", "codex_path": "/bin/codex"},
                                  "/workspace")
    assert isinstance(ex, executors.CodexExecutor)
    assert ex.model == ""
    print("PASS: test_factory_builds_codex_executor_without_a_model")


def test_preflight_forces_read_only_sandbox():
    captured = {}
    original = asyncio.create_subprocess_exec

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = list(argv)
        return _FakeProc([
            {"type": "item.completed", "item": {
                "type": "agent_message", "text": "ok"}},
            {"type": "turn.completed", "usage": {"input_tokens": 1}},
        ])

    asyncio.create_subprocess_exec = fake_exec
    try:
        ex = executors.CodexExecutor(codex_path="/bin/codex")
        result = asyncio.run(ex.run_preflight(_task("reply ok"), ""))
    finally:
        asyncio.create_subprocess_exec = original
    assert result.ok
    i = captured["argv"].index("--sandbox")
    assert captured["argv"][i:i + 2] == ["--sandbox", "read-only"]
    print("PASS: test_preflight_forces_read_only_sandbox")


def test_spawn_strips_api_credentials_from_codex_environment():
    original_exec = asyncio.create_subprocess_exec
    old_values = {key: os.environ.get(key) for key in (
        "CODEX_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY",
        "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY")}
    for key in old_values:
        os.environ[key] = "must-not-leak"
    captured = {}

    async def fake_exec(*argv, **kwargs):
        captured["env"] = kwargs.get("env")
        return _FakeProc([
            {"type": "item.completed", "item": {
                "type": "agent_message", "text": "ok"}},
            {"type": "turn.completed", "usage": {}},
        ])

    asyncio.create_subprocess_exec = fake_exec
    try:
        result = asyncio.run(executors.CodexExecutor(
            codex_path="/bin/codex").run(_task(), ""))
    finally:
        asyncio.create_subprocess_exec = original_exec
        for key, value in old_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    assert result.ok
    assert captured["env"] is not None
    for key in old_values:
        assert key not in captured["env"], f"{key} leaked to Codex"
    assert captured["env"].get("PATH") == os.environ.get("PATH")
    print("PASS: test_spawn_strips_api_credentials_from_codex_environment")


def test_windows_cmd_shim_resolves_native_executable_without_shell():
    original_platform = executors.sys.platform
    original_which = executors._which
    original_glob = executors.glob.glob
    original_isfile = executors.os.path.isfile
    native = "C:/npm/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe"
    try:
        executors.sys.platform = "win32"
        executors._which = lambda _name: "C:/npm/codex.cmd"
        executors.glob.glob = lambda _pattern: [native]
        executors.os.path.isfile = lambda path: path == native
        assert executors._codex_cmd(None) == [native]
    finally:
        executors.sys.platform = original_platform
        executors._which = original_which
        executors.glob.glob = original_glob
        executors.os.path.isfile = original_isfile
    print("PASS: test_windows_cmd_shim_resolves_native_executable_without_shell")


def test_windows_tree_kill_checks_taskkill_exit_status():
    original_name = executors.os.name
    original_run = executors.subprocess.run
    calls = []

    class Proc:
        pid = 4242
        returncode = None
        killed = False

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            return self.returncode

    try:
        executors.os.name = "nt"
        executors.subprocess.run = lambda argv, **kwargs: (
            calls.append(list(argv)) or types.SimpleNamespace(returncode=1))
        proc = Proc()
        cleaned = asyncio.run(executors._terminate_codex_process(proc))
    finally:
        executors.os.name = original_name
        executors.subprocess.run = original_run
    assert cleaned is False, "failed taskkill must not be reported as clean"
    assert proc.killed is True, "leader still gets a best-effort kill"
    assert calls == [["taskkill", "/PID", "4242", "/T", "/F"]]
    print("PASS: test_windows_tree_kill_checks_taskkill_exit_status")


def test_windows_completed_process_uses_job_for_descendant_cleanup():
    original_name = executors.os.name
    original_run = executors.subprocess.run

    class Proc:
        pid = 5252
        returncode = 0

        async def wait(self): return 0

    class Job:
        terminated = False
        closed = False

        def terminate(self):
            self.terminated = True
            return True

        def close(self): self.closed = True

    job = Job()
    try:
        executors.os.name = "nt"
        executors.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("normal Windows cleanup must use its Job Object, not dead PID taskkill"))
        cleaned = asyncio.run(executors._terminate_codex_process(
            Proc(), windows_job=job))
    finally:
        executors.os.name = original_name
        executors.subprocess.run = original_run
    assert cleaned is True and job.terminated and job.closed
    print("PASS: test_windows_completed_process_uses_job_for_descendant_cleanup")


def test_stderr_drainer_keeps_only_bounded_tail():
    async def check():
        reader = asyncio.StreamReader()
        reader.feed_data(b"a" * 100_000 + b"TAIL")
        reader.feed_eof()
        data = await executors._read_bounded_tail(reader, max_bytes=4096)
        assert len(data) == 4096
        assert data.endswith(b"TAIL")
    asyncio.run(check())
    print("PASS: test_stderr_drainer_keeps_only_bounded_tail")


def test_progress_callback_exception_cleans_up_running_process():
    captured = {}
    original = asyncio.create_subprocess_exec

    async def fake_exec(*argv, **kwargs):
        proc = _FakeProc([{"type": "item.completed", "item": {
            "type": "reasoning", "text": "thinking"}}], returncode=None)
        captured["proc"] = proc
        return proc

    def broken_callback(_detail):
        raise RuntimeError("frontend callback broke")

    asyncio.create_subprocess_exec = fake_exec
    try:
        result = asyncio.run(executors.CodexExecutor(
            codex_path="/bin/codex").run(_task(), "", broken_callback))
    finally:
        asyncio.create_subprocess_exec = original
    assert result.ok is False and result.error_kind == "task_error"
    assert "frontend callback broke" in result.error
    assert captured["proc"].terminated is True
    print("PASS: test_progress_callback_exception_cleans_up_running_process")


_HEARTBEAT_CHILD = """
import os, signal, time
path = os.environ["AH_CODEX_LIFECYCLE_BEAT"]
if os.name != "nt":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    with open(path, "w") as f:
        f.write(str(time.time()))
    time.sleep(0.1)
"""

_HANGING_CODEX = """
import os, signal, subprocess, sys, time
if os.name != "nt":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, os.environ["AH_CODEX_LIFECYCLE_CHILD"]])
with open(os.environ["AH_CODEX_LIFECYCLE_PID"], "w") as f:
    f.write(str(child.pid))
while True:
    time.sleep(1)
"""

_EXITING_CODEX = """
import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, os.environ["AH_CODEX_LIFECYCLE_CHILD"]],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
with open(os.environ["AH_CODEX_LIFECYCLE_PID"], "w") as f:
    f.write(str(child.pid))
deadline = time.time() + 3
while not os.path.exists(os.environ["AH_CODEX_LIFECYCLE_BEAT"]) and time.time() < deadline:
    time.sleep(0.02)
"""


class _ScriptCodex(executors.CodexExecutor):
    def __init__(self, script, **kwargs):
        super().__init__(codex_path=sys.executable, **kwargs)
        self.script = script

    def _argv(self, prompt, sandbox="workspace-write"):
        return [sys.executable, self.script]


def _lifecycle_fixture():
    tmp = tempfile.mkdtemp(prefix="ah_codex_lifecycle_")
    child = os.path.join(tmp, "child.py")
    parent = os.path.join(tmp, "parent.py")
    beat = os.path.join(tmp, "beat")
    pid_path = os.path.join(tmp, "child.pid")
    with open(child, "w", encoding="utf-8") as f:
        f.write(_HEARTBEAT_CHILD)
    with open(parent, "w", encoding="utf-8") as f:
        f.write(_HANGING_CODEX)
    keys = {
        "AH_CODEX_LIFECYCLE_CHILD": child,
        "AH_CODEX_LIFECYCLE_BEAT": beat,
        "AH_CODEX_LIFECYCLE_PID": pid_path,
    }
    old = {key: os.environ.get(key) for key in keys}
    os.environ.update(keys)
    return tmp, parent, beat, pid_path, old


def _cleanup_lifecycle(tmp, pid_path, old):
    if os.path.exists(pid_path):
        try:
            raw_pid = open(pid_path, encoding="utf-8").read()
            if raw_pid.isdigit():
                os.kill(int(raw_pid), 9)
        except (ProcessLookupError, ValueError, OSError):
            pass
    for key, value in old.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    shutil.rmtree(tmp, ignore_errors=True)


def _assert_heartbeat_stopped(beat):
    assert os.path.exists(beat), "fixture grandchild never started"
    time.sleep(0.25)
    first = os.stat(beat).st_mtime
    time.sleep(0.35)
    second = os.stat(beat).st_mtime
    assert first == second, f"grandchild still alive: {first} -> {second}"


def test_timeout_kills_codex_grandchild_process():
    if os.name == "nt":
        return
    tmp, parent, beat, pid_path, old = _lifecycle_fixture()
    try:
        ex = _ScriptCodex(parent, workdir=tmp, timeout=0.8)
        result = asyncio.run(ex.run(_task(), ""))
        assert result.ok is False and result.error_kind == "transient"
        _assert_heartbeat_stopped(beat)
    finally:
        _cleanup_lifecycle(tmp, pid_path, old)
    print("PASS: test_timeout_kills_codex_grandchild_process")


def test_async_cancellation_kills_codex_grandchild_process():
    if os.name == "nt":
        return
    tmp, parent, beat, pid_path, old = _lifecycle_fixture()
    try:
        async def cancel_run():
            ex = _ScriptCodex(parent, workdir=tmp, timeout=30)
            running = asyncio.create_task(ex.run(_task(), ""))
            deadline = asyncio.get_running_loop().time() + 3
            while not os.path.exists(beat) and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
            running.cancel()
            try:
                await running
            except asyncio.CancelledError:
                return
            raise AssertionError("executor must propagate CancelledError")
        asyncio.run(cancel_run())
        _assert_heartbeat_stopped(beat)
    finally:
        _cleanup_lifecycle(tmp, pid_path, old)
    print("PASS: test_async_cancellation_kills_codex_grandchild_process")


def test_timeout_kills_group_after_codex_leader_already_exited():
    """A tool child can outlive its Codex group leader; normal-result cleanup
    must still signal the saved PGID instead of returning early."""
    if os.name == "nt":
        return
    tmp, _parent, beat, pid_path, old = _lifecycle_fixture()
    exiting = os.path.join(tmp, "exiting.py")
    with open(exiting, "w", encoding="utf-8") as f:
        f.write(_EXITING_CODEX)
    try:
        ex = _ScriptCodex(exiting, workdir=tmp, timeout=0.8)
        result = asyncio.run(ex.run(_task(), ""))
        assert result.ok is False and result.error_kind == "task_error"
        _assert_heartbeat_stopped(beat)
    finally:
        _cleanup_lifecycle(tmp, pid_path, old)
    print("PASS: test_timeout_kills_group_after_codex_leader_already_exited")


if __name__ == "__main__":
    test_codex_executor_omits_model_for_auto()
    test_codex_executor_passes_explicit_model_only_when_configured()
    test_jsonl_progress_output_and_raw_usage()
    test_latest_agent_message_wins()
    test_failure_classification()
    test_zero_exit_without_agent_message_is_error()
    test_missing_cli_is_unavailable_without_spawning()
    test_factory_builds_codex_executor_without_a_model()
    test_preflight_forces_read_only_sandbox()
    test_spawn_strips_api_credentials_from_codex_environment()
    test_windows_cmd_shim_resolves_native_executable_without_shell()
    test_windows_tree_kill_checks_taskkill_exit_status()
    test_windows_completed_process_uses_job_for_descendant_cleanup()
    test_stderr_drainer_keeps_only_bounded_tail()
    test_progress_callback_exception_cleans_up_running_process()
    test_timeout_kills_codex_grandchild_process()
    test_async_cancellation_kills_codex_grandchild_process()
    test_timeout_kills_group_after_codex_leader_already_exited()
    print("\nAll Codex executor tests passed.")
