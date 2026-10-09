"""Model-specific effort validation; no network or real CLI calls."""
import os
import sys
import tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hub.agent_options import selection_capabilities, validate_effort


def test_capabilities():
    declared = {'test-a': {'effort_control': 'supported', 'effort_levels': ['low', 'high']},
                'test-b': {'effort_control': 'unsupported', 'effort_levels': []}}
    known = selection_capabilities('claude_code', 'test-a', declared, {})
    assert known['source'] == 'configuration'
    assert validate_effort({'effort': 'high'}, known) == {'effort': 'high'}
    assert validate_effort({'effort': ''}, known) == {'effort': ''}
    for cap, effort in ((known, 'xhigh'), (selection_capabilities('claude_code', 'test-b', declared, {}), 'high')):
        try:
            validate_effort({'effort': effort}, cap)
        except ValueError:
            pass
        else:
            raise AssertionError('unsupported level accepted')
    unknown = selection_capabilities('codex', 'custom', {}, {})
    assert unknown['effort_control'] == 'unverified'
    assert validate_effort({'effort': 'custom-high'}, unknown)['effort'] == 'custom-high'
    assert selection_capabilities('codebuddy', 'custom', declared, {})['effort_control'] == 'unsupported'
    runtime = {'test-a': {'effort_control': 'supported', 'effort_levels': ['medium']}}
    assert selection_capabilities('codex', 'test-a', declared, runtime)['effort_levels'] == ['medium']
    known['effort_levels'].append('fake')
    assert declared['test-a']['effort_levels'] == ['low', 'high']


def test_http_validation():
    import serve
    from test_gate_serve import _snap, _restore, _start, _req
    from hub.blackboard import Blackboard
    saved = _snap()
    with tempfile.TemporaryDirectory() as td:
        server, port = _start(td)
        try:
            cfg = serve._load_config()
            cfg['matrix_patch'] = {'agents': {'cc-sonnet': {
                'allowed_tools': 'Read', 'model': 'test-a', 'reasoning_effort': 'high',
                'model_capabilities': {'test-a': {'effort_control': 'supported', 'effort_levels': ['low', 'high']},
                                       'test-b': {'effort_control': 'unsupported', 'effort_levels': []}}}}}
            serve._save_config(cfg); serve._write_merged_matrix()
            before = open(serve.CONFIG_PATH, 'rb').read()
            for path, body in [('/settings/agent-options', {'agent': 'cc-sonnet', 'options': {'model': 'test-b'}}),
                               ('/settings/config', {'ui_prefs': {'manager_agent': 'cc-sonnet', 'manager_model_mode': 'explicit', 'manager_model': 'test-b'}}),
                               ('/settings/agent-options', {'agent': 'cc-sonnet', 'options': {'model': 'test-a', 'effort': 'xhigh'}}),
                               ('/settings/config', {'ui_prefs': {'manager_agent': 'cc-sonnet', 'manager_model_mode': 'explicit',
                                'manager_model': 'test-b', 'manager_effort_mode': 'explicit', 'manager_effort': 'high'}})]:
                code, data = _req(port, 'POST', path, body)
                assert code == 400, (path, code, data)
                assert open(serve.CONFIG_PATH, 'rb').read() == before
            code, data = _req(port, 'GET', '/agents')
            agent = next(a for a in data['agents'] if a['name'] == 'cc-sonnet')
            assert agent['model_capabilities']['test-a']['effort_levels'] == ['low', 'high']
            board = Blackboard(serve.BOARD_PATH, on_write=lambda e: None)
            board.put('run/gate/plan', {'kind': 'plan', 'gate_token': 'a'*16, 'available_agents': [agent]}, author='engine')
            board.put('tasks/t1/spec', {'type': 'coding'}, author='system')
            body = {'gate_id': 'plan', 'gate_token': 'a'*16, 'decision': 'approve', 'overrides': {'t1': 'cc-sonnet'},
                    'execution_options': {'t1': {'model': 'test-b', 'effort': 'high'}}}
            code, data = _req(port, 'POST', '/gate', body)
            assert code == 400, (code, data)
            body['execution_options']['t1'] = {'model': 'test-b'}
            code, data = _req(port, 'POST', '/gate', body)
            assert code == 400, ('inherited effort must be checked', code, data)
            assert Blackboard(serve.BOARD_PATH, on_write=lambda e: None).get('run/gate_reply/plan') is None
        finally:
            server.shutdown(); server.server_close(); _restore(saved)


if __name__ == '__main__':
    test_capabilities(); test_http_validation()
    print('Model capability tests passed')
