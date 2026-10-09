import os, sys, asyncio, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hub.router import Router
from hub.blackboard import Blackboard
from hub.executors import ExecResult
from hub.conversation import continue_task_conversation
from hub.engine import Engine
from hub.models import Plan, Task, TaskType, EventBus
from hub.executors import ClaudeCodeExecutor
from unittest.mock import patch
import yaml


def test_automatic_task_freezes_actual_options():
    with tempfile.TemporaryDirectory() as td:
        matrix = os.path.join(td, 'matrix.yaml')
        cfg = {'agents': {'cc': {'kind': 'claude_code', 'model': 'old-model',
                               'reasoning_effort': 'low', 'allowed_tools': 'Read'}},
               'routes': {'coding': ['cc']}, 'budget': {}}
        def write_matrix():
            with open(matrix, 'w', encoding='utf-8') as f:
                yaml.safe_dump(cfg, f)
        write_matrix()
        board = Blackboard(os.path.join(td, 'board.jsonl'))
        bus = EventBus(os.path.join(td, 'events.jsonl'))
        task = Task(id='t', title='automatic', type=TaskType.CODING, prompt='p')
        router = Router(matrix, workdir=td)
        async def first(executor, task, context, on_event=None):
            # The snapshot must survive a crash during the first call, too.
            spec = board.get('tasks/t/spec')
            assert spec['execution_options'] == {'model': 'old-model', 'effort': 'low'}
            assert spec['pinned_agent'] == 'cc'
            return ExecResult(ok=True, output='done', session_id='session')
        with patch.object(ClaudeCodeExecutor, 'run', first):
            asyncio.run(Engine(Plan(goal='g', tasks={'t': task}), router, bus, board,
                               preflight=False, summarize_enabled=False, workdir=td).run())
        bus.close()
        assert task.execution_options == {'model': 'old-model', 'effort': 'low'}
        cfg['agents']['cc'].update(model='new-model', reasoning_effort='high')
        write_matrix()
        new_router = Router(matrix, workdir=td)
        async def followup(executor, message, session_id):
            assert (executor.model, executor.reasoning_effort) == ('old-model', 'low')
            return ExecResult(ok=True, output='followup')
        with patch.object(ClaudeCodeExecutor, 'run_resumed', followup):
            result, _ = asyncio.run(continue_task_conversation('t', 'next', new_router, board))
        assert result.ok
        restored = Plan.from_blackboard(board)
        restored_engine = Engine(restored, new_router, bus, board, preflight=False)
        name, executor = restored_engine._task_chain(restored.tasks['t'])[0]
        assert (name, executor.model, executor.reasoning_effort) == ('cc', 'old-model', 'low')

def test_followup_uses_frozen_options():
    with tempfile.TemporaryDirectory() as td:
        board=Blackboard(os.path.join(td,'board.jsonl'))
        board.put('tasks/t/agent','cc-sonnet',author='engine')
        board.put('tasks/t/session_id','session',author='engine')
        board.put('tasks/t/spec',{'execution_options':{'model':'frozen/model','effort':'high'}},author='system')
        router=Router('hub/capability_matrix.yaml')
        seen=[]
        clone=router.executor_with_options
        def selected(name,options):
            executor=clone(name,options)
            async def resumed(message,session_id):
                seen.append((executor.model,executor.reasoning_effort,executor.allowed_tools,session_id))
                return ExecResult(ok=True,output='done')
            executor.run_resumed=resumed
            return executor
        router.executor_with_options=selected
        async def wrong(*args,**kwargs):
            raise AssertionError('follow-up used current profile instead of frozen options')
        router.executor_for('cc-sonnet').run_resumed=wrong
        result,_=asyncio.run(continue_task_conversation('t','follow-up',router,board))
        assert result.ok and seen==[('frozen/model','high',router.allowed_tools('cc-sonnet'),'session')]

if __name__=='__main__':
    test_automatic_task_freezes_actual_options()
    test_followup_uses_frozen_options();print('Conversation agent options tests passed')
