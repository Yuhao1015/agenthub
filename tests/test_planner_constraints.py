"""Planner input must retain confirmed constraints; no real models are called."""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main as cli
from hub.executors import ExecResult, MockExecutor
from hub.blackboard import Blackboard
from hub.engine import Engine
from hub.models import EventBus, Plan
from hub.planner import make_plan
from hub.router import Router

ROOT = Path(__file__).resolve().parents[1]
SINGLE = {'goal': '生成验收文件', 'tasks': [{'id': 't1', 'title': '读取、写入并自检',
          'type': 'coding', 'prompt': '读取说明，创建 acceptance.txt 并核对内容。',
          'depends_on': [], 'difficulty': 'easy'}]}
CONSTRAINTS = '只安排一个 coding 任务；禁止额外审查任务；只写 acceptance.txt，内容包含 {确认}。'


def test_cli_constraints_reach_planner_before_single_task_executes():
    original = MockExecutor.run
    prompts = []

    async def run(self, task, context, on_event=None):
        if task.id == '_plan':
            prompts.append(task.prompt)
            return ExecResult(ok=True, output=json.dumps(SINGLE, ensure_ascii=False))
        return await original(self, task, context, on_event)

    with tempfile.TemporaryDirectory() as temp:
        folder = Path(temp)
        argv = ['main.py', '--mock', '--goal', '生成验收文件', '--constraints', CONSTRAINTS,
                '--matrix', str(ROOT / 'hub/capability_matrix.yaml'),
                '--workdir', temp, '--board', str(folder / 'board.jsonl'),
                '--events', str(folder / 'events.jsonl'), '--no-preflight', '--no-summarize']
        with patch.object(sys, 'argv', argv), patch.object(MockExecutor, 'run', run):
            assert asyncio.run(cli.amain()) == 0
        assert len(prompts) == 1
        assert CONSTRAINTS in prompts[0], 'Confirmed constraints were lost before the planning model call'
        rows = [json.loads(line) for line in (folder / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
        assert [row['task_id'] for row in rows if row['type'] == 'task_started'] == ['t1']
        assert [row['task_id'] for row in rows if row['type'] == 'task_completed'] == ['t1']
        assert next(row for row in rows if row['type'] == 'run_finished')['total_cost_usd'] == 0


def test_direct_planner_keeps_goal_and_separate_constraints_verbatim():
    prompts = []

    async def run(self, task, context, on_event=None):
        prompts.append(task.prompt)
        return ExecResult(ok=True, output=json.dumps(SINGLE, ensure_ascii=False))

    router = Router(str(ROOT / 'hub/capability_matrix.yaml'), mock_all=True)
    with patch.object(MockExecutor, 'run', run):
        plan = asyncio.run(make_plan('生成验收文件 {目标}', router, constraints=CONSTRAINTS))
    assert '生成验收文件 {目标}' in prompts[0]
    assert CONSTRAINTS in prompts[0]
    assert list(plan.tasks) == ['t1']
    assert plan.tasks['t1'].depends_on == []


def test_unconstrained_planner_still_accepts_a_multi_task_dag():
    response = {'goal': '两阶段交付', 'tasks': [SINGLE['tasks'][0], {
        'id': 't2', 'title': '独立审查', 'type': 'review', 'prompt': '审查 t1 的输出。',
        'depends_on': ['t1'], 'difficulty': 'medium'}]}

    async def run(self, task, context, on_event=None):
        return ExecResult(ok=True, output=json.dumps(response, ensure_ascii=False))

    router = Router(str(ROOT / 'hub/capability_matrix.yaml'), mock_all=True)
    with patch.object(MockExecutor, 'run', run):
        plan = asyncio.run(make_plan('先生成再独立审查', router))
    assert list(plan.tasks) == ['t1', 't2']
    assert plan.tasks['t2'].depends_on == ['t1']


def test_worker_gets_completed_start_confirmation_but_keeps_later_approval():
    contexts = []
    original = MockExecutor.run
    constraints = '收到方案确认后才开始。发布前必须再次询问我。'

    async def run(self, task, context, on_event=None):
        contexts.append(context)
        return await original(self, task, context, on_event)

    with tempfile.TemporaryDirectory() as temp:
        board = Blackboard(str(Path(temp) / 'board.jsonl'))
        bus = EventBus(str(Path(temp) / 'events.jsonl'))
        router = Router(str(ROOT / 'hub/capability_matrix.yaml'), mock_all=True)
        engine = Engine(Plan.from_json(SINGLE), router, bus, board, workdir=temp,
                        constraints=constraints, preflight=False, summarize_enabled=False)
        try:
            with patch.object(MockExecutor, 'run', run):
                asyncio.run(engine.run())
        finally:
            bus.close()
        assert len(contexts) == 1
        assert constraints in contexts[0]
        assert 'initial goal-start confirmation is already complete' in contexts[0], 'Worker cannot distinguish completed start approval from a future prerequisite'
        assert 'does not waive any separate approval' in contexts[0], 'Goal-start confirmation must not bypass later user approvals'


if __name__ == '__main__':
    selected = sys.argv[1] if len(sys.argv) > 1 else None
    for name, test in list(globals().items()):
        if name.startswith('test_') and (selected is None or selected == name):
            test()
            print(name + ': PASS')
