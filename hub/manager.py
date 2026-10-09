"""Manager: the multi-turn clarification dialogue that runs BEFORE planning.

The manager is a normal Task (TaskType.MANAGER) routed through the same
Router/Executor machinery as everything else, so P0 protections (cost
attribution on failures, error triage, circuit skipping, paid-budget
routing) apply without re-implementation.

The only output of a clarification is two strings -- goal + constraints.
Once returned, the caller proceeds exactly like today's --goal path.
"""
from __future__ import annotations

import json
import re
from typing import Awaitable, Callable, Optional

from .models import Task, TaskType
from .router import Router

MANAGER_MAX_TURNS = 5   # Q&A turns; the final confirm step is exempt
MAX_QUESTIONS_PER_ROUND = 4
OTHER_OPTION_LABEL = "其他 —— 我自己写"

# Short, unambiguous phrases only -- matched EXACTLY after normalization,
# never by substring, so "开始之前我想先说明一下" can't false-positive.
_FORCE_START_PHRASES = {
    "开始", "开始吧", "直接开始", "开干", "开工", "就这样", "就这样吧",
    "别问了", "别问了直接开始", "不用问了", "go", "start", "startnow",
    "juststart", "letsgo",
}
_CONFIRM_PHRASES = _FORCE_START_PHRASES | {
    "对", "对的", "没错", "没问题", "可以", "行", "好", "好的", "确认",
    "对开始", "对开始吧", "yes", "y", "ok", "okay", "confirm", "confirmed",
    "lgtm", "sure",
}


def _norm(text: str) -> str:
    """Lowercase and strip whitespace/punctuation so phrase variants match."""
    return re.sub(r"[\s，。！？!?,.~、;；:：'\"“”‘’…-]+", "", text).lower()


def should_force_start(text: str) -> bool:
    """Keyword-only, no LLM call. True when the user/agent wants to skip
    remaining questions and jump straight to the manager's final proposal."""
    return _norm(text) in _FORCE_START_PHRASES


def is_confirmation(text: str) -> bool:
    """Keyword-only, no LLM call. True when the reply to a shown proposal
    means "yes, start". Anything else is treated as a correction and fed
    back into the conversation."""
    return _norm(text) in _CONFIRM_PHRASES


MANAGER_PROMPT = """You are the manager of a multi-agent orchestration system,
talking directly to the user before any work begins.

Your job is to surface three kinds of gaps before committing to a plan:
- known unknowns: things the user is aware they haven't decided (ask directly)
- unknown knowns: implicit assumptions the user takes for granted but hasn't
  stated (surface these with reflective/confirming questions)
- unknown unknowns: risks or considerations neither of you has raised yet
  (proactively flag these using your own judgment/experience)

Ask 1-4 focused questions per round -- group questions the user can answer
together. Once you believe you understand enough to proceed responsibly,
stop asking and propose a final goal + constraints for the user to confirm
-- you do not decide when work actually starts, the user does.

Respond with ONLY a JSON object, no markdown fences, no commentary:
- while still clarifying:
  {{"status": "asking", "questions": [
     {{"id": "q1", "question": "...",
       "options": [{{"label": "short choice", "detail": "what it implies"}}]}}]}}
- once you have a proposal: {{"status": "ready", "goal": "...", "constraints": "..."}}

Rules for questions:
- Offer 2-4 concrete options whenever the answer space is enumerable.
  Omit the "options" key entirely when the answer is genuinely open-ended.
- Keep each label short (a few words); put the explanation in "detail".
- Do NOT add an "other" / "something else" option yourself -- the UI adds one.

Conversation so far:
{history}
"""

_FORCE_FINISH_SUFFIX = (
    "\nIMPORTANT: You MUST respond with the \"ready\" JSON now, using your "
    "best judgment for anything still unclear. Do NOT ask another question."
)


def _normalize_questions(data: dict | None, raw_text: str) -> list[dict]:
    """Normalize new, legacy, and plain-text manager question shapes.

    Zero options is a valid open-answer mode.  The UI-required "other"
    option is appended here only when concrete options already exist.
    """
    items = None
    if isinstance(data, dict):
        if isinstance(data.get("questions"), list):
            items = data["questions"]
        elif data.get("question"):
            items = [{"question": str(data["question"])}]
    if items is None:
        items = [{"question": raw_text}]

    out: list[dict] = []
    for i, question in enumerate(items[:MAX_QUESTIONS_PER_ROUND], 1):
        if not isinstance(question, dict) or not question.get("question"):
            continue
        options: list[dict] = []
        raw_options = question.get("options")
        for option in (raw_options if isinstance(raw_options, list) else []):
            if isinstance(option, dict) and option.get("label"):
                options.append({"label": str(option["label"]),
                                "detail": str(option.get("detail") or ""),
                                "other": False})
            elif isinstance(option, str) and option.strip():
                options.append({"label": option.strip(), "detail": "", "other": False})
        if options:
            options.append({"label": OTHER_OPTION_LABEL, "detail": "", "other": True})
        out.append({"id": str(question.get("id") or f"q{i}"),
                    "question": str(question["question"]), "options": options})
    return out or [{"id": "q1", "question": raw_text, "options": []}]


def _render_questions_text(questions: list[dict]) -> str:
    """Render the canonical questions for string-only CLI/legacy clients."""
    if len(questions) == 1 and not questions[0]["options"]:
        return questions[0]["question"]
    lines: list[str] = []
    for i, question in enumerate(questions, 1):
        lines.append(f"{i}. {question['question']}")
        for j, option in enumerate(question["options"]):
            lines.append(f"   {chr(65 + j)}) {option['label']}")
    lines.append("（直接说你的选择，或者自己补充）")
    return "\n".join(lines)


