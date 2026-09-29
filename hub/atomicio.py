"""Atomic file replacement.

Split out of serve.py on 2026-09-05 because it has ten non-session callers
there (run index, credentials, config, schedules, merged matrix, preflight,
last project) and one in hub/sessions.py.  Filing it under sessions.py would
have made serve.py's credential writing depend on session storage; keeping a
second copy would have been worse still.

Write-to-temp-then-rename means a reader never observes a half-written file,
and a crash mid-write leaves the previous version intact rather than a
truncated one.
"""
from __future__ import annotations

import os


def atomic_write(path: str, text: str, mode: int = 0o600) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + f".tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
