"""Core data models: Task, Plan (DAG), Event.

This file defines the contract that everything else (router, engine,
future pixel frontend) depends on. Keep it stable.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .blackboard import Blackboard  # type-only, avoids a runtime import
                                          # cycle (blackboard.py has no
                                          # reason to import models.py back)


class TaskStatus(str, Enum):
    PENDING = "pending"        # waiting for dependencies
    READY = "ready"            # deps met, waiting for a worker
    RUNNING = "running"
    AWAITING_INPUT = "awaiting_input"  # a claude_code turn finished and a user
                                # message arrived during it -- reactively
                                # paused in a resume loop, not a DAG-terminal
                                # state (ready_tasks/finished() treat it like
                                # any other non-done status)
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"        # upstream failed
    STALE = "stale"            # was DONE, but an upstream dependency's output
                                # changed after this task already consumed it
                                # (deep collaboration follow-up on a done task)


class TaskType(str, Enum):
    """Task categories used by the router. Extend as needed."""
    PLANNING = "planning"          # goal decomposition / architecture
    CODING = "coding"              # writing code (may touch local files)
    WRITING = "writing"            # docs, reports, copy
    ANALYSIS = "analysis"          # research / long-context analysis
    REVIEW = "review"              # QA of another task's output
    IMAGE = "image"                # image generation (Phase 2)
    LIGHT = "light"                # summarize / translate / classify
    MANAGER = "manager"            # multi-turn goal clarification with the user


@dataclass
class Task:
    id: str
    title: str
    type: TaskType
    prompt: str                            # instruction for the agent
    depends_on: list[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    agent: Optional[str] = None            # assigned by router
    result: Optional[str] = None           # final deliverable text
    error: Optional[str] = None
    cost_usd: float = 0.0
    cost_credits: float = 0.0              # workbuddy/codebuddy credit spend, separate ledger
    summary_cost_usd: float = 0.0          # Summarizer LLM cost for this task's handoff summary
    summary_cost_credits: float = 0.0      # same, workbuddy/codebuddy credit ledger -- tracked
                                            # separately from cost_usd/cost_credits on purpose
    attempts: int = 0
    difficulty: str = "medium"             # "easy" | "medium" | "hard", set by Planner
    specialty: Optional[str] = None        # e.g. "frontend"; None = no preference
    pinned_agent: Optional[str] = None     # 用户在计划门上钉死的 agent。
    execution_options: dict = field(default_factory=dict)  # explicit per-task model/effort snapshot
                                            # 独占：设了就不走降级链 -- 用户
                                            # 钉选的动机常常是「别用 X 省额度」，
                                            # 偷偷降级回 X 是背叛。见 spec §3.7

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["type"] = self.type.value
        d["status"] = self.status.value
        return d


@dataclass
class Plan:
    goal: str
    tasks: dict[str, Task] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Plan":
        plan = cls(goal=data["goal"])
        for t in data["tasks"]:
            task = Task(
                id=t["id"],
                title=t["title"],
                type=TaskType(t["type"]),
                prompt=t["prompt"],
                depends_on=t.get("depends_on", []),
                difficulty=t.get("difficulty", "medium"),
                specialty=t.get("specialty"),
                pinned_agent=t.get("pinned_agent"),
                execution_options=dict(t.get('execution_options') or {}),
            )
            plan.tasks[task.id] = task
        plan.validate()
        return plan

    @classmethod
    def from_blackboard(cls, board: "Blackboard") -> "Plan":
        """Reconstruct a Plan from tasks/*/spec entries already on the
        board -- every run writes these before any task starts, regardless
        of whether the plan came from --plan or --goal (see hub/engine.py's
        run()). Used by main.py's --resume-without-goal/--plan path: a
        goal-driven run never persists its Planner-generated plan to a
        reusable file, so reconstructing from the board is the only way to
        force-rerun STALE tasks against it (see hub/conversation.py's
        mark_stale_dependents)."""
        plan = cls(goal=board.get("goal/statement", ""))
        for entry in board.query("tasks/"):
            if not entry["key"].endswith("/spec"):
                continue
            task_id = entry["key"].split("/")[1]
            spec = entry["value"]
            plan.tasks[task_id] = Task(
                id=task_id,
                title=spec.get("title", task_id),
                type=TaskType(spec["type"]),
                prompt=spec.get("prompt", ""),
                depends_on=spec.get("depends_on", []),
                difficulty=spec.get("difficulty", "medium"),
                specialty=spec.get("specialty"),
                pinned_agent=spec.get("pinned_agent"),
                execution_options=dict(spec.get('execution_options') or {}),
            )
        plan.validate()
        return plan

    def validate(self) -> None:
        """Reject unknown deps and cycles (simple DFS)."""
        for t in self.tasks.values():
            for dep in t.depends_on:
                if dep not in self.tasks:
                    raise ValueError(f"Task {t.id} depends on unknown task {dep}")
        visited: dict[str, int] = {}  # 0=in-progress, 1=done

        def dfs(tid: str) -> None:
            if visited.get(tid) == 0:
                raise ValueError(f"Cycle detected at task {tid}")
            if visited.get(tid) == 1:
                return
            visited[tid] = 0
            for dep in self.tasks[tid].depends_on:
                dfs(dep)
            visited[tid] = 1

        for tid in self.tasks:
            dfs(tid)

    def ready_tasks(self) -> list[Task]:
        out = []
        for t in self.tasks.values():
            if t.status != TaskStatus.PENDING:
                continue
            deps = [self.tasks[d] for d in t.depends_on]
            if all(d.status == TaskStatus.DONE for d in deps):
                out.append(t)
            elif any(d.status in (TaskStatus.FAILED, TaskStatus.SKIPPED) for d in deps):
                t.status = TaskStatus.SKIPPED
        return out

    def finished(self) -> bool:
        return all(
            t.status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.SKIPPED)
            for t in self.tasks.values()
        )


class EventBus:
    """Append-only JSONL event log + stdout mirror.

    This is the single source of truth the pixel frontend will subscribe
    to later (via WebSocket tailing this file or an in-process queue).
    Event types: plan_created, task_ready, task_started, agent_thinking,
    agent_failed, agent_circuit_open, preflight_result, task_completed,
    task_failed, run_finished, blackboard_write, summary_generated,
    task_awaiting_input, task_message_replied, task_auto_finished, task_stale,
    run_crashed, run_cancelled, gate_opened, gate_resolved, run_rolled_back.
    Additive fields: plan_created.checkpoints, task_completed.changes
    (docs/decisions/change-safety-net.md §3.4).
    """

    def __init__(self, path: str):
        self.path = path
        self._fh = open(path, "a", encoding="utf-8")

    def emit(self, type: str, **kw: Any) -> None:
        evt = {"ts": round(time.time(), 3), "id": uuid.uuid4().hex[:8], "type": type, **kw}
        line = json.dumps(evt, ensure_ascii=False)
        self._fh.write(line + "\n")
        self._fh.flush()
        print(f"[{type}] " + json.dumps(kw, ensure_ascii=False)[:200])

    def close(self) -> None:
        self._fh.close()
