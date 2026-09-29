"""Blackboard: shared memory layer (data plane).

Design (see README):
- DAG stays the control plane (who runs when); the blackboard is the
  data plane (what everyone knows).
- Append-only JSONL on disk -> crash recovery for free (replay on load).
- In-memory view is last-write-wins per key.
- Write permission is namespace-scoped to prevent context poisoning:
    * an agent working on task X may write  tasks/X/*  and  notes/*
    * the engine/system ("system", "planner", "manager") may write anything
- Every entry carries author/task_id/kind/confidence so consumers know
  the provenance of what they read.

Namespace conventions:
    run/workdir              absolute path agents write files into, so
                             serve.py can resolve tasks/{id}/files without
                             assuming its own cwd is the workdir
    goal/statement           original goal text
    goal/constraints         constraints clarified with the user
    tasks/{id}/spec          task specification
    tasks/{id}/output        full deliverable text
    tasks/{id}/summary       compressed handoff summary
    tasks/{id}/files         real files this task left in the workdir,
                             [{"path": relative-to-workdir, "size": bytes}]
                             (see hub/artifacts.py). Doubles as serve.py's
                             GET /file whitelist -- a path not listed here
                             is not servable, so no traversal is possible
    tasks/{id}/agent         which agent name completed this task (set by Engine)
    tasks/{id}/session_id    claude_code session id -- POST /task/{id}/message
                             resumes it via `claude -p --resume` (deep
                             collaboration mode MVP, claude_code only)
    tasks/{id}/session_usage_ratio  rough context-window fill estimate, used
                             to decide when to compact + reopen with a fresh
                             session_id (see hub/conversation.py)
    tasks/{id}/session_digest  handoff summary produced right before a
                             compaction reopens the session with a fresh id
    tasks/{id}/status        first written "done" the moment a task is truly
                             DAG-terminal -- serve.py's POST /task/{id}/message
                             reads this to decide whether to answer
                             synchronously (done: call hub/conversation.py
                             directly) or just queue (not yet done: the
                             Engine coroutine still owns the session and may
                             or may not ever read the inbox below -- see
                             tasks/{id}/inbox/{n}). Can be overwritten a
                             second time, "done" -> "stale", if a DIFFERENT
                             done task this one depends on gets a deep-
                             collaboration follow-up that changes its output
                             (see hub/conversation.py's mark_stale_dependents)
    tasks/{id}/inbox/{n}     follow-up messages queued by serve.py for a
                             task that isn't done yet, 0-indexed. A running
                             claude_code task checks this once right after
                             its turn finishes; a nonempty inbox reactively
                             opens an AWAITING_INPUT resume loop instead of
                             finalizing immediately (see hub/engine.py)
    tasks/{id}/inbox_finish  set true by serve.py's finish:true field --
                             tells an AWAITING_INPUT loop to stop waiting
                             for more messages and finalize now
    notes/{seq}              opportunistic findings from any agent
    review/{id}/verdict      reviewer conclusions
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Optional

SYSTEM_AUTHORS = {"system", "planner", "manager", "engine"}


class PermissionError_(Exception):
    pass


class Blackboard:
    def __init__(self, path: str, on_write: Optional[Callable[[dict], None]] = None):
        self.path = path
        self.on_write = on_write          # e.g. lambda e: bus.emit("blackboard_write", **e)
        self._kv: dict[str, dict] = {}    # key -> latest entry
        self._note_seq = 0
        self._replayed_bytes = 0          # how much of path this object has ingested so far
        if os.path.exists(path):
            self._replay()
            self._replayed_bytes = os.path.getsize(path)

    # ---------- persistence ----------
    def _replay(self) -> None:
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                self._apply_line(line)

    def _apply_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            return
        self._kv[e["key"]] = e
        if e["key"].startswith("notes/"):
            try:
                self._note_seq = max(self._note_seq, int(e["key"].split("/")[1]))
            except (IndexError, ValueError):
                pass

    def refresh(self) -> None:
        """Incrementally pick up entries appended by ANOTHER Blackboard
        instance (same process or a different one) since this object was
        constructed or last refreshed -- reads only the bytes past
        self._replayed_bytes instead of re-parsing the whole file from
        scratch. Cheap to call even when nothing changed.

        Exists for hot polling loops where a fresh Blackboard(path) every
        tick would be O(file size) per tick regardless of whether anything
        actually changed -- see hub/engine.py's _await_input_loop, which
        can poll for up to budget.awaiting_input_timeout_sec (default
        600s, ~200 ticks at the default interval) waiting on a single
        task's inbox. serve.py's POST /task/{id}/message still constructs
        a fresh Blackboard per request rather than sharing+refreshing one:
        that path is request-frequency, not tick-frequency, and sharing
        one Blackboard object across ThreadingHTTPServer's request threads
        would need its own lock -- a separate, riskier change not bundled
        into this one (see hub/conversation.py's continue_task_conversation
        docstring).

        Only consumes COMPLETE lines: a concurrent writer's in-flight last
        line (no trailing "\\n" yet) is left unconsumed so it gets replayed
        -- not silently dropped -- the next time it's actually complete.
        """
        if not os.path.exists(self.path):
            return
        with open(self.path, "rb") as f:
            f.seek(self._replayed_bytes)
            chunk = f.read()
        last_nl = chunk.rfind(b"\n")
        if last_nl == -1:
            return
        complete, self._replayed_bytes = chunk[:last_nl + 1], self._replayed_bytes + last_nl + 1
        for line in complete.decode("utf-8").split("\n"):
            self._apply_line(line)

    def _persist(self, entry: dict) -> None:
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line)
        self._replayed_bytes += len(line.encode("utf-8"))

    # ---------- permissions ----------
    @staticmethod
    def _can_write(author: str, key: str, task_id: Optional[str]) -> bool:
        if author in SYSTEM_AUTHORS:
            return True
        if key.startswith("notes/"):
            return True
        # 门的回复：语义上就是用户的意志，写成 author="system" 是撒谎
        # （黑板条目带 author 就是为了让消费者知道这句话是谁说的）。
        # 放宽而非收紧，且只开这一个前缀 -- agent 拿不到 author="user"，
        # 自批自放这条路是堵死的。见 spec §3.2。
        if author == "user" and key.startswith("run/gate_reply/"):
            return True
        if task_id and key.startswith(f"tasks/{task_id}/"):
            return True
        if task_id and key == f"review/{task_id}/verdict":
            return True
        return False

    # ---------- api ----------
    def put(self, key: str, value: Any, author: str, *, task_id: Optional[str] = None,
            kind: str = "fact", confidence: float = 1.0) -> dict:
        if not self._can_write(author, key, task_id):
            raise PermissionError_(
                f"agent '{author}' (task={task_id}) may not write '{key}'")
        entry = {"key": key, "value": value, "author": author,
                 "task_id": task_id, "kind": kind,
                 "confidence": confidence, "ts": round(time.time(), 3)}
        self._kv[key] = entry
        self._persist(entry)
        if self.on_write:
            self.on_write({"key": key, "author": author, "task_id": task_id,
                           "kind": kind,
                           "preview": str(value)[:120]})
        return entry

    def add_note(self, text: str, author: str, *, task_id: Optional[str] = None,
                 confidence: float = 0.7) -> dict:
        """Opportunistic finding — the only free-for-all namespace."""
        self._note_seq += 1
        return self.put(f"notes/{self._note_seq}", text, author,
                        task_id=task_id, kind="note", confidence=confidence)

    def get(self, key: str, default: Any = None) -> Any:
        e = self._kv.get(key)
        return e["value"] if e else default

    def entry(self, key: str) -> Optional[dict]:
        return self._kv.get(key)

    def query(self, prefix: str) -> list[dict]:
        """All latest entries under a namespace prefix, oldest first."""
        return sorted((e for k, e in self._kv.items() if k.startswith(prefix)),
                      key=lambda e: e["ts"])

    def keys(self) -> list[str]:
        return sorted(self._kv.keys())
