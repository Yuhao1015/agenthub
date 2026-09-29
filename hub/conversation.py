"""Deep collaboration mode (MVP): continue a chat with the specific
claude_code agent that completed a task, using `claude -p --resume`
instead of the DAG's one-shot dispatch.

Scope (MVP): only claude_code-kind agents, only tasks already DONE when
the first follow-up message arrives. codebuddy/ACP session reuse is
explicitly out of scope.

A successful follow-up overwrites tasks/{id}/output (see
continue_task_conversation) -- which can make a downstream task's already-
consumed output stale. mark_stale_dependents() below detects that (direct
dependents only, DONE -> STALE, no auto-rerun) for serve.py to call and
surface as a task_stale event; see CLAUDE.md's deep-collaboration section
for the "why not auto-rerun" rationale.

This module is ONLY reached from serve.py's synchronous branch, i.e. once
tasks/{id}/status == "done" -- at that point the Engine coroutine that
owned the task has already returned and nobody else will call
`claude -p --resume` on this session_id, so a direct call here is safe.
A task that isn't done yet never reaches this module: its follow-up
messages are queued onto the blackboard inbox instead and picked up
reactively by the still-alive Engine process itself (see hub/engine.py's
`_await_input_loop`) -- two processes resuming the same session
concurrently would race in a way no in-process lock here could prevent.
"""
from __future__ import annotations

import dataclasses
from typing import Optional

from .blackboard import Blackboard
from .executors import ClaudeCodeExecutor, ExecResult
from .models import Task, TaskType

CLAUDE_CONTEXT_WINDOW = 200_000   # matches capability_matrix.yaml's cc-opus/cc-sonnet
                                    # models (the plain, non-[1M]-context ones -- see
                                    # capability_matrix.yaml's cc-opus comment)
COMPACT_THRESHOLD = 0.75           # ballpark from Claude Code's own auto-compact
                                    # (~70-80%)
COMPACTION_PROMPT = (
    "This conversation is approaching its context limit. Produce a structured "
    "handoff summary for a fresh session that will continue this work: concrete "
    "decisions made and why, unresolved items, and anything explicitly flagged "
    "as important. Do not summarize file contents -- the new session can Read "
    "any file directly. Output ONLY the summary text."
)


class ConversationError(Exception):
    """Raised when a task cannot receive a follow-up message (wrong kind,
    not started yet, etc). str(e) is safe to return to the HTTP caller."""


