"""B10 (2026-09-25): on Windows npm installs CLIs as .cmd shims, and a
.cmd/.bat only runs through cmd.exe, which re-parses every argument. dsh
takes its prompt as an argument, and the prompt carries the user's goal and
upstream agents' output (possibly text from a web page an agent read): a `"`
plus `&` in it runs arbitrary commands (the BatBadBut class). _dsh_cmd used
to hand npm's shim to `cmd /c`.

Now npm's shim is resolved to `node <script>` (the shim's own target; the
format was checked against the npm shims installed on this machine), and a
.cmd/.bat of any other shape is refused rather than run through cmd.exe.
claude needs no such rule any more: its prompt goes through stdin (B2,
tests/test_long_prompt.py), so nothing dynamic reaches its argv.

Run: python tests/test_no_cmd_exe_for_prompts.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub import executors
from hub.executors import DshExecutor
from hub.models import Task, TaskType

# npm's cmd-shim output, byte for byte the shape of %APPDATA%\npm\*.cmd
NPM_SHIM = r'''@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0

IF EXIST "%dp0%\node.exe" (
  SET "_prog=%dp0%\node.exe"
) ELSE (
  SET "_prog=node"
  SET PATHEXT=%PATHEXT:;.JS;=;%
)

endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\node_modules\@deepseek-ai\dsh\dist\cli.js" %*
'''


def _touch(path: str, text: str = "") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def _npm_install(with_node: bool, target: str = r"dist\cli.js") -> tuple[str, str]:
    base = tempfile.mkdtemp()
    shim = _touch(os.path.join(base, "dsh.cmd"), NPM_SHIM.replace(r"dist\cli.js", target))
    script = _touch(os.path.join(base, "node_modules", "@deepseek-ai", "dsh", *target.split("\\")))
    if with_node:
        _touch(os.path.join(base, "node.exe"))
    return shim, script


def test_npm_shim_runs_node_directly():
    shim, script = _npm_install(with_node=True)
    assert executors._dsh_cmd(shim) == [os.path.join(os.path.dirname(shim), "node.exe"), script], \
        executors._dsh_cmd(shim)

    # npm bin targets may have no extension (a node script with a shebang,
    # e.g. opencode's `bin\opencode` on this machine)
    shim, script = _npm_install(with_node=True, target=r"bin\dsh")
    assert executors._dsh_cmd(shim) == [os.path.join(os.path.dirname(shim), "node.exe"), script], \
        executors._dsh_cmd(shim)

    shim, script = _npm_install(with_node=False)      # node comes from PATH then
    original = executors._which
    executors._which = lambda name: r"C:\nodejs\node.exe" if name == "node" else None
    try:
        assert executors._dsh_cmd(shim) == [r"C:\nodejs\node.exe", script], executors._dsh_cmd(shim)
    finally:
        executors._which = original
    print("PASS: test_npm_shim_runs_node_directly")


def test_other_batch_files_are_refused_not_run_through_cmd():
    base = tempfile.mkdtemp()
    hand_written = _touch(os.path.join(base, "dsh.cmd"), "@echo off\r\nnode C:\\x\\cli.js %*\r\n")
    bat = _touch(os.path.join(base, "dsh.bat"), NPM_SHIM.replace("dsh\\dist", "nope"))
    # npm's shim for a non-node target (a `#!/bin/sh` script) runs it with sh
    sh_shim, _ = _npm_install(with_node=True)
    with open(sh_shim, "w", encoding="utf-8") as f:
        f.write(NPM_SHIM.replace("node.exe", "sh.exe").replace('"_prog=node"', '"_prog=sh"'))
    for path in (hand_written, bat, sh_shim):
        assert executors._dsh_cmd(path) == [], (path, executors._dsh_cmd(path))
    print("PASS: test_other_batch_files_are_refused_not_run_through_cmd")


def test_dsh_executor_never_spawns_cmd_exe():
    shim, script = _npm_install(with_node=True)
    spawned = []

    async def spy(*argv, **kwargs):
        spawned.append(argv)
        raise FileNotFoundError(2, "stop here")

    original = asyncio.create_subprocess_exec
    asyncio.create_subprocess_exec = spy
    try:
        prompt = 'x" & calc & "'
        task = Task(id="t1", title="x", type=TaskType.ANALYSIS, prompt=prompt)
        asyncio.run(DshExecutor(dsh_path=shim).run(task, ""))
    finally:
        asyncio.create_subprocess_exec = original
    assert spawned, "executor never tried to spawn"
    argv = spawned[0]
    assert os.path.basename(argv[0]).lower() == "node.exe" and argv[1] == script, argv
    assert not any(os.path.basename(a).lower() in ("cmd", "cmd.exe") for a in argv), argv
    print("PASS: test_dsh_executor_never_spawns_cmd_exe")


if __name__ == "__main__":
    if sys.platform != "win32":
        print("SKIP: .cmd/.bat shims only exist on Windows")
        sys.exit(0)
    test_npm_shim_runs_node_directly()
    test_other_batch_files_are_refused_not_run_through_cmd()
    test_dsh_executor_never_spawns_cmd_exe()
    print("\nAll no-cmd-exe tests passed.")
