"""Artifact tracking: which real files did a task actually produce?

The Executor contract only carries TEXT back from an agent
(ExecResult.output), but a coding task's real deliverable is usually a
file on disk (demo_site/index.html, report.md, ...). Nothing in the
system knew those files existed, so the frontend had nothing to point at
— hence this module.

Mechanism: snapshot the workdir before a task runs, snapshot again when
it finishes, and treat "appeared or changed" as "this task produced it".

Why a directory snapshot rather than reading the agent's own tool calls:
claude_code/codebuddy DO stream tool_use blocks carrying file_path (see
hub/executors.py's _format_claude_thinking_detail), but that signal
exists for only two of the five executor kinds, disappears entirely under
--no-streaming, and misses any file an agent creates indirectly (a bash
one-liner, or a script it wrote and then ran). A snapshot diff is
executor-agnostic, needs no change to the Executor contract, and records
what actually landed on disk rather than what the agent said it did.

Known limitations, deliberate for now:
  - Parallel tasks share one workdir, so a file written by task B while
    task A is running is attributed to BOTH. Over-attribution, never
    loss. Exact attribution would need per-task sandboxes (worktrees).
  - Follow-ups on an already-DONE task (hub/conversation.py, driven by
    POST /task/{id}/message) run outside the Engine and record nothing,
    so files they write don't show up until the task is rerun.
"""
from __future__ import annotations

import os
from typing import Iterable, Optional

MAX_SCAN_FILES = 5000     # bail out entirely past this — a workdir that big
                          # is not something we should be diffing per task
MAX_FILES_PER_TASK = 50   # cap what one task may claim, so a stray `npm i`
                          # can't dump thousands of paths into an event
SKIP_DIRS = {"node_modules", "__pycache__", "site-packages"}

Snapshot = dict[str, tuple[int, int]]   # relpath -> (mtime_ns, size)


def snapshot(workdir: str, exclude: Iterable[str] = ()) -> Optional[Snapshot]:
    """Map every visible file under `workdir` to (mtime_ns, size).

    Dot-prefixed files and directories are skipped (.git, .venv, .DS_Store
    — none of them are ever a deliverable), as are SKIP_DIRS. `exclude`
    takes paths to ignore outright; the Engine passes events.jsonl and
    blackboard.jsonl, which change during literally every task and would
    otherwise be claimed as each one's deliverable.

    Returns None if the tree exceeds MAX_SCAN_FILES — the caller treats
    that as "artifact tracking off for this run" rather than diffing a
    truncated snapshot and reporting nonsense.
    """
    excluded = {os.path.realpath(p) for p in exclude}
    out: Snapshot = {}
    for root, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if name.startswith("."):
                continue
            full = os.path.join(root, name)
            if os.path.realpath(full) in excluded:
                continue
            try:
                st = os.stat(full)
            except OSError:
                continue        # vanished mid-walk, or unreadable — not ours
            rel = os.path.relpath(full, workdir).replace(os.sep, "/")
            out[rel] = (st.st_mtime_ns, st.st_size)
            if len(out) > MAX_SCAN_FILES:
                return None
    return out


def changed_files(before: Optional[Snapshot],
                  after: Optional[Snapshot]) -> list[dict]:
    """Files that appeared or changed between two snapshots, oldest path
    order, capped. Deletions are ignored — there's nothing to preview.
    A None snapshot on either side yields [] (tracking was off)."""
    if before is None or after is None:
        return []
    changed = [{"path": path, "size": meta[1]}
               for path, meta in after.items() if before.get(path) != meta]
    changed.sort(key=lambda f: f["path"])
    return changed[:MAX_FILES_PER_TASK]