def estimate_context_usage_ratio(usage: Optional[dict]) -> float:
    """Approximate how full the model's context window is, from the same
    `usage` dict ExecResult already carries (see executors.py). Returns
    0.0 if usage is missing or unreadable -- callers treat that as "don't
    know, don't compact" rather than raising."""
    if not usage:
        return 0.0
    used = (usage.get("input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0))
    return used / CLAUDE_CONTEXT_WINDOW


async def continue_task_conversation(task_id: str, message: str, router,
                                      board: Blackboard) -> tuple[ExecResult, bool]:
    """Send `message` to whichever claude_code agent completed task_id,
    resuming its session -- or, if the last known reply ran the context
    hot (see COMPACT_THRESHOLD), first compact it into a handoff digest
    and open a fresh session seeded with that digest. Returns (result,
    compacted) where compacted is True iff this call performed a
    compaction pass before answering.

    Raises ConversationError if the task never ran, wasn't handled by a
    claude_code agent, or the agent is no longer in the capability matrix.

    Concurrency note: this function itself has no locking -- two concurrent
    calls for the SAME task_id would race on the same blackboard keys (last
    write wins) if invoked directly. serve.py's `POST /task/{id}/message`
    handler is what actually protects against this in practice: it runs on
    a `ThreadingHTTPServer` (one thread per request, same process) and
    holds a per-task_id `threading.Lock` around its call into this
    function, so concurrent follow-ups to the same task queue instead of
    racing (fixed 2026-07-31; see `_lock_for_task` in serve.py). A caller
    that invokes `continue_task_conversation` directly, outside that
    handler, does not get this protection for free.

    Known limitation (MVP): callers typically pass in a freshly-constructed
    Blackboard (see serve.py's _handle_task_message), and construction
    replays the entire blackboard.jsonl file from disk -- an O(file size)
    cost paid on every single follow-up message, just to look up two keys.
    Fine at MVP scale (this is request-frequency, not tick-frequency --
    only paid when a user actually sends a message); would need a shared,
    lock-protected Blackboard instance across serve.py's ThreadingHTTPServer
    request threads to do better, which is a separate, riskier change not
    bundled into this MVP. The hotter, tick-frequency instance of the same
    cost -- hub/engine.py's _await_input_loop polling for up to
    awaiting_input_timeout_sec while a task is AWAITING_INPUT -- IS fixed,
    via Blackboard.refresh()'s incremental (not full-replay) re-read.

    Known limitation (real-world, confirmed 2026-07-29 via a live T1 run):
    `claude -p --resume <session_id>` looks up the session by the cwd the
    original `claude -p` process ran in -- resume fails with "No conversation
    found" if called from a different directory. This function itself is
    workdir-agnostic (it goes through whatever ClaudeCodeExecutor the router
    hands it), but only works correctly end-to-end when the workdir used for
    the original task and the workdir used for this follow-up match. In
    practice they always do for the real product path (serve.py's /run and
    /task/{id}/message both default to workdir="."), but a task started via
    `main.py --workdir <other>` outside of serve.py cannot currently be
    resumed through this endpoint. Not tracked on the blackboard today; would
    need a tasks/{id}/workdir key if this needs to be fixed properly.
    """
    agent_name = board.get(f"tasks/{task_id}/agent")
    session_id = board.get(f"tasks/{task_id}/session_id")
    if not agent_name or not session_id:
        raise ConversationError(
            f"task '{task_id}' has no recorded claude_code session yet "
            "(it may not have completed, or wasn't handled by a claude_code agent)")

    try:
        executor = router.executor_for(agent_name)
    except KeyError:
        raise ConversationError(f"agent '{agent_name}' is no longer in the capability matrix")
    if not isinstance(executor, ClaudeCodeExecutor):
        raise ConversationError(
            f"agent '{agent_name}' is not a claude_code agent -- deep "
            "collaboration mode only supports claude_code for now")

    ratio = float(board.get(f"tasks/{task_id}/session_usage_ratio") or 0.0)
    compacted = False
    if ratio >= COMPACT_THRESHOLD:
        digest_result = await executor.run_resumed(COMPACTION_PROMPT, session_id)
        if digest_result.ok:
            board.put(f"tasks/{task_id}/session_digest", digest_result.output,
                      author="engine", task_id=task_id, kind="summary", confidence=0.85)
        digest = digest_result.output if digest_result.ok else ""
        fresh_task = Task(id=task_id, title="deep collaboration follow-up",
                          type=TaskType.CODING, prompt=message)
        result = await executor.run(fresh_task, digest)
        compacted = True
        # both the compaction call and the fresh-session call are real LLM
        # spend -- surface the total, not just the second call's cost, or
        # the digest pass's dollars silently vanish (see CLAUDE.md P0 "成本
        # 归因补全").
        result = dataclasses.replace(
            result,
            cost_usd=digest_result.cost_usd + result.cost_usd,
            cost_credits=digest_result.cost_credits + result.cost_credits,
        )
    else:
        result = await executor.run_resumed(message, session_id)

    # if the fresh session call itself fails after a successful compaction,
    # we deliberately leave the old session_id/ratio in place -- the next
    # follow-up will retry compaction+fresh from scratch against the old
    # session (safe, if not free; a smarter retry/backoff strategy is out
    # of scope for this MVP).
    if result.ok:
        # every successful reply IS the task's new deliverable version, same
        # as hub/engine.py's _persist_turn treats every AWAITING_INPUT turn
        # -- there's no MVP-level way to distinguish "just asking a question"
        # from "redo this", so we don't try; downstream staleness detection
        # below depends on this write actually happening.
        board.put(f"tasks/{task_id}/output", result.output,
                  author=agent_name, task_id=task_id, kind="output")
        if result.session_id:
            board.put(f"tasks/{task_id}/session_id", result.session_id,
                      author="engine", task_id=task_id, kind="fact")
        new_ratio = estimate_context_usage_ratio(result.usage)
        board.put(f"tasks/{task_id}/session_usage_ratio", new_ratio,
                  author="engine", task_id=task_id, kind="fact")
    return result, compacted


def mark_stale_dependents(board: Blackboard, task_id: str) -> list[str]:
    """Call after a DONE task's output changes via a deep-collaboration
    follow-up (see continue_task_conversation above). Finds tasks that
    directly depend on task_id (reconstructed from tasks/*/spec entries --
    the run's Engine process is typically long gone by the time a user goes
    back to chat with a done task, so there's no live Plan object to
    consult) and, for any of them already DONE, marks it STALE so the DAG's
    implicit guarantee ("my output reflects the upstream version I actually
    consumed") isn't silently violated. Returns the task ids marked.

    Scope (matches the design doc): direct dependents only, not transitive
    -- and detection/notification only, no auto-rerun. A stale task stays
    stale until something else (future work) forces it to re-execute.
    """
    marked = []
    for entry in board.query("tasks/"):
        if not entry["key"].endswith("/spec"):
            continue
        dep_id = entry["key"].split("/")[1]
        if task_id not in (entry["value"].get("depends_on") or []):
            continue
        if board.get(f"tasks/{dep_id}/status") == "done":
            board.put(f"tasks/{dep_id}/status", "stale",
                      author="system", task_id=dep_id, kind="fact")
            marked.append(dep_id)
    return marked
