"""DshExecutor: `dsh --profile headless` wiring -- argv shape, --patch model
override, stdout parse, error classification, missing-CLI handling. Subprocess
is faked (monkeypatched asyncio.create_subprocess_exec); zero real dsh calls.

Run: python tests/test_dsh_executor.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub import executors
from hub.executors import DshExecutor
from hub.models import Task, TaskType


class _FakeProc:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr


def _patch_subprocess(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["argv"] = args
        captured["kwargs"] = kwargs
        argv = list(args)
        # read the --patch overlay at spawn time -- this is the only moment it
        # is guaranteed to exist (the executor deletes it in its own finally)
        if "--patch" in argv:
            patch_path = argv[argv.index("--patch") + 1]
            captured["patch_path"] = patch_path
            with open(patch_path, encoding="utf-8") as f:
                captured["patch_content"] = f.read()
        return _FakeProc(stdout, stderr, returncode)

    original = asyncio.create_subprocess_exec
    asyncio.create_subprocess_exec = fake_exec
    return original, captured


def _run(executor, prompt="count to five", context=""):
    task = Task(id="t1", title="x", type=TaskType.CODING, prompt=prompt)
    return asyncio.run(executor.run(task, context))


def test_success_parses_stdout():
    original, _ = _patch_subprocess(stdout=b"one two three four five\n", returncode=0)
    try:
        result = _run(DshExecutor(model="deepseek-v4-flash", workdir=".", dsh_path="fixture/dsh"))
    finally:
        asyncio.create_subprocess_exec = original
    assert result.ok is True
    assert result.output == "one two three four five", f"got {result.output!r}"
    print("PASS: test_success_parses_stdout")


def test_argv_shape_and_patch_content():
    original, captured = _patch_subprocess(stdout=b"ok\n", returncode=0)
    try:
        _run(DshExecutor(model="deepseek-v4-flash", workdir=".", dsh_path="fixture/dsh"), prompt="hello task")
    finally:
        asyncio.create_subprocess_exec = original

    argv = list(captured["argv"])
    assert argv[0].endswith("dsh"), f"expected the dsh binary first, got {argv[0]!r}"
    assert "--profile" in argv and "headless" in argv, f"expected headless profile, got {argv}"
    assert "--patch" in argv, f"expected --patch flag, got {argv}"

    content = captured["patch_content"]
    assert "id: agent-default-model" in content, f"bad patch: {content}"
    assert "provider: deepseek-official" in content, f"bad patch: {content}"
    assert "model: deepseek-v4-flash" in content, f"bad patch: {content}"
    assert argv[-1] == "hello task", f"prompt must be the last positional, got {argv[-1]!r}"

    # the temp --patch overlay must be cleaned up after the run
    assert not os.path.exists(captured["patch_path"]), \
        "patch file must be deleted after the run"

    # workdir must be forwarded so file-touching tasks operate on the project
    assert captured["kwargs"].get("cwd") == ".", f"bad cwd: {captured['kwargs'].get('cwd')}"
    print("PASS: test_argv_shape_and_patch_content")


def test_model_override_writes_v4_pro_patch():
    original, captured = _patch_subprocess(stdout=b"ok\n", returncode=0)
    try:
        _run(DshExecutor(model="deepseek-v4-pro", workdir=".", dsh_path="fixture/dsh"))
    finally:
        asyncio.create_subprocess_exec = original
    assert "model: deepseek-v4-pro" in captured["patch_content"], \
        f"bad patch: {captured['patch_content']}"
    print("PASS: test_model_override_writes_v4_pro_patch")


def test_nonzero_exit_classifies_task_error():
    original, _ = _patch_subprocess(stdout=b"", stderr=b"something blew up", returncode=1)
    try:
        result = _run(DshExecutor(model="deepseek-v4-flash", workdir=".", dsh_path="fixture/dsh"))
    finally:
        asyncio.create_subprocess_exec = original
    assert result.ok is False
    assert result.error_kind == "task_error", f"got {result.error_kind}"
    assert "blew up" in result.error, f"got {result.error!r}"
    print("PASS: test_nonzero_exit_classifies_task_error")


def test_auth_failure_classifies_unavailable():
    original, _ = _patch_subprocess(stdout=b"", stderr=b"invalid api key", returncode=1)
    try:
        result = _run(DshExecutor(model="deepseek-v4-flash", workdir=".", dsh_path="fixture/dsh"))
    finally:
        asyncio.create_subprocess_exec = original
    assert result.ok is False
    assert result.error_kind == "unavailable", f"got {result.error_kind}"
    print("PASS: test_auth_failure_classifies_unavailable")


def test_empty_output_is_task_error():
    original, _ = _patch_subprocess(stdout=b"", stderr=b"", returncode=0)
    try:
        result = _run(DshExecutor(model="deepseek-v4-flash", workdir=".", dsh_path="fixture/dsh"))
    finally:
        asyncio.create_subprocess_exec = original
    assert result.ok is False
    assert result.error_kind == "task_error", f"got {result.error_kind}"
    print("PASS: test_empty_output_is_task_error")


def test_missing_dsh_cli_is_unavailable():
    original_which = executors._which
    original_exec = asyncio.create_subprocess_exec
    # 打桩 _which 而不是 shutil.which：_which 还会去 _EXTRA_BIN_DIRS 里找，
    # 而这台机器上 /usr/local/bin/dsh 可能真的存在 —— 只桩 shutil.which
    # 模拟不出「哪儿都没装」
    executors._which = lambda _name: None
    called = {"n": 0}

    async def fake_exec(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("subprocess must NOT be spawned when dsh is missing")

    asyncio.create_subprocess_exec = fake_exec
    try:
        result = _run(DshExecutor(model="deepseek-v4-flash", workdir="."))
    finally:
        executors._which = original_which
        asyncio.create_subprocess_exec = original_exec
    assert result.ok is False
    assert result.error_kind == "unavailable", f"got {result.error_kind}"
    assert called["n"] == 0, "no subprocess for a missing CLI"
    print("PASS: test_missing_dsh_cli_is_unavailable")


def test_dsh_agents_registered_in_matrix():
    from hub.router import Router

    router = Router("hub/capability_matrix.yaml", workdir=".", mock_all=False)
    names = set(router.dsh_agents())
    assert names == {"dsh-pro", "dsh-flash"}, f"unexpected dsh agent set: {names}"
    print("PASS: test_dsh_agents_registered_in_matrix")


if __name__ == "__main__":
    test_success_parses_stdout()
    test_argv_shape_and_patch_content()
    test_model_override_writes_v4_pro_patch()
    test_nonzero_exit_classifies_task_error()
    test_auth_failure_classifies_unavailable()
    test_empty_output_is_task_error()
    test_missing_dsh_cli_is_unavailable()
    test_dsh_agents_registered_in_matrix()
    print("\nAll dsh executor tests passed.")
