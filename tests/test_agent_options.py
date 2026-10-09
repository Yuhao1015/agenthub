import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hub.agent_options import normalize_options, agent_group
from hub import agent_options

def rejects(kind, raw):
    try:
        normalize_options(kind, raw)
    except ValueError:
        return
    raise AssertionError((kind, raw))

def test_options():
    assert normalize_options('claude_code', {}) == {}
    assert normalize_options('claude_code', {'model':'my/model-v2', 'effort':'high'}) == {'model':'my/model-v2', 'effort':'high'}
    assert normalize_options('codex', {'model':'', 'effort':''}) == {'model':'', 'effort':''}
    for value in ('two words', 'x\n--danger', 'x' * 201, '--danger', '\x00x'):
        rejects('codex', {'model':value})
    for value in (True, None, [], 'very high', 'x' * 33):
        rejects('codex', {'effort':value})
    rejects('codebuddy', {'effort':'high'})
    rejects('openrouter', {'model':''})
    rejects('claude_code', {'allowed_tools':'Bash'})
    assert agent_group('claude_code') == 'Claude Code'

def test_dsh_probe():
    original = agent_options.subprocess.run
    calls = []
    class Result:
        returncode = 0
        stdout = '- id: agent-default-model\n  config:\n    reasoningEffort: high\n'
    def fake(*args, **kwargs):
        calls.append(args)
        return Result()
    try:
        agent_options.dsh_effort_supported.cache_clear()
        agent_options.subprocess.run = fake
        assert agent_options.dsh_effort_supported(('fake-dsh',))
        assert agent_options.dsh_effort_supported(('fake-dsh',)) and len(calls) == 1
        def timeout(*args, **kwargs):
            raise agent_options.subprocess.TimeoutExpired('dsh', 3)
        agent_options.subprocess.run = timeout
        assert not agent_options.dsh_effort_supported(('timeout-dsh',))
    finally:
        agent_options.subprocess.run = original
        agent_options.dsh_effort_supported.cache_clear()

if __name__ == '__main__':
    test_options()
    test_dsh_probe()
    print('Agent options tests passed')
