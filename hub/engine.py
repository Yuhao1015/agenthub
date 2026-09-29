"""Engine: async DAG runner + blackboard data plane.

Control plane  = the DAG (who runs when) — deterministic, debuggable.
Data plane     = the Blackboard (what everyone knows) — shared, persistent.

Context handoff: each task reads from the blackboard:
  - goal/statement + goal/constraints (the user's will, injected everywhere)
  - tasks/{dep}/summary if present, else tasks/{dep}/output (capped) -- the
    summary is written by hub/summarizer.py, and only when the upstream
    output is long AND some other task actually depends on it; otherwise
    downstream reads the raw (capped) output directly, which is already a
    strictly more generous fallback than any placeholder summary would be.
  - recent notes/* (opportunistic findings from other agents)

Opportunism hook: any line in an agent's output starting with "NOTE:"
is lifted onto the blackboard as notes/*, visible to all later tasks.

Crash recovery: tasks whose tasks/{id}/output already exists on the
blackboard are restored as DONE and skipped (enable via resume=True) --
EXCEPT tasks marked STALE (see hub/conversation.py's mark_stale_dependents),
which stay PENDING and re-execute. That exception is also forced rerun's
entire mechanism: main.py --resume with neither --goal nor --plan
reconstructs the DAG via Plan.from_blackboard() and this same resume loop
naturally reruns only what's STALE, restoring everything else unchanged.
"""
from __future__ import annotations

import asyncio
import os
import uuid

from .artifacts import changed_files, snapshot
from .blackboard import Blackboard
from .conversation import estimate_context_usage_ratio
from .executors import ClaudeCodeExecutor
from .models import EventBus, Plan, Task, TaskStatus, TaskType
from .router import Router
from .summarizer import SUMMARIZE_TRIGGER_LEN, summarize

MAX_OUTPUT_CTX = 4000     # cap when falling back to raw output
MAX_NOTES_CTX = 6         # most recent notes injected into context
TRANSIENT_MAX_RETRIES = 2     # same-agent retries before falling through the chain
TRANSIENT_BACKOFF_BASE = 1.0  # seconds, doubles each retry
PREFLIGHT_PROMPT = "reply ok"
AWAITING_INPUT_POLL_INTERVAL = 3.0  # seconds between inbox checks while idle
AWAITING_INPUT_TIMEOUT = 600.0      # default seconds of silence before auto-finalizing;
                                     # overridable via capability_matrix.yaml's
                                     # budget.awaiting_input_timeout_sec
GATE_POLL_INTERVAL = 1.0    # 门的轮询比 AWAITING_INPUT_POLL_INTERVAL(3s) 快，
                             # 因为门后面站着一个正在等的人
WRITE_TOOLS = {"Edit", "Write", "Bash"}      # claude_code 的写盘工具
ALWAYS_WRITE_KINDS = {"codebuddy", "codex", "dsh"}  # 全权 agent：自带 tools/skills，
                                              # Agent Hub 对它们的工具权限没有
                                              # 任何可见性，保守当写盘处理
                                              # （宁可多问，不装作能判断）


