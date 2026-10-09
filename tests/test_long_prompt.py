"""B2 (2026-09-25): Windows caps a command line at 32,767 chars. Past it,
CreateProcess fails with WinError 206, which Python raises as
FileNotFoundError -- and every executor that puts the prompt in argv read
that as "CLI not installed" + error_kind=unavailable, circuit-breaking the
agent for the whole run. A long pasted goal, a long manager chat, or a task
that stitches in upstream outputs (up to 4000 chars each) all get there.

- claude: the prompt now goes through stdin. Verified 2026-09-25 against
  claude 2.1.282 with a local fake API (no quota): json and stream-json
  modes both read a 40K-char stdin prompt; empty stdin gives "Input must be
  provided either through stdin or as a prompt argument". The test below
  runs a real child process standing in for claude.
- codebuddy / dsh: their stdin behaviour can't be verified on this machine,
  so they keep argv, but a too-long command line must read as THIS prompt
  failing (task_error -> next agent in the chain), not the CLI missing.

Run: python tests/test_long_prompt.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub import executors
from hub.executors import ClaudeCodeExecutor, CodeBuddyExecutor, DshExecutor
from hub.models import Task, TaskType

MARKER = "LONG_PROMPT_MARKER_B2"
LONG = "x" * 40000 + MARKER          # past the 32,767-char Windows cap

# Stands in for `claude -p`: reports what reached it through stdin and
# whether the prompt leaked into argv, as one result line that both the
# json and the stream-json parsers accept.
FAKE_CLAUDE = r'''
import json, sys
data = sys.stdin.buffer.read().decode("utf-8")
m = "LONG_PROMPT_MARKER_B2"
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "stdin=%d marker=%s argv_has_marker=%s" % (
                      len(data), m in data, any(m in a for a in sys.argv)),
                  "total_cost_usd": 0, "session_id": "s-fake"}))
'''


def _write(name: str, text: str) -> str:
    path = os.path.join(tempfile.mkdtemp(), name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def _task(prompt: str) -> Task:
    return Task(id="t1", title="x", type=TaskType.ANALYSIS, prompt=prompt)


def test_claude_long_prompt_goes_through_stdin():
    fake = [sys.executable, _write("fake_claude.py", FAKE_CLAUDE)]
    expected = f"stdin={len(LONG)} marker=True argv_has_marker=False"
    original = executors._claude_cmd
    executors._claude_cmd = lambda: list(fake)
    try:
        for stream in (False, True):
            ex = ClaudeCodeExecutor(model="sonnet", workdir=".", stream=stream)
            r = asyncio.run(ex.run(_task(LONG), ""))
            assert r.ok, f"stream={stream}: {r.error_kind} {r.error!r}"
            assert r.output == expected, f"stream={stream}: {r.output!r}"
        r = asyncio.run(ClaudeCodeExecutor(model="sonnet", workdir=".")
                        .run_resumed(LONG, "sess-1"))
        assert r.ok, f"run_resumed: {r.error_kind} {r.error!r}"
        assert r.output == expected, f"run_resumed: {r.output!r}"
    finally:
        executors._claude_cmd = original
    print("PASS: test_claude_long_prompt_goes_through_stdin")


def test_too_long_command_line_is_this_prompts_failure_not_missing_cli():
    if sys.platform != "win32":
        print("SKIP: test_too_long_command_line_is_this_prompts_failure_not_missing_cli"
              " (WinError 206 is Windows-only)")
        return
    script = _write("never_runs.py", "")
    cases = [
        ("codebuddy blocking", CodeBuddyExecutor(sys.executable, script, "m", stream=False)),
        ("codebuddy streaming", CodeBuddyExecutor(sys.executable, script, "m", stream=True)),
        ("dsh", DshExecutor(dsh_path=sys.executable)),
    ]
    for name, ex in cases:
        r = asyncio.run(ex.run(_task(LONG), ""))
        assert not r.ok, name
        assert r.error_kind == "task_error", f"{name}: {r.error_kind} {r.error!r}"
        assert "too long" in r.error, f"{name}: {r.error!r}"
    print("PASS: test_too_long_command_line_is_this_prompts_failure_not_missing_cli")


def test_missing_cli_still_circuit_breaks():
    """Control: the WinError 206 carve-out must not swallow a CLI that is
    really not there."""
    missing = os.path.join(tempfile.mkdtemp(), "no-such-cli.exe")
    cases = [
        ("codebuddy blocking", CodeBuddyExecutor(missing, missing, "m", stream=False)),
        ("codebuddy streaming", CodeBuddyExecutor(missing, missing, "m", stream=True)),
        ("dsh", DshExecutor(dsh_path=missing)),
    ]
    for name, ex in cases:
        r = asyncio.run(ex.run(_task("short"), ""))
        assert r.error_kind == "unavailable", f"{name}: {r.error_kind} {r.error!r}"
    print("PASS: test_missing_cli_still_circuit_breaks")


if __name__ == "__main__":
    test_claude_long_prompt_goes_through_stdin()
    test_too_long_command_line_is_this_prompts_failure_not_missing_cli()
    test_missing_cli_still_circuit_breaks()
    print("\nAll long-prompt tests passed.")
