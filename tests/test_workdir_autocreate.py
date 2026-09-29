"""main.py must create --workdir if it doesn't exist yet. Without this,
asyncio.create_subprocess_exec(..., cwd=<missing dir>) raises
FileNotFoundError, which claude_code/codebuddy executors misclassify as
"CLI not found" (error_kind=unavailable) -- silently circuit-breaking every
agent on a plain missing directory, nothing to do with the CLI at all.

No framework -- plain assert, exit 1 on failure.
Run: python tests/test_workdir_autocreate.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main


def test_amain_creates_missing_workdir_and_completes_with_mock():
    tmp = tempfile.mkdtemp()
    workdir = os.path.join(tmp, "does", "not", "exist", "yet")
    events = os.path.join(tmp, "events.jsonl")
    board = os.path.join(tmp, "blackboard.jsonl")
    assert not os.path.isdir(workdir), "precondition: workdir must not exist yet"

    argv = ["main.py", "--plan", "example_plan.json", "--mock",
            "--workdir", workdir, "--events", events, "--board", board]
    old_argv = sys.argv
    sys.argv = argv
    try:
        rc = asyncio.run(main.amain())
        # MockExecutor never touches cwd, so it happily "succeeds" even when
        # the directory is missing -- rc==0 alone would not catch a
        # regression here. The real assertion is that amain() actually
        # created the directory (what claude_code/codebuddy's cwd= needs).
        created = os.path.isdir(workdir)
    finally:
        sys.argv = old_argv
        shutil.rmtree(tmp, ignore_errors=True)

    assert rc == 0, f"expected exit 0 (all tasks done), got {rc}"
    assert created, "amain() must create --workdir if it doesn't exist yet"
    print("PASS: test_amain_creates_missing_workdir_and_completes_with_mock")


if __name__ == "__main__":
    test_amain_creates_missing_workdir_and_completes_with_mock()
    print("\nAll workdir-autocreate tests passed.")