def _render_history(history: list[dict]) -> str:
    return "\n".join(f"{h['role']}: {h['text']}" for h in history)


async def manager_step(history: list[dict], router: Router, workdir: str,
                       force_finish: bool = False,
                       on_event: Optional[Callable[[str], None]] = None,
                       execution_options: Optional[dict] = None) -> dict:
    """One LLM turn through the normal Router/Executor chain.

    status="ready" means "here is my proposal", NOT "go" -- the caller
    still needs an explicit user confirmation before starting anything.
    `on_event` is the Executor contract's streaming callback, forwarded
    unchanged. It is how the caller sees the manager is alive during a
    long high-reasoning turn: `claude -p` streams EMPTY thinking blocks
    (only an opaque signature -- probed 2026-07-23, re-probed 2026-08-31),
    so there is no reasoning text to show; what these callbacks give is
    the SHAPE of the reasoning (how many rounds, what it read, when it
    started writing). Do not invent narration to fill the gap.

    JSON handling mirrors planner.py: strip fences, tolerate garbage.
    Unparseable output falls back to a plain-text follow-up question;
    under force_finish (where only "ready" is acceptable) it retries the
    whole chain once, then raises.
    """
    prompt = MANAGER_PROMPT.format(history=_render_history(history))
    if force_finish:
        prompt += _FORCE_FINISH_SUFFIX
    attempts = 2 if force_finish else 1
    spent = 0.0
    last_err = "no agents available for the manager route"
    for _ in range(attempts):
        task = Task(id="_manager", title="clarify goal",
                    type=TaskType.MANAGER, prompt=prompt)
        try:
            chain = router.chain_for(task)
        except ValueError:
            if last_err != 'no agents available for the manager route':
                raise RuntimeError(last_err) from None
            raise
        for agent_name, executor in chain:
            if execution_options:
                executor = router.executor_with_options(agent_name, dict(execution_options))
            result = await executor.run(task, context="", on_event=on_event)
            spent += result.cost_usd
            if not result.ok and result.error_kind == "transient":
                # lightweight chat turn: one retry on the same agent, no
                # backoff (contrast with Engine's 2-retry exponential
                # backoff, which is overkill for a single dialogue turn)
                result = await executor.run(task, context="", on_event=on_event)
                spent += result.cost_usd
            if not result.ok:
                last_err = result.error or "unknown executor error"
                if result.error_kind == "unavailable" and not execution_options:
                    router.open_circuit(agent_name)
                continue
            text = re.sub(r"^```(json)?|```$", "", result.output.strip(), flags=re.M).strip()
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict) and data.get("status") == "ready" and data.get("goal"):
                return {"status": "ready", "goal": str(data["goal"]),
                        "constraints": str(data.get("constraints", "")),
                        "_cost_usd": spent}
            if isinstance(data, dict) and data.get("status") == "asking" and (
                    data.get("questions") or data.get("question")):
                questions = _normalize_questions(data, result.output.strip())
                return {"status": "asking", "questions": questions,
                        "question": _render_questions_text(questions),
                        "_cost_usd": spent}
            if not force_finish:
                # Unparseable output becomes one open question.  Keep the
                # string field for main.py and the legacy redesign frontend.
                questions = _normalize_questions(None, result.output.strip())
                return {"status": "asking", "questions": questions,
                        "question": _render_questions_text(questions),
                        "_cost_usd": spent}
            last_err = f"unparseable output under force_finish: {text[:200]}"
            break  # retry the whole chain once via the outer loop
    raise RuntimeError(f"manager unavailable: {last_err}")


async def run_clarification_loop(ask_user: Callable[[str], Awaitable[str]],
                                 router: Router, workdir: str, initial_goal: str,
                                 max_turns: int = MANAGER_MAX_TURNS
                                 ) -> tuple[str, str, float]:
    """Shared clarification core. CLI passes an input() wrapper as ask_user;
    tests pass a canned-answer stub.

    Flow: Q&A until the manager proposes (or the turn cap / a force-start
    answer forces it to), then show the proposal via ask_user and only
    return once is_confirmation() accepts the reply. Any other reply is a
    correction fed back into the conversation. The confirm step is exempt
    from the turn cap. Returns (goal, constraints, total_cost_usd).
    """
    history = [{"role": "user", "text": initial_goal}]
    total_cost = 0.0
    questions = 0
    force = False
    while True:
        step = await manager_step(history, router, workdir, force_finish=force)
        total_cost += step.get("_cost_usd", 0.0)
        if step["status"] == "asking":
            if force:
                raise RuntimeError("manager kept asking after being told to finalize")
            questions += 1
            history.append({"role": "manager", "text": step["question"]})
            answer = await ask_user(step["question"])
            history.append({"role": "user", "text": answer})
            if should_force_start(answer) or questions >= max_turns:
                force = True
            continue
        goal, constraints = step["goal"], step["constraints"]
        proposal = (f"确认一下——\n目标：{goal}\n约束：{constraints or '（无）'}\n"
                    f"没问题请回复\"对/开始\"；要改的话直接说哪里不对。")
        history.append({"role": "manager", "text": proposal})
        reply = await ask_user(proposal)
        history.append({"role": "user", "text": reply})
        if is_confirmation(reply):
            return goal, constraints, total_cost
        force = False   # correction round: the manager may ask again
