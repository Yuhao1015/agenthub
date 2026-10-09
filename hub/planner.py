"""Planner: decompose a goal into a Task DAG using the planning agent."""
from __future__ import annotations

import json
import re

from .models import Plan, Task, TaskType
from .router import Router

PLANNER_PROMPT = """You are a project planner in a multi-agent orchestration system.
Create the smallest useful task DAG for the goal and constraints below.
Task count must follow the user's request; there is no fixed minimum.

Available task types: planning, coding, writing, analysis, review, light.
Rules:
- Explicit goal and constraints take precedence over usual decomposition.
- The initial goal-start confirmation is already complete. Do not turn it into
  a request to ask for the same confirmation again; preserve separate approvals
  that the user requires before later actions.
- If the user requests one task, return exactly one task with no dependencies.
  Reading, writing and verification are steps inside that task, not extra tasks.
- Each task must be independently executable by one agent given only its
  prompt plus the outputs of its dependencies.
- Write each task's prompt as a complete, self-contained instruction.
- Add a separate `review` only when useful and allowed by the goal and constraints.
- Assign each task a difficulty: "easy" | "medium" | "hard", reflecting how
  much reasoning/context the task genuinely needs (not its importance).
- Optionally assign a specialty tag (e.g. "frontend", "backend") if the
  task clearly calls for one; omit the field or use null otherwise.
- Respond with ONLY a JSON object, no markdown fences, no commentary:

{{"goal": "...", "tasks": [{{"id": "t1", "title": "...", "type": "coding",
  "prompt": "...", "depends_on": [], "difficulty": "medium", "specialty": null}}]}}

GOAL: {goal}
CONSTRAINTS: {constraints}
"""


async def make_plan(goal: str, router: Router, constraints: str = "") -> Plan:
    planner_task = Task(id="_plan", title="decompose goal",
                        type=TaskType.PLANNING,
                        prompt=PLANNER_PROMPT.format(goal=goal, constraints=constraints or "(none)"))
    last_err = ""
    for agent_name, executor in router.chain_for(planner_task):
        result = await executor.run(planner_task, context="")
        if not result.ok:
            last_err = result.error
            continue
        text = result.output.strip()
        # strip accidental code fences
        text = re.sub(r"^```(json)?|```$", "", text, flags=re.M).strip()
        try:
            data = json.loads(text)
            return Plan.from_json(data)
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            last_err = f"planner returned invalid plan: {e}"
            continue
    raise RuntimeError(f"planning failed: {last_err}")
