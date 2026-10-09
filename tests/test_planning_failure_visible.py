"""Bug B11 (docs/decisions/gaps-2026-09-25.md): when planning fails -- the
most common failure of a goal-driven run -- the reason must reach the event
stream, not only a traceback on serve.py's console.

Before the fix main.py died on the uncaught RuntimeError from make_plan with
no event at all; serve.py's watchdog could only add "engine exited with code
1 before run_finished", and the dashboard's pre-plan crash text blamed a
moved repository.

`--mock` with `--goal` is a real, zero-cost planning failure: the mock
planner answers with text that is not a JSON plan.

Run: python tests/test_planning_failure_visible.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_planning_failure_reason_reaches_the_event_stream():
    tmp = tempfile.mkdtemp(prefix="ah_b11_")
    events = os.path.join(tmp, "events.jsonl")
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "main.py"), "--goal=做一个落地页",
         "--mock", f"--events={events}", f"--board={os.path.join(tmp, 'board.jsonl')}",
         f"--workdir={os.path.join(tmp, 'wd')}", "--no-preflight"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120)
    assert proc.returncode != 0, "a run that planned nothing must not exit 0"
    assert os.path.exists(events), f"no event stream written; stderr:\n{proc.stderr[-800:]}"
    with open(events, encoding="utf-8") as f:
        crashed = [json.loads(l) for l in f if l.strip()]
    crashed = [e for e in crashed if e["type"] == "run_crashed"]
    assert len(crashed) == 1, crashed
    assert "planning failed" in crashed[0]["error"], crashed[0]
    print("PASS: test_planning_failure_reason_reaches_the_event_stream")


if __name__ == "__main__":
    test_planning_failure_reason_reaches_the_event_stream()
