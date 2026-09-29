"""Planner: decompose a goal into a Task DAG using the planning agent."""
from __future__ import annotations

import json
import re

from .models import Plan, Task, TaskType
from .router import Router

PLANNER_PROMPT = """You are a project planner in a multi-agent orchestration system.
Decompose the goal below into 3-8 tasks forming a DAG.

Available task types: planning, coding, writing, analysis, review, light.
Rules:
- Each task must be independently executable by one agent given only its
  prompt plus the outputs of its dependencies.
- Write each task's prompt as a complete, self-contained instruction.
- The final task should usually be a `review` that checks upstream outputs.
- Assign each task a difficulty: "easy" | "medium" | "hard", reflecting how
  much reasoning/context the task genuinely needs (not its importance).
- Optionally assign a specialty tag (e.g. "frontend", "backend") if the
  task clearly calls for one; omit the field or use null otherwise.
- Respond with ONLY a JSON object, no markdown fences, no commentary:

{{"goal": "...", "tasks": [{{"id": "t1", "title": "...", "type": "coding",
  "prompt": "...", "depends_on": [], "difficulty": "medium", "specialty": null}}]}}

GOAL: {goal}
"""


async def make_plan(goal: str, router: Router) -> Plan:
    planner_task = Task(id="_plan", title="decompose goal",
                        type=TaskType.PLANNING,
                        prompt=PLANNER_PROMPT.format(goal=goal))
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
