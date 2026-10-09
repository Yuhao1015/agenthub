"""Agent Hub — Phase 1 orchestration engine.

Usage:
  # dry run with mock agents (no keys needed, tests the whole pipeline):
  python main.py --plan example_plan.json --mock

  # real run from a pre-written plan:
  python main.py --plan example_plan.json

  # real run with AI planning (Claude Code decomposes the goal first):
  python main.py --goal "Build a landing page for my pixel-art agent hub"

Events stream to events.jsonl — that file is the contract the pixel
frontend will consume later.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from hub import run_changes
from hub.blackboard import Blackboard
from hub.engine import Engine
from hub.models import EventBus, Plan
from hub.api_budget import PaidLedger, board_budget_path
from hub.planner import make_plan
from hub.router import Router


def _make_output_crashproof() -> None:
    """Make console output resilient to characters the terminal encoding
    can't represent. Windows consoles use a legacy code page (e.g. GBK)
    that can't encode an emoji an AI might return; without this the final
    RESULTS print would raise UnicodeEncodeError and exit 1 even though the
    DAG, blackboard and events already finished cleanly. Replace such
    characters instead of crashing the whole run on the last line."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


async def amain() -> int:
    _make_output_crashproof()
    ap = argparse.ArgumentParser()
    ap.add_argument("--goal", help="natural-language goal (AI planner decomposes it)")
    ap.add_argument("--plan", help="path to a plan JSON file (skips AI planning)")
    ap.add_argument("--matrix", default="hub/capability_matrix.yaml")
    ap.add_argument("--credentials-file", default="", help="local credential store path; never a key")
    ap.add_argument("--paid-budget-source", default="", help="confirmed manager paid-ledger seed path")
    ap.add_argument("--workdir", default=".", help="working dir for Claude Code tasks")
    ap.add_argument("--events", default="events.jsonl")
    ap.add_argument("--board", default="blackboard.jsonl", help="blackboard storage path")
    ap.add_argument("--resume", action="store_true",
                    help="restore DONE tasks from an existing blackboard and skip them "
                         "(STALE tasks are the exception -- they rerun). Combine with "
                         "neither --goal nor --plan to force-rerun a previous run's STALE "
                         "tasks: the DAG is reconstructed from the blackboard itself")
    ap.add_argument("--mock", action="store_true", help="replace all agents with mocks")
    ap.add_argument("--parallel", type=int, default=3)
    ap.add_argument("--no-preflight", action="store_true",
                    help="skip the startup probe of each claude_code agent")
    ap.add_argument("--no-streaming", action="store_true",
                    help="disable live agent_thinking progress events; use the old "
                         "one-shot wait-for-result path for every executor")
    ap.add_argument("--no-summarize", action="store_true",
                    help="disable the Summarizer agent -- never write tasks/{id}/summary; "
                         "downstream tasks always fall back to raw output (capped at 4000 chars)")
    ap.add_argument("--constraints",
                    help="constraints text written to the blackboard as goal/constraints")
    ap.add_argument("--interactive", action="store_true",
                    help="clarify the goal with the manager LLM before planning (needs --goal)")
    ap.add_argument("--gate-mode", default="auto", choices=["auto", "ask"],
                    help="ask = pause before write-capable tasks start")
    ap.add_argument("--assign-mode", default="auto", choices=["auto", "manual"],
                    help="manual = pin an agent per task at the plan gate")
    ap.add_argument("--checkpoint-store", default="",
                    help="Agent Hub's shadow git dir for change checkpoints "
                         "(serve.py passes it; without it the run takes none)")
    ap.add_argument("--run-id", default="", help="run id used in checkpoint ref names")
    ap.add_argument("--checkpoint-exclude", action="append", default=[],
                    help="a path inside the workdir that checkpoints must never "
                         "include (serve.py passes its data dir); repeatable")
    args = ap.parse_args()

    if not args.goal and not args.plan and not args.resume:
        ap.error("need --goal or --plan")

    if args.interactive:
        if not args.goal:
            ap.error("--interactive requires --goal")
        if args.plan or args.constraints or args.mock:
            ap.error("--interactive cannot be combined with --plan/--constraints/--mock")

    os.makedirs(args.workdir, exist_ok=True)
    router = Router(args.matrix, workdir=args.workdir, mock_all=args.mock,
                    stream=not args.no_streaming)
    if args.credentials_file:
        from hub.connection_store import ConnectionStore
        router.bind_api_store(ConnectionStore(
            os.path.join(os.path.dirname(os.path.abspath(args.matrix)), 'config.yaml'),
            args.credentials_file))
    bus = EventBus(args.events)

    goal = args.goal
    constraints = args.constraints or ""
    if args.interactive:
        from hub.manager import run_clarification_loop
        # Clarification can spend money before the user confirms a new run.
        # Keep its own durable scope without touching the previous blackboard.
        manager_budget_path = args.board + '.manager-api-budget.json'
        manager_ledger = PaidLedger(router.budget.get('max_usd_per_run', 2.0), path=manager_budget_path)
        manager_ledger.persist()
        router.bind_paid_ledger(manager_ledger)

        async def ask_user(question: str) -> str:
            print(f"\n[经理] {question}")
            return await asyncio.to_thread(input, "> ")

        try:
            goal, constraints, mgr_cost = await run_clarification_loop(
                ask_user, router, args.workdir, goal)
        except RuntimeError:
            print("经理暂时不可用，改用 --goal 直接跑或用 --constraints 手动给约束",
                  file=sys.stderr)
            return 1
        print(f"(需求澄清完成，经理对话花费 ${mgr_cost:.4f})")

    # fresh run -> fresh board. Deliberately placed AFTER the --interactive
    # clarification loop: resetting it earlier would destroy a previous
    # run's resumable blackboard even if the user Ctrl+C's mid-dialogue,
    # before any new work has actually been confirmed.
    if not args.resume and os.path.exists(args.board):
        os.remove(args.board)
    board = Blackboard(args.board,
                       on_write=lambda e: bus.emit("blackboard_write", **e))
    seed = router.paid_ledger.snapshot()
    limit = router.budget.get('max_usd_per_run', 2.0)
    if args.paid_budget_source and not args.resume:
        seed = PaidLedger(limit, path=args.paid_budget_source).snapshot()
    ledger = PaidLedger(limit, seed['spent_usd'], seed['unknown'],
                        path=board_budget_path(args.board), reset=not args.resume)
    ledger.persist()
    router.bind_paid_ledger(ledger)
    if args.interactive and os.path.exists(manager_budget_path):
        os.unlink(manager_budget_path)  # Transferred only after the run copy is durable.
    if args.resume and not args.goal and not args.plan and not os.path.exists(args.board):
        # 必须在下面拍基线之前查：基线要往黑板写段记录，一写文件就存在了
        print("no existing blackboard to resume from -- need --goal or "
              "--plan for a fresh run", file=sys.stderr)
        return 1
    # 基线在调 planner、读计划之前（change-safety-net.md §3.2）：planning 的
    # 降级链里有带 Bash 的 cc-sonnet 和全权的 codex，拆解任务时也可能改文件
    seg, ckpt = run_changes.begin(board, args.checkpoint_store, args.workdir, args.run_id,
                                  kind="rerun" if args.resume else "run", author="engine",
                                  exclude=tuple(args.checkpoint_exclude))

    if args.plan:
        with open(args.plan, encoding="utf-8") as f:
            plan = Plan.from_json(json.load(f))
    elif args.goal:
        print(f"Planning goal: {goal}")
        try:
            plan = await make_plan(goal, router, constraints=constraints)
        except (RuntimeError, ValueError) as e:
            # goal 模式最常见的失败。原来只有一段 traceback 进 serve 的控制台，
            # 前端只看得到「exit code 1」（bug B11）——原因要进事件流
            run_changes.end(board, seg, author="engine")
            paid = router.paid_ledger.snapshot()
            bus.emit('api_cost_updated', paid_api_cost_usd=paid['spent_usd'],
                     total_cost_usd=paid['spent_usd'], cost_complete=not paid['unknown'])
            bus.emit("run_crashed", error=f"拆解任务失败：{e}"[:500],
                     total_cost_usd=paid['spent_usd'], cost_complete=not paid['unknown'])
            bus.close()
            return 1
    else:
        # --resume with neither --goal nor --plan: force-rerun a previous
        # run's STALE tasks. Goal-driven runs never persist their Planner-
        # generated plan to a reusable file, so the blackboard's own
        # tasks/*/spec entries (written by every run, see hub/engine.py)
        # are the only source of truth left to reconstruct the DAG from.
        # (That the board exists was checked above, before the baseline.)
        plan = Plan.from_blackboard(board)

    engine = Engine(plan, router, bus, board, max_parallel=args.parallel, resume=args.resume,
                    preflight=not args.no_preflight, constraints=constraints,
                    summarize_enabled=not args.no_summarize, workdir=args.workdir,
                    gate_mode=args.gate_mode, assign_mode=args.assign_mode,
                    checkpoint_seg=seg, checkpoint_status=ckpt)
    plan = await engine.run()
    bus.close()

    print("\n=== RESULTS ===")
    for t in plan.tasks.values():
        print(f"[{t.status.value:8}] {t.id} {t.title} (agent={t.agent}, ${t.cost_usd:.4f})")
        if t.result:
            print("  " + t.result[:300].replace("\n", "\n  "))
    failed = any(t.status.value == "failed" for t in plan.tasks.values())
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
