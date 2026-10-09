"""Consent storage/HTTP tests; no real executors or processes."""
from __future__ import annotations
import contextlib
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).parent)]
import serve
from hub.local_agents import validate_local_settings, apply_local_policy
from test_gate_serve import _snap, _restore, _start, _req


def found(kind, overrides=None):
    return {'kind': kind, 'status': 'found', 'argv': ['/fake/' + kind],
            'source': 'installation', 'detail': '未验证', 'candidates': []}


@contextlib.contextmanager
def app():
    snapshot = _snap()
    proxy = types.SimpleNamespace(**vars(serve.subprocess))
    def no_calls(*args, **kwargs):
        raise AssertionError('real call forbidden')
    proxy.Popen = no_calls
    with tempfile.TemporaryDirectory() as root, \
         patch.object(serve, 'subprocess', proxy), \
         patch.object(serve.conversation, 'continue_task_conversation', no_calls), \
         patch.object(serve, '_resolve_commit', return_value='test'), \
         patch.object(serve, 'discover_local', side_effect=found) as discover:
        server, port = _start(root, with_run=False, authorize_agents=False)
        try:
            yield port, discover
        finally:
            server.shutdown()
            server.server_close()
            _restore(snapshot)


def test_validation_and_matrix_projection():
    state = validate_local_settings({}, 'win32')
    assert not state['confirmed']
    base = {'agents': {'c': {'kind': 'codex'}}, 'routes': {}, 'tiers': {}}
    result = apply_local_policy(base, state, 'win32')
    assert result['local_agent_policy']['enabled_kinds'] == []
    assert 'local_agent_policy' not in base
    state['confirmed'] = True
    state['agents']['codex']['enabled'] = True
    state['agents']['codex']['paths'] = {'executable': 'C:/chosen/codex.exe'}
    result = apply_local_policy(base, state, 'win32')
    assert result['agents']['c']['codex_path'] == 'C:/chosen/codex.exe'
    assert result['local_agent_policy']['generic_fallback'] == ['c']
    assert not validate_local_settings(state, 'darwin')['confirmed']
    for bad in ({'version': 99}, {'version': 1, 'agents': {'codex': {'enabled': 'yes'}}},
                {'version': 1, 'agents': {'alien': {}}}, {'version': 1, 'confirmed': 1}):
        try:
            validate_local_settings(bad, 'win32')
        except ValueError:
            pass
        else:
            raise AssertionError(bad)


def test_get_discover_save_and_conflict():
    with app() as (port, discover):
        code, data = _req(port, 'GET', '/settings/local-agents')
        assert code == 200, (code, data)
        assert not data['settings']['confirmed']
        discover.assert_not_called()
        before = serve._load_config()
        code, data = _req(port, 'POST', '/settings/local-agents/discover', {})
        assert code == 200 and len(data['agents']) == 3, (code, data)
        assert before == serve._load_config()
        settings = data['settings']
        settings['confirmed'] = True
        settings['agents']['codex']['enabled'] = True
        body = {'settings': settings, 'expected_revision': 0}
        code, data = _req(port, 'POST', '/settings/local-agents', body)
        assert code == 200, (code, data)
        assert serve._load_config()['local_agents']['agents']['codex']['enabled']
        assert _req(port, 'POST', '/settings/local-agents', body)[0] == 409
        code, data = _req(port, 'POST', '/settings/local-agents/discover', {})
        assert code == 200 and data['settings']['agents']['codex']['enabled']
        assert data['settings']['revision'] == 1


def test_invalid_candidates_busy_corruption_and_write_failure():
    with app() as (port, discover):
        state = _req(port, 'GET', '/settings/local-agents')[1]['settings']
        state['confirmed'] = True
        state['agents']['codex']['enabled'] = True
        body = {'settings': state, 'expected_revision': 0}
        discover.side_effect = lambda kind, overrides=None: dict(found(kind), status='missing', argv=[])
        assert _req(port, 'POST', '/settings/local-agents', body)[0] == 400
        discover.side_effect = found
        with patch.object(serve, '_run_busy', return_value=True):
            assert _req(port, 'POST', '/settings/local-agents', body)[0] == 409
        with patch.object(serve, '_save_config', side_effect=OSError('disk full')):
            assert _req(port, 'POST', '/settings/local-agents', body)[0] == 500
        assert 'local_agents' not in serve._load_config()
        Path(serve.CONFIG_PATH).write_text('broken: [', encoding='utf-8')
        before = Path(serve.CONFIG_PATH).read_bytes()
        assert _req(port, 'GET', '/settings/local-agents')[0] == 409
        assert _req(port, 'POST', '/settings/local-agents', body)[0] == 409
        assert Path(serve.CONFIG_PATH).read_bytes() == before


def test_restart_and_concurrent_saves_keep_one_revision():
    with app() as (port, discover):
        state = _req(port, 'GET', '/settings/local-agents')[1]['settings']
        state['confirmed'] = True
        body = {'settings': state, 'expected_revision': 0}
        codes = []
        threads = [threading.Thread(target=lambda: codes.append(
            _req(port, 'POST', '/settings/local-agents', body)[0])) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(codes) == [200, 409], codes
        data_dir = serve.DATA_DIR
        serve._configure(data_dir, 'default', port)
        discover.reset_mock()
        data = _req(port, 'GET', '/settings/local-agents')[1]
        assert data['settings']['revision'] == 1 and data['settings']['confirmed']
        discover.assert_not_called()


def test_failed_matrix_publish_restores_config():
    with app() as (port, discover):
        state = _req(port, 'GET', '/settings/local-agents')[1]['settings']
        state['confirmed'] = True
        body = {'settings': state, 'expected_revision': 0}
        with patch.object(serve, '_write_merged_matrix', side_effect=OSError('replace denied')):
            assert _req(port, 'POST', '/settings/local-agents', body)[0] == 500
        assert 'local_agents' not in serve._load_config()


def test_clearing_manual_path_restores_discovery():
    with app() as (port, discover):
        state = _req(port, 'GET', '/settings/local-agents')[1]['settings']
        state['confirmed'] = True
        state['agents']['codex']['paths'] = {'executable': str(Path(serve.CONFIG_PATH).parent/'custom.exe')}
        assert _req(port,'POST','/settings/local-agents',{'settings':state,'expected_revision':0})[0]==200
        state['agents']['codex']['paths'] = {}
        code, result = _req(port,'POST','/settings/local-agents',{'settings':state,'expected_revision':1})
        assert code==200 and result['settings']['agents']['codex']['paths']=={}, result
        assert not serve._load_yaml_dict(serve.MATRIX)['agents']['codex-local'].get('codex_path')


if __name__ == '__main__':
    for name in sorted(n for n in globals() if n.startswith('test_')):
        globals()[name]()
        print('PASS', name)
