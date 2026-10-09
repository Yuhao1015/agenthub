"""Only the confirmed model receives a probe; no real CLI is executed."""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from hub import local_model_probe as probe
except ImportError:
    probe = None

HELP = '--tools --disallowedTools --strict-mcp-config --mcp-config --setting-sources --safe-mode --restricted'


def test_single_target_isolated_no_retry():
    assert probe is not None, 'Single model probe is not implemented'
    calls = []
    async def runner(command, prompt, cwd, timeout):
        calls.append((command, prompt, cwd))
        assert os.path.isdir(cwd)
        return {'ok': True, 'usage': {'input_tokens': 4}, 'cost_status': 'unknown'}
    with patch.object(probe, '_probe_config_blocked', return_value=False):
        result = asyncio.run(probe.run_model_probe('codex', ['C:/Program Files/团队/codex.exe'], 'my-model', runner=runner))
    assert len(calls) == 1
    argv, prompt, cwd = calls[0]
    assert prompt == 'Reply with exactly: ok'
    assert argv[0] == 'C:/Program Files/团队/codex.exe'
    assert argv[argv.index('--model') + 1] == 'my-model'
    assert argv[argv.index('--sandbox') + 1] == 'read-only'
    assert '--dangerously-bypass-approvals-and-sandbox' not in argv
    assert result['ok'] and result['usage']['input_tokens'] == 4
    assert result['cost_status'] == 'unknown' and 'cost_usd' not in result
    assert not os.path.exists(cwd)
    async def failed(*args):
        calls.append(args)
        return {'ok': False, 'error': 'SECRET rejected', 'cost_status': 'unknown'}
    with patch.object(probe, '_probe_config_blocked', return_value=False):
        failure = asyncio.run(probe.run_model_probe('codex', ['fixture.exe'], 'my-model', runner=failed))
    assert not failure['ok'] and len(calls) == 2
    assert 'SECRET' not in json.dumps(failure)


def test_tools_and_option_injection():
    for kind, prefix in [('claude_code', ['claude.exe']), ('codebuddy', ['node.exe', 'codebuddy.js'])]:
        argv = probe.build_probe_argv(kind, prefix, 'model', help_text=HELP)
        assert argv[argv.index('--tools') + 1] == ''
        assert '--strict-mcp-config' in argv and '--setting-sources' in argv
        if kind == 'codebuddy':
            assert argv[argv.index('-p') + 1] == 'Reply with exactly: ok'
        try: probe.build_probe_argv(kind, prefix, 'model', help_text='old CLI')
        except ValueError: pass
        else: raise AssertionError('Unsupported safety controls accepted')
    for model in ['-option', 'line\nbreak', 'has space']:
        try: probe.build_probe_argv('codex', ['fixture.exe'], model)
        except ValueError: pass
        else: raise AssertionError('Invalid model argument accepted')


def test_reported_model_and_usage_projection():
    async def runner(*args):
        return {'ok': True, 'model': 'different', 'usage': {'input_tokens': 3, 'accessToken': 'SECRET'},
                'cost_status': 'reported', 'cost_usd': 0.0001, 'secret': 'SECRET'}
    with patch.object(probe, '_probe_config_blocked', return_value=False):
        result = asyncio.run(probe.run_model_probe('codex', ['fixture.exe'], 'selected', runner=runner))
    assert not result['ok'], 'A different model cannot prove the selected model worked'
    assert 'SECRET' not in json.dumps(result)


def test_metadata_json_is_not_a_successful_model_response():
    assert not probe._parse_result('{}')['ok']
    assert not probe._parse_result('{"account":{"type":"chatgpt"}}')['ok']
    assert probe._parse_result('{"type":"result","result":"ok","is_error":false}')['ok']


def test_codebuddy_array_reports_actual_tokens_and_credits():
    messages = [{'role': 'assistant', 'providerData': {'rawUsage': {
        'prompt_tokens': 8, 'completion_tokens': 1, 'credit': 0.2}}},
                {'type': 'result', 'result': 'ok', 'is_error': False}]
    result = probe.public_probe_result(probe._parse_result(json.dumps(messages)))
    assert result['ok']
    assert result['usage']['prompt_tokens'] == 8
    assert result['cost_credits'] == 0.2 and result['cost_status'] == 'reported'


def test_codex_guard_structurally_rejects_dotted_and_quoted_tools():
    variants=['mcp_servers.demo.command = "fixture-server"',
              '["mcp_servers"."demo"]\ncommand="fixture-server"',
              '["mcp_\\u0073ervers".demo]\ncommand="fixture-server"',
              'plugins.demo.enabled=true', 'apps.demo.enabled=true']
    with tempfile.TemporaryDirectory() as root,patch.dict(os.environ,{'CODEX_HOME':root}),patch.object(probe,'customizations_blocked',return_value=False):
        path=Path(root,'config.toml')
        for text in variants:
            path.write_text(text,encoding='utf-8')
            assert probe._probe_config_blocked('codex'),text
        path.write_text('model="fixture-model"\n',encoding='utf-8')
        assert not probe._probe_config_blocked('codex')
        with patch.dict(sys.modules, {'tomllib': None}):
            assert probe._probe_config_blocked('codex'), 'Missing parser must fail closed'
        path.write_text('invalid = [', encoding='utf-8')
        assert probe._probe_config_blocked('codex'), 'Invalid TOML must fail closed'


if __name__ == '__main__':
    for name, test in list(globals().items()):
        if name.startswith('test_'):
            test()
            print('PASS', name)
    print('PASS 6 single model probe tests; no inference')
