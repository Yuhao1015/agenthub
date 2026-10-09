"""引擎和 main.py 接上检查点（docs/decisions/change-safety-net.md §3.2-§3.4）。

- 会写盘的任务在写锁里拍前后检查点，task_completed 带 changes；只读任务不拍。
- plan_created 带 checkpoints；运行的结束检查点在 run_finished 之前。
- 不传检查点参数时事件跟以前一模一样。
- main.py：基线在 planner 之前；planner 失败也收段；--resume 找不到黑板照旧
  报错，而且不留下黑板文件。

真 git（没装就跳过）、假执行器、--mock。零 LLM 调用。

另外几个针对控制者裁定和审阅发现的测试（见文件底部）：
- R-T8a：写盘任务拿到写锁之后被取消（asyncio.CancelledError），锁都必须放掉，
  否则这次运行后面所有写盘任务都会卡死等一把永远不会释放的锁。拿到锁之后有
  两个会让出事件循环的窗口，各有一个测试：
  - test_cancelling_a_writer_mid_before_checkpoint_...：取消落在 before 检查点
    的 await 上 -- 钉住 take() 里「先 held = True、再 await 检查点」的顺序，
    以及 (c)「before 没拍完就不补拍 after」；
  - test_cancelling_a_writer_waiting_for_the_slot_...：before 检查点已经拍完，
    卡在等并发名额上时被取消（原 R-T8a 场景）。
- R-T8b：从只读 agent 降级到会写的那条路径里，进循环前拍的 before 快照可能
  已经过期（等锁的这段时间里，另一个写盘任务可能已经改了工作目录）；拿到锁、
  拿回名额之后必须重拍 before，否则这个任务的交付物列表会把别人的改动也算进来。
- 审阅 round 1/2：任务门被取消时 _skip_all_pending() 只改 task.status，被标成
  SKIPPED 的写盘任务的协程可能还活着。两道防线各有一个测试，任何一个单独被
  撤掉都会红：
  - test_skipped_writer_queued_on_the_lock_...：(a) 排在写锁上、已被标成 SKIPPED
    的任务从不拍检查点、不跑 agent；
  - test_run_waits_for_an_in_flight_checkpoint_...：(b) 任务在 before 快照还在
    线程里跑时被标成 SKIPPED、主循环随即退出 -- run() 必须先取消这个协程、再
    等到那个线程真的跑完，才能拍收尾检查点。

Run: python tests/test_engine_checkpoints.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hub import checkpoints, run_changes
from hub.blackboard import Blackboard
from hub.engine import Engine
from hub.executors import ExecResult
from hub.models import EventBus, Plan, Task, TaskStatus, TaskType

RID = "abcd1234"


def needs_git(fn):
    def wrapper():
        if shutil.which("git") is None:
            print("  (skipped: no git on PATH)")
            return
        fn()
    return wrapper


class _FileEx:
    """Writes <task id>.txt into the work folder when it runs; workdir=None
    touches nothing."""

    def __init__(self, workdir=None, ok=True):
        self.workdir, self.ok = workdir, ok

    async def run(self, task, context, on_event=None):
        if self.workdir:
            with open(os.path.join(self.workdir, task.id + ".txt"), "w", encoding="utf-8") as f:
                f.write(task.id)
        if self.ok:
            return ExecResult(ok=True, output="ok")
        return ExecResult(ok=False, error="bad output", error_kind="task_error")


class _DelayedWrite:
    """R-T8a/R-T8b 专用：写一个固定文件名（不是 task.id 派生的），可选先 delay
    一下 -- 这样两个任务谁先写、谁后写可以在测试里精确排出顺序。"""

    def __init__(self, workdir, filename, delay=0.0, ok=True):
        self.workdir, self.filename, self.delay, self.ok = workdir, filename, delay, ok

    async def run(self, task, context, on_event=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        if not self.ok:
            return ExecResult(ok=False, error="bad output", error_kind="task_error")
        with open(os.path.join(self.workdir, self.filename), "w", encoding="utf-8") as f:
            f.write(self.filename)
        return ExecResult(ok=True, output="ok")


class _Router:
    """agents: name -> (kind, allowed_tools, executor); chains: task id -> [names]."""

    def __init__(self, agents, chains):
        self._agents, self._chains = agents, chains
        self.budget = {"max_usd_per_run": 1e9, "max_attempts_per_task": 2}
        self.tiers = {}

    def chain_for(self, task):
        return [(n, self._agents[n][2]) for n in self._chains[task.id]]

    def executor_for(self, n):
        return self._agents[n][2]

    def kind(self, n):
        return self._agents[n][0]

    def allowed_tools(self, n):
        return self._agents[n][1]

    def animal(self, n):
        return "cat"

    def is_paid_api(self, n):
        return False

    def open_circuit(self, n):
        pass

    def claude_code_agents(self):
        return []

    def codebuddy_agents(self):
        return []

    def all_agents(self):
        return list(self._agents)

    def circuit_open(self):
        return set()

    def specialty(self, n):
        return None

    def tier_of(self, n):
        return None

    def blurb(self, n):
        return ""


def _events(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def _engine_run(tmp, wd, agents, chains, with_checkpoints=True):
    os.makedirs(wd, exist_ok=True)
    board = Blackboard(os.path.join(tmp, "blackboard.jsonl"), on_write=None)
    bus = EventBus(os.path.join(tmp, "events.jsonl"))
    seg = status = None
    if with_checkpoints:
        store = checkpoints.store_path(os.path.join(tmp, "rt"), wd)
        seg, status = run_changes.begin(board, store, wd, RID, "run", "engine")
    plan = Plan(goal="g", tasks={tid: Task(id=tid, title=tid, type=TaskType.CODING, prompt="p")
                                 for tid in chains})
    eng = Engine(plan, _Router(agents, chains), bus, board, preflight=False,
                 summarize_enabled=False, workdir=wd, checkpoint_seg=seg,
                 checkpoint_status=status)
    plan = asyncio.run(eng.run())
    bus.close()
    return plan, board, _events(os.path.join(tmp, "events.jsonl"))


@needs_git
def test_writer_task_gets_checkpoints_and_a_changes_field():
    tmp = tempfile.mkdtemp(prefix="ah_eck_")
    wd = os.path.join(tmp, "wd")
    try:
        agents = {"w": ("claude_code", "Read,Write", _FileEx(wd)),
                  "r": ("claude_code", "Read", _FileEx())}
        plan, board, events = _engine_run(tmp, wd, agents, {"t1": ["w"], "t2": ["r"]})
        created = [e for e in events if e["type"] == "plan_created"][0]
        assert created["checkpoints"] == {"on": True, "reason": ""}, created
        done = {e["task_id"]: e for e in events if e["type"] == "task_completed"}
        assert done["t1"]["changes"] == {"added": 1, "modified": 0, "deleted": 0}, done["t1"]
        assert "changes" not in done["t2"], "read-only tasks take no checkpoints"
        assert board.get("tasks/t2/checkpoints/1") is None
        t1 = board.get("tasks/t1/checkpoints/1")
        assert t1["before"] and t1["after"], t1
        seg = board.get("run/checkpoints/seg/1")
        finished = [e for e in events if e["type"] == "run_finished"][0]
        assert seg["after"] and seg["ended"] <= finished["ts"], (seg, finished["ts"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_failed_writer_still_gets_its_after_checkpoint():
    tmp = tempfile.mkdtemp(prefix="ah_eck_")
    wd = os.path.join(tmp, "wd")
    try:
        agents = {"w": ("claude_code", "Read,Write", _FileEx(wd, ok=False))}
        plan, board, events = _engine_run(tmp, wd, agents, {"t1": ["w"]})
        assert plan.tasks["t1"].status == TaskStatus.FAILED
        t1 = board.get("tasks/t1/checkpoints/1")
        assert t1["before"] and t1["after"], t1
        groups = run_changes.summary(board, os.path.join(tmp, "rt"))["segments"][0]["tasks"]
        assert groups[0]["id"] == "t1" and groups[0]["paths"] == ["t1.txt"], groups
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_fallback_to_a_writer_is_checkpointed():
    tmp = tempfile.mkdtemp(prefix="ah_eck_")
    wd = os.path.join(tmp, "wd")
    try:
        agents = {"r": ("claude_code", "Read", _FileEx(ok=False)),
                  "w": ("codex", "", _FileEx(wd))}
        plan, board, events = _engine_run(tmp, wd, agents, {"t1": ["r", "w"]})
        done = [e for e in events if e["type"] == "task_completed"][0]
        assert done["agent"] == "w" and done["changes"]["added"] == 1, done
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_without_checkpoints_the_events_are_unchanged():
    """No git needed: nothing is snapshotted at all."""
    tmp = tempfile.mkdtemp(prefix="ah_eck_")
    wd = os.path.join(tmp, "wd")
    try:
        agents = {"w": ("claude_code", "Read,Write", _FileEx(wd))}
        plan, board, events = _engine_run(tmp, wd, agents, {"t1": ["w"]},
                                          with_checkpoints=False)
        created = [e for e in events if e["type"] == "plan_created"][0]
        done = [e for e in events if e["type"] == "task_completed"][0]
        assert "checkpoints" not in created and "changes" not in done, (created, done)
        assert board.query("run/checkpoints/") == [] and board.get("tasks/t1/checkpoints/1") is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _main(tmp, *args):
    return subprocess.run(
        [sys.executable, os.path.join(ROOT, "main.py"), *args,
         f"--events={os.path.join(tmp, 'events.jsonl')}",
         f"--board={os.path.join(tmp, 'board.jsonl')}",
         f"--workdir={os.path.join(tmp, 'wd')}", "--no-preflight"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180)


def _store(tmp):
    return checkpoints.store_path(os.path.join(tmp, "rt"), os.path.join(tmp, "wd"))


@needs_git
def test_main_takes_the_baseline_before_planning():
    """--goal + --mock is a real, zero-cost planning failure (the mock planner
    does not answer with a JSON plan). The baseline must already exist, and the
    segment must be closed before run_crashed."""
    tmp = tempfile.mkdtemp(prefix="ah_main_ck_")
    try:
        proc = _main(tmp, "--goal=做一个落地页", "--mock",
                     f"--checkpoint-store={_store(tmp)}", f"--run-id={RID}")
        assert proc.returncode != 0, proc.stdout[-500:]
        seg = Blackboard(os.path.join(tmp, "board.jsonl"),
                         on_write=None).get("run/checkpoints/seg/1")
        assert seg and seg["kind"] == "run" and seg["before"] and seg["after"], seg
        crashed = [e for e in _events(os.path.join(tmp, "events.jsonl"))
                   if e["type"] == "run_crashed"]
        assert crashed and seg["ended"] <= crashed[0]["ts"], (seg, crashed)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_main_mock_run_checkpoints_its_writer_tasks():
    tmp = tempfile.mkdtemp(prefix="ah_main_ck_")
    try:
        proc = _main(tmp, "--plan=example_plan.json", "--mock",
                     f"--checkpoint-store={_store(tmp)}", f"--run-id={RID}")
        assert proc.returncode == 0, proc.stderr[-800:]
        board = Blackboard(os.path.join(tmp, "board.jsonl"), on_write=None)
        seg = board.get("run/checkpoints/seg/1")
        assert seg["before"] and seg["after"] and seg["kind"] == "run", seg
        created = [e for e in _events(os.path.join(tmp, "events.jsonl"))
                   if e["type"] == "plan_created"][0]
        assert created["checkpoints"] == {"on": True, "reason": ""}, created
        # "code" goes to cc-sonnet, declared with Write/Bash: --mock swaps the
        # executor, the task still counts as a writer
        code = board.get("tasks/code/checkpoints/1")
        assert code["before"] and code["after"], code
        assert board.get("tasks/arch/checkpoints/1") is None, "arch goes to read-only cc-opus"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_main_without_a_store_says_not_enabled():
    tmp = tempfile.mkdtemp(prefix="ah_main_ck_")
    try:
        proc = _main(tmp, "--plan=example_plan.json", "--mock")
        assert proc.returncode == 0, proc.stderr[-800:]
        created = [e for e in _events(os.path.join(tmp, "events.jsonl"))
                   if e["type"] == "plan_created"][0]
        assert created["checkpoints"] == {"on": False, "reason": "未启用"}, created
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_resume_without_a_board_still_refuses_and_leaves_no_board():
    tmp = tempfile.mkdtemp(prefix="ah_main_ck_")
    try:
        proc = _main(tmp, "--resume", f"--checkpoint-store={_store(tmp)}", f"--run-id={RID}")
        assert proc.returncode == 1 and "no existing blackboard" in proc.stderr, proc.stderr
        assert not os.path.exists(os.path.join(tmp, "board.jsonl")), \
            "the baseline must not create the very board the resume check looks for"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_cancelling_a_writer_mid_before_checkpoint_frees_the_write_lock():
    """round-1 (d)：_WriteHold.take() 必须先 self.held = True，再 await 前置
    检查点 -- 顺序反过来的话，取消发生在检查点 await 期间时 held 还是 False，
    hold.release() 会以为「没拿锁」直接跳过，写锁就永远锁死了。

    这条顺序只有检查点真的会 await 才测得出来（checkpoint_seg=None 时
    _task_checkpoint 直接 return，take() 整个不让出事件循环）：用真的检查点库，
    轮询 eng._checkpoint_threads 非空，确认这一刻协程确实卡在 before 快照的
    后台线程上，此时取消，写锁必须被放掉。

    顺带钉住 (c)：before 没拍完的任务，release() 不会去补拍 after，黑板上不会
    留下 tasks/t1/checkpoints/1（否则就是一条只有 after、没有 before 的记录）。

    这个测试里取消一定落在 before 检查点上，不涉及并发名额；「before 已经拍完、
    卡在等名额时被取消」是另一个窗口，见下面
    test_cancelling_a_writer_waiting_for_the_slot_frees_the_write_lock。
    """
    tmp = tempfile.mkdtemp(prefix="ah_eck_")
    wd = os.path.join(tmp, "wd")
    os.makedirs(wd, exist_ok=True)
    try:
        async def scenario():
            board = Blackboard(os.path.join(tmp, "blackboard.jsonl"), on_write=None)
            bus = EventBus(os.path.join(tmp, "events.jsonl"))
            store = checkpoints.store_path(os.path.join(tmp, "rt"), wd)
            seg, status = run_changes.begin(board, store, wd, RID, "run", "engine")
            assert seg is not None, status
            agents = {"w": ("claude_code", "Read,Write", _FileEx(wd))}
            chains = {"t1": ["w"]}
            plan = Plan(goal="g", tasks={"t1": Task(id="t1", title="t1",
                                                     type=TaskType.CODING, prompt="p")})
            eng = Engine(plan, _Router(agents, chains), bus, board, preflight=False,
                         summarize_enabled=False, workdir=wd,
                         checkpoint_seg=seg, checkpoint_status=status)
            task = asyncio.create_task(eng._run_task_body(plan.tasks["t1"]))
            for _ in range(200):
                await asyncio.sleep(0)
                if eng._checkpoint_threads:
                    break
            assert eng._checkpoint_threads, "should be mid before-checkpoint by now"
            assert eng.write_lock.locked(), \
                "the writer must already hold the write lock while its before snapshot is in flight"
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            assert not eng.write_lock.locked(), \
                "cancelling mid before-checkpoint must not leak the write lock"
            assert board.get("tasks/t1/checkpoints/1") is None, \
                "no after snapshot (and no record at all) for a task whose before never finished"
            if eng._checkpoint_threads:
                # 被取消的协程没等到的后台线程还在跑；测试自己收尾前等它结束，
                # 不然 tmp 目录被删的时候它可能还在读写里面的文件（Windows 上
                # 会导致 rmtree 间歇性失败）
                await asyncio.gather(*eng._checkpoint_threads, return_exceptions=True)
            bus.close()

        asyncio.run(scenario())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_cancelling_a_writer_waiting_for_the_slot_frees_the_write_lock():
    """R-T8a（原场景，round 2 恢复成独立测试）：写盘任务的 before 检查点已经
    拍完，拿着写锁卡在等并发名额上 -- 名额被提前占满，正是裁定里说的「a
    pre-acquired semaphore」-- 此时被取消，写锁必须被放掉。这个窗口在
    _run_task_body 里「拿锁 + 拿名额」那一段的 try/except BaseException 保护
    范围之内；如果保护范围缩到只包 take()，等名额时被取消就会把锁永远锁死，
    上面那个 mid-before-checkpoint 测试测不出来，这个测试才测得出来。

    before 已经拍完 (_before_done 为真)，所以取消后 except BaseException 里的
    release() 走的是完整路径：先补拍 after 检查点（又是一次真的 await）、再放锁。
    这个测试因此顺带覆盖了「在异常处理里 await 一次检查点」这条路径：断言锁空了、
    after 也记下来了（等 task 完全结束之后看的终态，看不到两者的先后）。
    """
    tmp = tempfile.mkdtemp(prefix="ah_eck_")
    wd = os.path.join(tmp, "wd")
    os.makedirs(wd, exist_ok=True)
    try:
        async def scenario():
            board = Blackboard(os.path.join(tmp, "blackboard.jsonl"), on_write=None)
            bus = EventBus(os.path.join(tmp, "events.jsonl"))
            store = checkpoints.store_path(os.path.join(tmp, "rt"), wd)
            seg, status = run_changes.begin(board, store, wd, RID, "run", "engine")
            assert seg is not None, status
            agents = {"w": ("claude_code", "Read,Write", _FileEx(wd))}
            chains = {"t1": ["w"]}
            plan = Plan(goal="g", tasks={"t1": Task(id="t1", title="t1",
                                                     type=TaskType.CODING, prompt="p")})
            eng = Engine(plan, _Router(agents, chains), bus, board, max_parallel=1,
                         preflight=False, summarize_enabled=False, workdir=wd,
                         checkpoint_seg=seg, checkpoint_status=status)
            await eng.sem.acquire()      # 占满唯一的并发名额
            task = asyncio.create_task(eng._run_task_body(plan.tasks["t1"]))
            key = "tasks/t1/checkpoints/1"
            # before 检查点拍完的那一步（record_task 写黑板）之后，协程紧接着就
            # 在同一步里卡到 sem.acquire() 上：别的协程看到黑板上有 before 的时候，
            # 任务已经是「拿着锁、等名额」了，不用再靠 sleep 掐时间
            for _ in range(2000):
                await asyncio.sleep(0.005)
                if (board.get(key) or {}).get("before"):
                    break
            assert (board.get(key) or {}).get("before"), "the before checkpoint should have completed"
            assert eng.write_lock.locked(), "the writer holds the write lock while it waits for the slot"
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            assert not eng.write_lock.locked(), \
                "cancelling while waiting for the concurrency slot must not leak the write lock"
            rec = board.get(key)
            assert rec["before"] and rec["after"], rec
            if eng._checkpoint_threads:
                await asyncio.gather(*eng._checkpoint_threads, return_exceptions=True)
            bus.close()

        asyncio.run(scenario())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_fallback_writer_gets_a_fresh_before_snapshot():
    """R-T8b：t2 先在只读 agent 上试（会失败），落到会写的 agent 前，
    在循环外拍的那个 before 是「t2 刚开始跑、还没等锁」那一刻的快照。等锁
    期间 t1（另一个写盘任务）可能已经跑完并改了工作目录 -- t2 真正落到写盘
    agent、拿到锁和名额之后，必须重拍 before，不然 t1 的改动会被记到 t2 头上。
    不用真 git：这里测的是 hub/artifacts.py 的快照 diff（task_completed.files），
    不是检查点。
    """
    tmp = tempfile.mkdtemp(prefix="ah_eck_")
    wd = os.path.join(tmp, "wd")
    try:
        agents = {"w1": ("claude_code", "Read,Write", _DelayedWrite(wd, "a.txt", delay=0.15)),
                  "bad-reader": ("claude_code", "Read", _FileEx(ok=False)),
                  "w2": ("codex", "", _DelayedWrite(wd, "b.txt"))}
        chains = {"t1": ["w1"], "t2": ["bad-reader", "w2"]}
        plan, board, events = _engine_run(tmp, wd, agents, chains, with_checkpoints=False)
        assert plan.tasks["t1"].status == TaskStatus.DONE
        assert plan.tasks["t2"].status == TaskStatus.DONE and plan.tasks["t2"].agent == "w2"
        done = {e["task_id"]: e for e in events if e["type"] == "task_completed"}
        paths = sorted(f["path"] for f in done["t2"]["files"])
        assert paths == ["b.txt"], done["t2"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_skipped_writer_queued_on_the_lock_never_touches_git():
    """审阅 round 1 的 Important 发现：任务门被取消后，已经被 _skip_all_pending()
    标成 SKIPPED、但协程还排在写锁上的写盘任务，如果这时候还能拍检查点，会跟
    run() 收尾时的结束快照同时对同一个 git 库跑 git add。断言这次运行的五个
    不变量：seg 1 的结束检查点必须拍成、不带 index.lock 冲突的痕迹；
    被跳过的 t2 的 agent 必须从未真正跑过；t2 不留下 tasks/t2/checkpoints/*；
    run_finished 之后黑板上不能再多出任何东西；t2 从头到尾没拍过任何检查点
    （round 2 补上：spy 记下 _task_checkpoint 的调用，再去检查点库里确认没有
    t2 的 ref）。

    最后一条是 (a) 的单独钉子：只撤掉 (a)（take() 和 _run_task_body 里的两处
    SKIPPED 判断）、留着 (b) 的时候，t2 醒来照样会起 before 快照，但随后被
    run() 取消、线程也被等完，seg 收得了尾，前面四个不变量都还成立 -- 只有
    「t2 有没有拍过检查点」这一条看得出 (a) 在不在。

    用跟审阅探针一样的手法，不用真的走门也不用掐时间点：t1 的假执行器真正
    执行到的时候（也就是 t1 的写锁 + before 检查点都已经拿到/拍完之后）直接
    调 eng._skip_all_pending()。此时 t2 必然已经卡在写锁的 acquire() 上 --
    t1 在自己的 before 检查点上会先让出一次事件循环（真检查点，真的要起
    线程跑 git），t2 才有机会被调度到并卡在等锁那一步；t1 的执行器要等到
    before 检查点拍完、写锁也真的锁上之后才会被调用，顺序由代码结构保证，
    不用睡眠等时间。

    workdir 里预先放几百个文件，让 git add -A 有实际工作量：审阅探针实测
    300 个文件时 5/6 次复现、1500 个文件时 1/3 次复现，不是每次都中 --
    这里循环跑几次，在报告里如实记录修复前观察到的复现情况。
    """
    tmp_root = tempfile.mkdtemp(prefix="ah_eck_skip_")
    wd = os.path.join(tmp_root, "wd")
    os.makedirs(wd, exist_ok=True)
    for i in range(400):
        with open(os.path.join(wd, f"f{i}.txt"), "w", encoding="utf-8") as f:
            f.write(str(i))
    try:
        class _Skipper:
            """t1 的假执行器：真正跑到（写锁 + before 检查点都已经拿到/拍完）
            时，把 t2 标成 SKIPPED -- 模拟"另一个任务门被取消"的效果。"""
            engine = None

            async def run(self, task, context, on_event=None):
                self.engine._skip_all_pending()
                return ExecResult(ok=True, output="ok")

        class _T2Agent:
            def __init__(self):
                self.ran = False

            async def run(self, task, context, on_event=None):
                self.ran = True
                return ExecResult(ok=True, output="ok")

        for attempt in range(4):
            iter_dir = os.path.join(tmp_root, f"iter{attempt}")
            os.makedirs(iter_dir, exist_ok=True)
            board = Blackboard(os.path.join(iter_dir, "blackboard.jsonl"), on_write=None)
            bus = EventBus(os.path.join(iter_dir, "events.jsonl"))
            store = checkpoints.store_path(os.path.join(iter_dir, "rt"), wd)
            seg, status = run_changes.begin(board, store, wd, RID, "run", "engine")
            assert seg is not None, status   # 基线必须真的拍成了，不然这个测试没意义

            skipper = _Skipper()
            t2_agent = _T2Agent()
            agents = {"w1": ("claude_code", "Read,Write", skipper),
                      "w2": ("claude_code", "Read,Write", t2_agent)}
            chains = {"t1": ["w1"], "t2": ["w2"]}
            plan = Plan(goal="g", tasks={tid: Task(id=tid, title=tid, type=TaskType.CODING,
                                                    prompt="p") for tid in chains})
            eng = Engine(plan, _Router(agents, chains), bus, board, preflight=False,
                         summarize_enabled=False, workdir=wd, checkpoint_seg=seg,
                         checkpoint_status=status)
            skipper.engine = eng

            calls = []                      # 每一次 _task_checkpoint 调用：(任务 id, when)
            real_checkpoint = eng._task_checkpoint

            async def spy(task, when):
                calls.append((task.id, when))
                return await real_checkpoint(task, when)

            eng._task_checkpoint = spy      # _WriteHold 每次都从引擎实例上取这个方法
            asyncio.run(eng.run())
            bus.close()

            events = _events(os.path.join(iter_dir, "events.jsonl"))
            seg1 = board.get("run/checkpoints/seg/1")
            assert seg1["after"], (attempt, seg1)
            assert "index.lock" not in (seg1.get("note") or ""), (attempt, seg1)
            assert not t2_agent.ran, (attempt, "t2's agent must never have run once skipped")
            assert board.get("tasks/t2/checkpoints/1") is None, attempt
            # (a) 的单独钉子：被跳过的 t2 一次检查点都不拍 -- 调用层看 spy，落盘层
            # 看检查点库里有没有它的 ref；t1 的前后各一次作为对照，证明这两个检查
            # 不是空转
            assert [c for c in calls if c[0] == "t2"] == [], \
                (attempt, "a skipped task must never checkpoint", calls)
            assert calls == [("t1", "before"), ("t1", "after")], (attempt, calls)
            refs = subprocess.run(
                ["git", "--git-dir", store, "for-each-ref", "--format=%(refname)",
                 "refs/agenthub/runs/"], capture_output=True, check=True).stdout.decode().split()

            def task_ref(tid, when):
                return checkpoints.ref(RID, run_changes.task_label(tid, 1, when))

            assert task_ref("t1", "before") in refs and task_ref("t1", "after") in refs, \
                (attempt, refs)
            assert task_ref("t2", "before") not in refs \
                and task_ref("t2", "after") not in refs, (attempt, refs)
            finished = [e for e in events if e["type"] == "run_finished"][0]
            after_board = Blackboard(os.path.join(iter_dir, "blackboard.jsonl"), on_write=None)
            late = [e for e in after_board.query("") if e["ts"] > finished["ts"]]
            assert late == [], (attempt, late)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def test_run_waits_for_an_in_flight_checkpoint_before_closing_the_segment():
    """round 2 单独钉住 (b)：写盘任务的 before 快照还在线程里跑的时候被标成
    SKIPPED，主循环随即因为 plan.finished() 退出 -- run() 必须先取消这个还活着
    的协程、再等到那个线程真的跑完，才能拍收尾检查点。取消协程不会让 git 线程
    停下来；不等它，收尾快照就跟它同时对同一个库跑 git（index.lock 冲突）。

    上面那个 skipped_writer 场景里，(a) 在的时候 t2 醒来当场就走完了，run() 恢复
    之前它已经结束，(b) 根本没机会执行 -- 只撤掉 (b) 那个场景照样绿。所以这里
    单独造 (b) 真正要防的场景，不用真 git：

    - run_changes.task_snapshot 换成假的：对 t2 的 before 阻塞在 threading.Event
      上，并数着此刻在飞的快照线程有几个；它一进来就 call_soon_threadsafe 让
      事件循环把 t2 标成 SKIPPED（t2 此刻正卡在这个线程上，还没走完 before）；
    - t1（只读，不碰写锁）等这个 SKIPPED 落地才返回：t1 一完成，主循环马上看到
      plan.finished() 成立、退出循环 -- 此刻 t2 的协程还活着、快照线程还在飞；
    - 那个 Event 由 Timer 在 t1 返回之后 0.5 秒才放开。t1 返回到 run() 走到
      run_changes.end 之间只有几毫秒的事件循环空转，所以「主循环退出那一刻快照
      必然还在飞」是 Event 钉住的；唯一的时间依赖是这 0.5 秒的富余量 -- 机器
      卡顿超过 0.5 秒时，撤掉 (b) 的代码可能假绿，有 (b) 的代码永远不会假红；
    - run_changes.end 换成包装：记录「被调用那一刻还有几个快照线程在飞」，
      断言是 0。有 (b) 时，run() 先取消 t2 的协程、再 gather 等到线程返回，
      才调 end；没有 (b) 时 end 立刻被调用，此刻线程还被 Event 钉着，数字是 1。

    换掉的两个函数在 finally 里还原。
    """
    tmp = tempfile.mkdtemp(prefix="ah_eck_")
    wd = os.path.join(tmp, "wd")
    os.makedirs(wd, exist_ok=True)
    real_snapshot, real_end = run_changes.task_snapshot, run_changes.end
    lock = threading.Lock()
    state = {"in_flight": 0, "entered": 0, "at_end": None, "other": []}
    release = threading.Event()        # 放开被钉住的那个 before 快照线程
    timer = threading.Timer(0.5, release.set)
    timer.daemon = True
    t2_ran = []
    try:
        async def scenario():
            loop = asyncio.get_running_loop()
            skipped = asyncio.Event()  # 事件循环这一侧：t2 已经被标成 SKIPPED

            class _Reader:
                """t1：等 SKIPPED 落地才返回。它一返回，主循环就会看到
                plan.finished() 并退出；Timer 从这一刻才起算。"""

                async def run(self, task, context, on_event=None):
                    await asyncio.wait_for(skipped.wait(), timeout=10)
                    timer.start()
                    return ExecResult(ok=True, output="ok")

            class _Writer:
                async def run(self, task, context, on_event=None):
                    t2_ran.append(True)
                    return ExecResult(ok=True, output="ok")

            board = Blackboard(os.path.join(tmp, "blackboard.jsonl"), on_write=None)
            bus = EventBus(os.path.join(tmp, "events.jsonl"))
            # 不拍真检查点：手工造一段，快照函数换成假的，收尾的 end 找不到这一段的
            # 黑板记录、什么都不做
            seg = run_changes.Segment(os.path.join(tmp, "unused.git"), wd, RID, 1)
            agents = {"r": ("claude_code", "Read", _Reader()),
                      "w": ("claude_code", "Read,Write", _Writer())}
            chains = {"t1": ["r"], "t2": ["w"]}
            plan = Plan(goal="g", tasks={tid: Task(id=tid, title=tid, type=TaskType.CODING,
                                                    prompt="p") for tid in chains})
            eng = Engine(plan, _Router(agents, chains), bus, board, preflight=False,
                         summarize_enabled=False, workdir=wd, checkpoint_seg=seg,
                         checkpoint_status={"on": True, "reason": ""})

            def apply_skip():          # 在事件循环线程上跑
                eng._skip_all_pending()
                skipped.set()

            def fake_snapshot(seg_, task_id, when):     # 在 to_thread 的线程里跑
                if (task_id, when) != ("t2", "before"):
                    state["other"].append((task_id, when))
                    return {"commit": "", "skipped": [], "error": "unexpected snapshot"}
                with lock:
                    state["in_flight"] += 1
                    state["entered"] += 1
                try:
                    loop.call_soon_threadsafe(apply_skip)
                    release.wait(timeout=5)     # 5 秒只是兜底，防止测试写坏了永远挂住
                finally:
                    with lock:
                        state["in_flight"] -= 1
                return {"commit": "", "skipped": [], "error": ""}

            def spy_end(*args, **kwargs):
                with lock:
                    state["at_end"] = state["in_flight"]
                return real_end(*args, **kwargs)

            run_changes.task_snapshot, run_changes.end = fake_snapshot, spy_end
            await asyncio.wait_for(eng.run(), timeout=30)
            bus.close()
            return plan

        plan = asyncio.run(scenario())
        assert state["entered"] == 1, state          # 场景真的走到了「t2 的 before 快照在飞」
        assert state["other"] == [], state           # 除此之外没有别的快照
        assert plan.tasks["t1"].status == TaskStatus.DONE, plan.tasks["t1"].status
        assert plan.tasks["t2"].status == TaskStatus.SKIPPED, plan.tasks["t2"].status
        assert not t2_ran, "t2's agent must never run"
        assert state["at_end"] is not None, "run_changes.end was never called"
        assert state["at_end"] == 0, (
            f"run_changes.end was called while {state['at_end']} checkpoint snapshot "
            f"thread(s) were still in flight")
    finally:
        run_changes.task_snapshot, run_changes.end = real_snapshot, real_end
        timer.cancel()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll engine/main checkpoint tests passed.")
