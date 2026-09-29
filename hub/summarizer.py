"""Summarizer: compress a completed task's output into a handoff summary
for downstream tasks.

Reuses the same "throwaway Task through Router.chain_for()" pattern
hub/manager.py uses for clarification turns -- this gets P0's cost
attribution, error triage, and circuit-breaker protection for free
instead of reimplementing them.

See docs/superpowers/specs/2026-07-23-summarizer-agent-design.md.
"""
from __future__ import annotations

from typing import Callable, Optional

from .models import Task, TaskType

SUMMARIZE_TRIGGER_LEN = 600   # at or below this, the raw output IS the summary
MAX_SUMMARIZE_INPUT_LEN = 12000   # head-truncate before embedding in the prompt --
                                    # keeps the subprocess argv comfortably under
                                    # Windows' ~32,767-char CreateProcess limit, and
                                    # caps the summarizer's own token cost

SUMMARIZER_PROMPT = """You are compressing the output of a completed task
into a concise handoff summary for another agent. That agent will use
this summary as its ONLY visibility into what "{title}" produced --
it cannot see the original output below.

Preserve, in priority order:
1. Concrete deliverables (file paths, generated artifacts, key numbers/results)
2. Decisions made and why (if any)
3. Anything explicitly flagged as important, unresolved, or an error

Cut narration, restated instructions, and step-by-step process descriptions.
Target roughly 150-250 words. Output ONLY the summary text -- no preamble,
no markdown headers, no "Summary:" prefix.

## Original output
{output}
"""


async def summarize(output: str, task_title: str, router,
                     on_circuit_open: Optional[Callable[[str, str], None]] = None
                     ) -> tuple[Optional[str], Optional[str], float, float]:
    """Try each agent in the economy-tier chain (via a throwaway
    TaskType.LIGHT/difficulty=easy pseudo-task) until one produces a
    summary. on_circuit_open, if given, is called with (agent_name, reason)
    whenever an agent is circuit-broken. Returns (summary_text, agent_name,
    cost_usd, cost_credits); summary_text and agent_name are None if the
    whole chain failed (or no chain was configured at all) -- the caller
    must NOT write a blackboard summary in that case, its existing
    raw-output fallback is already better than any placeholder we could
    write here.

    Cost is accumulated across every attempt, including failed ones.
    """
    prompt = SUMMARIZER_PROMPT.format(title=task_title,
                                       output=output[:MAX_SUMMARIZE_INPUT_LEN])
    probe = Task(id="_summarize", title="summarize", type=TaskType.LIGHT,
                 difficulty="easy", prompt=prompt)

    try:
        chain = router.chain_for(probe)
    except ValueError:
        return None, None, 0.0, 0.0

    cost_usd = 0.0
    cost_credits = 0.0
    for agent_name, executor in chain:
        result = await executor.run(probe, "")
        cost_usd += result.cost_usd
        cost_credits += result.cost_credits
        if not result.ok and result.error_kind == "transient":
            result = await executor.run(probe, "")
            cost_usd += result.cost_usd
            cost_credits += result.cost_credits
        if result.ok:
            return result.output.strip(), agent_name, cost_usd, cost_credits
        if result.error_kind == "unavailable":
            router.open_circuit(agent_name)
            if on_circuit_open:
                on_circuit_open(agent_name, (result.error or "")[:200])
        # task_error, or the transient retry also failed -- try next agent

    return None, None, cost_usd, cost_credits
