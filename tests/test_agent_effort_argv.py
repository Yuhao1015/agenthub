import os, sys, asyncio, yaml
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hub import executors as e
from hub.models import Task, TaskType

def test_argv():
    old_claude, old_codex, old_spawn = e._claude_cmd, e._codex_cmd, asyncio.create_subprocess_exec
    captured = []
    async def missing(*argv, **kwargs):
        captured.append(list(argv))
        raise FileNotFoundError()
    try:
        e._claude_cmd = lambda: ['fake-claude']
        e._codex_cmd = lambda path=None: ['fake-codex']
        asyncio.create_subprocess_exec = missing
        task = Task(id='t', title='t', type=TaskType.CODING, prompt='secret prompt')
        for model, effort in [('custom/model', 'high'), ('', '')]:
            ex = e.build_executor({'kind':'claude_code', 'model':model, 'reasoning_effort':effort, 'allowed_tools':'Read'}, '.')
            asyncio.run(ex.run(task, ''))
            ex.stream = False
            asyncio.run(ex.run(task, ''))
            asyncio.run(ex.run_resumed('followup', 'sid'))
            for argv in captured[-3:]:
                assert ('--model' in argv) == bool(model), argv
                assert ('--effort' in argv) == bool(effort), argv
                if effort: assert argv[argv.index('--effort')+1] == effort
                assert argv[argv.index('--allowedTools')+1] == 'Read'
                assert 'secret prompt' not in argv
        ex = e.build_executor({'kind':'codex', 'model':'custom/model', 'reasoning_effort':'high'}, '.')
        argv = ex._argv('p')
        assert argv[argv.index('-c')+1] == 'model_reasoning_effort="high"', argv
        assert argv[argv.index('--sandbox')+1] == 'workspace-write'
        patch = yaml.safe_load(e._dsh_model_patch_text('provider', 'my: model', 'high'))
        assert patch == [{'id':'agent-default-model', 'config':{'provider':'provider', 'model':'my: model', 'reasoningEffort':'high'}}]
        assert 'reasoningEffort' not in yaml.safe_load(e._dsh_model_patch_text('p','m'))[0]['config']
    finally:
        e._claude_cmd, e._codex_cmd, asyncio.create_subprocess_exec = old_claude, old_codex, old_spawn

if __name__ == '__main__':
    test_argv()
    print('Effort argv tests passed')