class Engine:
    def __init__(self, plan: Plan, router: Router, bus: EventBus,
                 board: Blackboard, max_parallel: int = 3, resume: bool = False,
                 preflight: bool = True, constraints: str = "",
                 summarize_enabled: bool = True, workdir: str = ".",
                 gate_mode: str = "auto", assign_mode: str = "auto"):
        self.plan = plan
        self.router = router
        self.bus = bus
        self.board = board
        # where agents actually write files. Kept here rather than read off
        # the Router (which only forwards it to build_executor and never
        # stores it) so the Engine keeps working with the stub routers the
        # tests build.
        self.workdir = workdir
        self._max_parallel = max_parallel
        # lazy: constructing asyncio.Semaphore() outside a running loop
        # raises "no current event loop" on Python 3.9 if a prior
        # asyncio.run() call already tore one down in this thread (hit by
        # every test file that builds >1 Engine() across separate
        # asyncio.run() calls in one process). Deferring construction to
        # first use inside an async method sidesteps that entirely.
        self._sem: asyncio.Semaphore | None = None
        self.total_cost = 0.0   # combined USD, for reporting
        self.total_credits = 0.0  # combined workbuddy/codebuddy credits, separate ledger
        self.paid_cost = 0.0    # pay-as-you-go only; this is what max_usd_per_run gates
        self.resume = resume
        self.preflight = preflight
        self.constraints = constraints
        self.summarize_enabled = summarize_enabled
        # "auto" | "ask"：写盘任务开跑前是否停下来问
        self.gate_mode = gate_mode
        # "auto" | "manual"：模型由路由决定还是用户在计划门上钉
        self.assign_mode = assign_mode
        # reverse dependency index: task id -> "does some other task depend
        # on it?" -- computed once so _run_task() can skip summarizing
        # leaf/terminal tasks nobody will ever read the summary of
        self._has_dependents: set[str] = set()
        for t in plan.tasks.values():
            self._has_dependents.update(t.depends_on)

    @property
    def sem(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._max_parallel)
        return self._sem

    # ---------- startup: probe every local CLI agent once ----------
    async def _run_preflight(self) -> None:
        probe = Task(id="_preflight", title="preflight", type=TaskType.LIGHT,
                     prompt=PREFLIGHT_PROMPT)
        # getattr keeps old third-party/test Router stubs source-compatible;
        # real Router always exposes codex_agents().
        codex_agents = getattr(self.router, "codex_agents", lambda: [])()
        for agent_name in self.router.claude_code_agents() + codex_agents:
            executor = self.router.executor_for(agent_name)
            runner = (getattr(executor, "run_preflight", executor.run)
                      if agent_name in codex_agents else executor.run)
            result = await runner(probe, "")
            self.total_cost += result.cost_usd
            self.bus.emit("preflight_result", agent=agent_name, ok=result.ok,
                          cost_usd=round(result.cost_usd, 4), cost_credits=0.0,
                          usage=result.usage)
            if not result.ok:
                self.router.open_circuit(agent_name)
                self.bus.emit("agent_circuit_open", agent=agent_name, task_id=None,
                              reason=f"preflight failed: {result.error[:200]}")
        for agent_name in self.router.codebuddy_agents():
            executor = self.router.executor_for(agent_name)
            result = await executor.run(probe, "")
            self.total_credits += result.cost_credits
            self.bus.emit("preflight_result", agent=agent_name, ok=result.ok,
                          cost_usd=0.0, cost_credits=round(result.cost_credits, 4))
            if not result.ok:
                self.router.open_circuit(agent_name)
                self.bus.emit("agent_circuit_open", agent=agent_name, task_id=None,
                              reason=f"preflight failed: {result.error[:200]}")

    # ---------- blackboard-backed context ----------
    def _context_for(self, task: Task) -> str:
        parts = []
        goal = self.board.get("goal/statement")
        if goal:
            parts.append(f"## Goal\n{goal}")
        constraints = self.board.get("goal/constraints")
        if constraints:
            parts.append(f"## Constraints (from the user)\n{constraints}")
        for dep_id in task.depends_on:
            dep = self.plan.tasks.get(dep_id)
            summary = self.board.get(f"tasks/{dep_id}/summary")
            output = self.board.get(f"tasks/{dep_id}/output")
            body = summary or (output[:MAX_OUTPUT_CTX] if output else None)
            if body:
                parts.append(f"## Upstream [{dep.title if dep else dep_id}]\n{body}")
        notes = self.board.query("notes/")[-MAX_NOTES_CTX:]
        if notes:
            lines = "\n".join(
                f"- ({n['author']}, confidence={n['confidence']}) {n['value']}"
                for n in notes)
            parts.append(f"## Notes from other agents\n{lines}")
        return "\n\n".join(parts)

    # ---------- opportunism: lift NOTE: lines onto the board ----------
    def _harvest_notes(self, task: Task, output: str) -> None:
        for line in output.splitlines():
            s = line.strip()
            if s.upper().startswith("NOTE:") and len(s) > 6:
                self.board.add_note(s[5:].strip(), author=task.agent or "unknown",
                                    task_id=task.id)

    # ---------- turn bookkeeping shared by the first turn and any later
    # resumed ones (reactive AWAITING_INPUT loop) ----------
    def _persist_turn(self, task: Task, agent_name: str, executor, result) -> None:
        task.result = result.output
        self.board.put(f"tasks/{task.id}/output", result.output,
                       author=agent_name, task_id=task.id, kind="output")
        self.board.put(f"tasks/{task.id}/agent", agent_name,
                       author="engine", task_id=task.id, kind="fact")
        if result.session_id and isinstance(executor, ClaudeCodeExecutor):
            self.board.put(f"tasks/{task.id}/session_id", result.session_id,
                           author="engine", task_id=task.id, kind="fact")
            ratio = estimate_context_usage_ratio(result.usage)
            self.board.put(f"tasks/{task.id}/session_usage_ratio", ratio,
                           author="engine", task_id=task.id, kind="fact")

    # ---------- DAG-terminal bookkeeping: summarize, harvest notes, mark
    # DONE. Called either immediately (nobody messaged this task while it
    # ran -- the overwhelming common case) or after an AWAITING_INPUT loop
    # wraps up (finish signal or timeout) ----------
    def _workdir_snapshot(self):
        """events.jsonl/blackboard.jsonl are written during every single
        task, so they must never count as anyone's deliverable."""
        return snapshot(self.workdir, exclude=(self.bus.path, self.board.path))

    async def _finalize_done(self, task: Task, agent_name: str, result,
                             before=None) -> None:
        if (self.summarize_enabled
                and len(result.output) > SUMMARIZE_TRIGGER_LEN
                and task.id in self._has_dependents):
            try:
                summary_text, summ_agent, summ_cost_usd, summ_cost_credits = \
                    await summarize(
                        result.output, task.title, self.router,
                        on_circuit_open=lambda name, reason: self.bus.emit(
                            "agent_circuit_open", agent=name,
                            task_id=task.id, reason=reason))
            except Exception:
                summary_text, summ_agent = None, None
                summ_cost_usd, summ_cost_credits = 0.0, 0.0
            task.summary_cost_usd += summ_cost_usd
            task.summary_cost_credits += summ_cost_credits
            self.total_cost += summ_cost_usd
            self.total_credits += summ_cost_credits
            self.bus.emit("summary_generated", task_id=task.id,
                          agent=summ_agent, ok=summary_text is not None,
                          cost_usd=round(summ_cost_usd, 4),
                          cost_credits=round(summ_cost_credits, 4),
                          output_len=len(result.output),
                          summary_len=len(summary_text) if summary_text else 0)
            if summary_text:
                self.board.put(f"tasks/{task.id}/summary", summary_text,
                               author="engine", task_id=task.id,
                               kind="summary", confidence=0.85)
        self._harvest_notes(task, result.output)
        # real files this task left on disk (see hub/artifacts.py). Recorded
        # on the board so serve.py's GET /file can use it as a whitelist,
        # and carried on task_completed so the frontend can offer a preview
        # straight off the event stream (additive field, older frontends
        # that ignore it are unaffected).
        files = changed_files(before, self._workdir_snapshot())
        if files:
            self.board.put(f"tasks/{task.id}/files", files,
                           author="engine", task_id=task.id, kind="fact")
        task.status = TaskStatus.DONE
        self.board.put(f"tasks/{task.id}/status", "done",
                       author="engine", task_id=task.id, kind="fact")
        extra = {"usage": result.usage} if result.usage else {}
        self.bus.emit("task_completed", task_id=task.id, agent=agent_name,
                      cost_usd=round(task.cost_usd, 4),
                      cost_credits=round(task.cost_credits, 4),
                      preview=result.output[:200], files=files, **extra)

    # ---------- reactive long-collaboration loop: only entered when the
    # blackboard inbox already has >=1 message queued the instant a
    # claude_code turn finishes. A task nobody messages never reaches this
    # -- zero added latency for normal DAG runs. Releases the concurrency
    # slot while idle/waiting so a long human back-and-forth doesn't starve
    # other independent tasks; reacquires before returning so the caller's
    # semaphore hold is left exactly as it found it. ----------
    async def _await_input_loop(self, task: Task, agent_name: str, executor,
                                 session_id: str, start_idx: int, initial_result):
        task.status = TaskStatus.AWAITING_INPUT
        self.bus.emit("task_awaiting_input", task_id=task.id, agent=agent_name)
        timeout = float(self.router.budget.get(
            "awaiting_input_timeout_sec", AWAITING_INPUT_TIMEOUT))
        idx = start_idx
        last_ok_result = initial_result
        silent_for = 0.0
        self.sem.release()
        # one Blackboard, incrementally refreshed each tick instead of a
        # full from-scratch replay every poll -- a session can sit in this
        # loop for up to `timeout` seconds (default 600s, ~200 ticks at
        # AWAITING_INPUT_POLL_INTERVAL), and re-parsing the ENTIRE
        # blackboard.jsonl on every single tick regardless of whether
        # anything changed was real, avoidable cost on a long-running
        # session (see Blackboard.refresh()'s docstring)
        probe = Blackboard(self.board.path, on_write=None)
        try:
            while True:
                probe.refresh()
                entry = probe.get(f"tasks/{task.id}/inbox/{idx}")
                if entry is not None:
                    idx += 1
                    silent_for = 0.0
                    await self.sem.acquire()
                    try:
                        result = await executor.run_resumed(entry, session_id)
                    finally:
                        self.sem.release()
                    self.total_cost += result.cost_usd
                    self.total_credits += result.cost_credits
                    task.cost_usd += result.cost_usd
                    task.cost_credits += result.cost_credits
                    if result.ok:
                        last_ok_result = result
                        session_id = result.session_id or session_id
                        self._persist_turn(task, agent_name, executor, result)
                        self.bus.emit("task_message_replied", task_id=task.id,
                                      agent=agent_name, reply=result.output[:500],
                                      cost_usd=round(result.cost_usd, 4))
                    else:
                        self.bus.emit("agent_failed", task_id=task.id, agent=agent_name,
                                      error=result.error[:300], error_kind=result.error_kind,
                                      cost_usd=round(result.cost_usd, 4),
                                      cost_credits=round(result.cost_credits, 4))
                    continue
                if probe.get(f"tasks/{task.id}/inbox_finish"):
                    break
                if silent_for >= timeout:
                    self.bus.emit("task_auto_finished", task_id=task.id, agent=agent_name,
                                  reason=f"no reply within {int(timeout)}s")
                    break
                await asyncio.sleep(AWAITING_INPUT_POLL_INTERVAL)
                silent_for += AWAITING_INPUT_POLL_INTERVAL
        finally:
            await self.sem.acquire()
        return last_ok_result

    # ---------- 门（spec §3）----------

    def _available_agents(self) -> list[dict]:
        """计划门的 agent 选择器数据。**排除已熔断的**。

        这条是独占钉选能安全的全部原因：run() 里 _run_preflight() 跑在
        plan_created 之前，所以这里的熔断状态是真的，本机没装/没登录的
        agent 在 UI 上根本选不到。
        """
        open_ = self.router.circuit_open()
        out = []
        for name in self.router.all_agents():
            if name in open_:
                continue
            out.append({"name": name,
                        "kind": self.router.kind(name),
                        "animal": self.router.animal(name),
                        "tier": self.router.tier_of(name),
                        "specialty": self.router.specialty(name),
                        "blurb": self.router.blurb(name)})
        return out

    def _new_gate_token(self) -> str:
        """Unique instance id; a test seam keeps gate polling deterministic."""
        return uuid.uuid4().hex[:16]

    async def _await_gate(self, gate_id: str, payload: dict) -> dict:
        """挂起直到用户对这一次 gate 实例作出答复。

        **刻意不碰 self.sem。** _await_input_loop 要 release/acquire 是
        因为它坐在一个已经占用的任务槽里；计划门在 run() 里等（还没进任何
        槽），写盘门开在 _run_task 的 acquire 之前（同样没占槽）。

        超时只能取消，不能放行 -- 超时自动放行等于权限模式失效
        （spec §3.4）。默认 gate_timeout_sec=0 即永不超时。
        """
        gate_key = f"run/gate/{gate_id}"
        reply_key = f"run/gate_reply/{gate_id}"
        gate_token = self._new_gate_token()
        opened = dict(payload)
        opened["gate_token"] = gate_token
        self.board.put(gate_key, opened, author="engine", kind="fact")
        self.bus.emit("gate_opened", gate_id=gate_id, **opened)
        timeout = float(self.router.budget.get("gate_timeout_sec", 0) or 0)
        probe = Blackboard(self.board.path, on_write=None)
        waited = 0.0
        while True:
            probe.refresh()
            reply = probe.get(reply_key)
            matches = (isinstance(reply, dict) and
                       reply.get("gate_token") == gate_token)
            if matches:
                decision = str(reply.get("decision") or "cancel")
                self.bus.emit("gate_resolved", gate_id=gate_id,
                              gate_token=gate_token, decision=decision,
                              overrides=reply.get("overrides") or {})
                return reply
            if timeout and waited >= timeout:
                self.bus.emit("gate_resolved", gate_id=gate_id,
                              gate_token=gate_token, decision="cancel",
                              reason=f"no answer within {timeout}s")
                return {"decision": "cancel"}
            await asyncio.sleep(GATE_POLL_INTERVAL)
            waited += GATE_POLL_INTERVAL

    def _allowed_tools_of(self, agent_name: str) -> list[str]:
        """claude_code 执行器的 allowed_tools，拆成列表。

        存进来的形状可能是 "Read, Edit"（POST /settings/tools 用逗号
        空格拼的），所以要 strip。

        优先读 Router 声明值而不是执行器实例属性：mock_all=True 时执行器
        是 MockExecutor，没有这个属性 -- 那样 --mock 下写盘门永远不开，
        权限流程就没法预演了。
        """
        raw = ""
        get = getattr(self.router, "allowed_tools", None)
        if callable(get):
            raw = get(agent_name) or ""
        if not raw:
            # 测试里的桩 Router 没有 allowed_tools()，回落到执行器属性
            try:
                raw = getattr(self.router.executor_for(agent_name),
                              "allowed_tools", "") or ""
            except (KeyError, AttributeError):
                raw = ""
        return [t.strip() for t in str(raw).split(",") if t.strip()]

    def _task_chain(self, task: Task):
        """Return the effective execution chain for one task.

        Automatic routing uses the task-type fallback chain.  A user pin is
        different: the plan gate offers every preflight-surviving matrix
        agent, including agents outside that task type's automatic route, so
        filtering the automatic chain can turn a valid selection into an
        empty chain.  Build the one-item exclusive chain directly instead.
        """
        if not task.pinned_agent:
            return self.router.chain_for(task)
        name = task.pinned_agent
        circuit_open = getattr(self.router, "circuit_open", None)
        if callable(circuit_open) and name in circuit_open():
            return []
        try:
            return [(name, self.router.executor_for(name))]
        except (KeyError, AttributeError):
            return []

    def _is_write_task(self, task: Task) -> bool:
        """这个任务开跑前要不要停在写盘门上（spec §3.8）。

        codebuddy/codex/dsh 是全权 agent，自带 tools/subagents/skills，Agent Hub
        对它们的工具权限**没有任何可见性** -- 保守地全当写盘处理，宁可多
        问几次，也不装作能判断。
        """
        for name, _executor in self._task_chain(task):
            kind = self.router.kind(name)
            if kind in ALWAYS_WRITE_KINDS:
                return True
            if kind == "claude_code" and any(
                    tool in WRITE_TOOLS for tool in self._allowed_tools_of(name)):
                return True
        return False

    def _skip_all_pending(self) -> None:
        """把还没跑的任务全部置 SKIPPED，让 plan.finished() 成立。

        这样 run() 的主循环自然空转结束、正常走到 run_finished -- 提前
        return 会漏掉那个事件，serve.py 的 _watch_run 看门狗就会把这次
        运行误判成 crashed。
        """
        for t in self.plan.tasks.values():
            if t.status in (TaskStatus.PENDING, TaskStatus.READY):
                t.status = TaskStatus.SKIPPED

    # ---------- run one task ----------
    async def _run_task(self, task: Task) -> None:
        if self.gate_mode == "ask" and self._is_write_task(task):
            # 门开在 acquire 之前：等用户点头的时候不占并发槽，只读任务
            # 照跑（spec §3.3）
            chain = self._task_chain(task)
            intended = chain[0][0] if chain else ""
            reply = await self._await_gate(f"task:{task.id}", {
                "kind": "task", "task_id": task.id, "title": task.title,
                "agent": intended,                     # 「预计使用」-- 降级链
                                                        # 可能改变实际执行者
                "allowed_tools": self._allowed_tools_of(intended)})
            decision = str(reply.get("decision") or "cancel")
            if decision == "skip":
                task.status = TaskStatus.SKIPPED
                return
            if decision != "approve":
                self.bus.emit("run_cancelled",
                              reason=f"task gate {task.id} rejected")
                task.status = TaskStatus.SKIPPED
                self._skip_all_pending()
                return
        await self.sem.acquire()
        try:
            budget = float(self.router.budget.get("max_usd_per_run", 1e9))
            before = self._workdir_snapshot()
            task.status = TaskStatus.RUNNING
            context = self._context_for(task)
            max_attempts = int(self.router.budget.get("max_attempts_per_task", 2))

            chain = self._task_chain(task)
            for agent_name, executor in chain:
                if task.attempts >= max_attempts:
                    break

                # subscription/claude_code agents are never budget-gated; only
                # pay-as-you-go agents (openrouter) can trip max_usd_per_run
                if self.router.is_paid_api(agent_name) and self.paid_cost >= budget:
                    task.attempts += 1
                    task.agent = agent_name
                    self.bus.emit("agent_failed", task_id=task.id, agent=agent_name,
                                  error="paid API budget exceeded", error_kind="unavailable",
                                  cost_usd=0.0, cost_credits=0.0)
                    self.router.open_circuit(agent_name)
                    self.bus.emit("agent_circuit_open", agent=agent_name, task_id=task.id,
                                  reason="paid API budget exceeded")
                    continue

                task.attempts += 1
                task.agent = agent_name

                def _on_event(detail: str) -> None:
                    self.bus.emit("agent_thinking", task_id=task.id, agent=agent_name,
                                  detail=detail)

                retry = 0
                while True:
                    self.bus.emit("task_started", task_id=task.id, title=task.title,
                                  agent=agent_name, animal=self.router.animal(agent_name),
                                  attempt=task.attempts)
                    result = await executor.run(task, context, on_event=_on_event)
                    self.total_cost += result.cost_usd
                    self.total_credits += result.cost_credits
                    task.cost_usd += result.cost_usd
                    task.cost_credits += result.cost_credits
                    if self.router.is_paid_api(agent_name):
                        self.paid_cost += result.cost_usd
                    if result.ok:
                        self._persist_turn(task, agent_name, executor, result)
                        final_result = result
                        if result.session_id and isinstance(executor, ClaudeCodeExecutor):
                            probe = Blackboard(self.board.path, on_write=None)
                            if probe.get(f"tasks/{task.id}/inbox/0") is not None:
                                final_result = await self._await_input_loop(
                                    task, agent_name, executor, result.session_id,
                                    0, result)
                        await self._finalize_done(task, agent_name, final_result, before)
                        return

                    self.bus.emit("agent_failed", task_id=task.id, agent=agent_name,
                                  error=result.error[:300], error_kind=result.error_kind,
                                  cost_usd=round(result.cost_usd, 4),
                                  cost_credits=round(result.cost_credits, 4))

                    if result.error_kind == "unavailable":
                        self.router.open_circuit(agent_name)
                        self.bus.emit("agent_circuit_open", agent=agent_name,
                                      task_id=task.id, reason=result.error[:200])
                        break  # move to next agent in the chain
                    if result.error_kind == "transient" and retry < TRANSIENT_MAX_RETRIES:
                        retry += 1
                        await asyncio.sleep(TRANSIENT_BACKOFF_BASE * (2 ** (retry - 1)))
                        continue  # same agent, same attempt slot
                    break  # task_error, or transient retries exhausted

            task.status = TaskStatus.FAILED
            if not chain and task.pinned_agent:
                # 空链且有钉选 = 一个 agent 都没试过。报「all agents
                # failed」是撒谎，用户会去翻那个 agent 的调用日志而那里
                # 什么都没有。钉选是 UI 让他选的，失败原因得说清楚。
                task.error = task.error or (
                    f"pinned agent '{task.pinned_agent}' is unavailable "
                    f"(circuit-open or not in the matrix); pinning is "
                    f"exclusive, so no fallback was attempted")
            else:
                task.error = task.error or "all agents in chain failed"
            self.bus.emit("task_failed", task_id=task.id, error=task.error)
        finally:
            self.sem.release()

    # ---------- main loop ----------
    async def run(self) -> Plan:
        if self.preflight:
            await self._run_preflight()

        # seed the board with the plan (control plane -> data plane)
        # run/workdir lets serve.py resolve tasks/{id}/files back to real
        # paths without having to guess that its own cwd is the workdir
        self.board.put("run/workdir", os.path.realpath(self.workdir),
                       author="engine", kind="fact")
        self.board.put("goal/statement", self.plan.goal, author="system", kind="goal")
        if self.constraints:
            self.board.put("goal/constraints", self.constraints,
                           author="system", kind="goal")
        for t in self.plan.tasks.values():
            self.board.put(f"tasks/{t.id}/spec",
                           {"title": t.title, "type": t.type.value,
                            "prompt": t.prompt, "depends_on": t.depends_on,
                            "difficulty": t.difficulty, "specialty": t.specialty,
                            "pinned_agent": t.pinned_agent},
                           author="system", kind="spec")

        restored = []
        if self.resume:
            for t in self.plan.tasks.values():
                out = self.board.get(f"tasks/{t.id}/output")
                # STALE tasks are deliberately NOT restored -- they stay
                # PENDING so the normal DAG loop below re-executes them
                # against the upstream task's now-updated output. This is
                # forced rerun's entire mechanism: nothing else needs to
                # change (see Plan.from_blackboard, mark_stale_dependents,
                # and main.py's --resume-without-goal/--plan path).
                stale = self.board.get(f"tasks/{t.id}/status") == "stale"
                if out is not None and not stale:
                    t.status = TaskStatus.DONE
                    t.result = out
                    restored.append(t.id)

        tasks_payload = []
        for t in self.plan.tasks.values():
            d = t.to_dict()
            # 路由「预计」派给谁。降级链可能改变实际执行者，所以前端
            # 必须标注「预计」而不是当成事实（spec §4.7）。
            try:
                chain = self.router.chain_for(t)
            except ValueError:
                chain = []      # 该任务类型没有路由 -- 照实留空，不猜
            d["intended_agent"] = chain[0][0] if chain else ""
            tasks_payload.append(d)
        self.bus.emit("plan_created", goal=self.plan.goal,
                      tasks=tasks_payload, restored=restored)

        if self.gate_mode == "ask" or self.assign_mode == "manual":
            # 「或」是有意的：手动分配模型强制开门，哪怕权限设成 auto。
            # 「我要自己选模型」和「别停下来问我」逻辑冲突，前者赢（spec §3.9）
            reply = await self._await_gate("plan", {
                "kind": "plan", "available_agents": self._available_agents()})
            if str(reply.get("decision") or "") != "approve":
                self.bus.emit("run_cancelled", reason="plan gate rejected")
                self._skip_all_pending()
            else:
                for tid, agent in (reply.get("overrides") or {}).items():
                    if tid in self.plan.tasks:
                        self.plan.tasks[tid].pinned_agent = str(agent)

        running: set[asyncio.Task] = set()
        while not self.plan.finished():
            for task in self.plan.ready_tasks():
                task.status = TaskStatus.READY
                self.bus.emit("task_ready", task_id=task.id, title=task.title)
                h = asyncio.create_task(self._run_task(task))
                running.add(h)
                h.add_done_callback(running.discard)
            if running:
                await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            elif not self.plan.finished():
                break
            await asyncio.sleep(0)

        self.bus.emit("run_finished",
                      total_cost_usd=round(self.total_cost, 4),
                      paid_api_cost_usd=round(self.paid_cost, 4),
                      total_credits=round(self.total_credits, 4),
                      statuses={t.id: t.status.value for t in self.plan.tasks.values()})
        return self.plan
