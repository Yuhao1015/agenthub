"""HTTP confirmation and target checks; the async model runner is fake."""
import copy
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).parent)]
import serve
from test_gate_serve import _req
from test_local_agent_settings import app
from test_local_agent_inspection import enable, RESULT


def target(port):
    revision = enable(port)
    result = copy.deepcopy(RESULT)
    result['catalog']['models'].append(dict(result['catalog']['models'][0], id='other-model'))
    with patch.object(serve, 'inspect_agent', return_value=result):
        inspection = _req(port, 'POST', '/settings/local-agents/check', {'kind': 'codex', 'revision': revision})[1]['inspection']
    return {'kind': 'codex', 'model': 'actual-model', 'source': 'catalog',
            'revision': revision, 'fingerprint': inspection['fingerprint'], 'confirm_cost': True}


def test_confirmation_one_target_and_unknown_cost():
    calls = []
    async def fake(kind, argv, model, **kwargs):
        calls.append((kind, argv, model))
        return {'ok': True, 'usage': {'input_tokens': 5}, 'cost_status': 'unknown'}
    with app() as (port, _), patch.object(serve, 'run_model_probe', side_effect=fake, create=True):
        body = target(port)
        for value in (False, None, 1):
            bad = dict(body, confirm_cost=value)
            code, data = _req(port, 'POST', '/settings/local-agents/test', bad)
            assert code == 400, (code, data)
        assert calls == []
        assert _req(port, 'POST', '/settings/preflight', {})[0] == 400
        assert calls == []
        code, data = _req(port, 'POST', '/settings/local-agents/test', body)
        assert code == 200 and data['ok'], (code, data)
        assert len(calls) == 1 and calls[0][2] == 'actual-model'
        assert data['cost_status'] == 'unknown' and 'cost_usd' not in data
        snapshot = serve._inspection_snapshot('codex', serve._strict_config())
        assert snapshot['tests']['["catalog","actual-model"]']['status'] == 'passed'
        assert '["catalog","other-model"]' not in snapshot['tests']
        assert serve._connection_inflight == 0


def test_stale_busy_disabled_and_latest_failure():
    async def failed(*args, **kwargs):
        return {'ok': False, 'error': 'SECRET', 'usage': {}, 'cost_status': 'unknown'}
    with app() as (port, _), patch.object(serve, 'run_model_probe', side_effect=failed, create=True) as runner:
        body = target(port)
        for bad in (dict(body, revision=body['revision']-1), dict(body, fingerprint='old')):
            assert _req(port, 'POST', '/settings/local-agents/test', bad)[0] == 409
        assert _req(port, 'POST', '/settings/local-agents/test', dict(body, model='not-in-catalog'))[0] == 400
        with patch.object(serve, '_run_busy', return_value=True):
            assert _req(port, 'POST', '/settings/local-agents/test', body)[0] == 409
        runner.assert_not_called()
        code, data = _req(port, 'POST', '/settings/local-agents/test', body)
        assert code == 200 and not data['ok'] and 'SECRET' not in json.dumps(data), (code, data)
        assert serve._inspection_snapshot('codex', serve._strict_config())['tests']['["catalog","actual-model"]']['status'] == 'failed'
        assert serve._connection_inflight == 0
        with patch.object(serve, 'run_model_probe', side_effect=RuntimeError('SECRET')):
            assert _req(port, 'POST', '/settings/local-agents/test', body)[0] == 502
        assert serve._connection_inflight == 0 and not serve._inspection_locks['codex'].locked()
        assert serve._inspection_snapshot('codex', serve._strict_config())['tests']['["catalog","actual-model"]']['status'] == 'failed'
        cfg = serve._load_config()
        cfg['local_agents']['agents']['codex']['enabled'] = False
        serve._save_config(cfg)
        assert _req(port, 'POST', '/settings/local-agents/test', body)[0] == 409


def test_running_probe_blocks_engine_and_manager():
    with app() as (port, _):
        enable(port)
        previous = serve._connection_inflight
        serve._connection_inflight = 1
        try:
            with patch.object(serve, '_spawn_main', return_value=42) as spawn:
                assert serve._spawn_main_if_idle(['--mock']) is None
                assert serve._spawn_manager_main_if_idle(['--mock']) == (None, '')
                spawn.assert_not_called()
            try:
                with serve._connection_call():
                    raise AssertionError('Overlapping connection call accepted')
            except ValueError:
                pass
            assert serve._connection_inflight == 1
            assert _req(port, 'POST', '/settings/api-connections/test',
                        {'id': 'missing', 'revision': 0, 'confirm_cost': True})[0] == 409
            with patch.object(serve.Handler, '_handle_chat_locked',
                              side_effect=lambda *args: None) as manager:
                # Atomic reservation must reject before the slow manager callback.
                code, data = _req(port, 'POST', '/chat', {'message': 'hello'})
                assert code == 409, (code, data)
                manager.assert_not_called()
        finally:
            serve._connection_inflight = previous


def test_latest_concrete_model_result_shared_across_sources():
    async def passed(*args,**kwargs):return {'ok':True}
    async def failed(*args,**kwargs):return {'ok':False}
    with app() as (port,_):
        cfg=serve._load_config();cfg.setdefault('matrix_patch',{}).setdefault('agents',{}).setdefault('codex-local',{})['model']='actual-model';serve._save_config(cfg)
        body=target(port)
        with patch.object(serve,'run_model_probe',side_effect=passed):assert _req(port,'POST','/settings/local-agents/test',body)[1]['ok']
        with patch.object(serve,'run_model_probe',side_effect=failed):assert not _req(port,'POST','/settings/local-agents/test',dict(body,source='configured'))[1]['ok']
        tests=serve._inspection_snapshot('codex',serve._strict_config())['tests']
        assert tests['["catalog","actual-model"]']['status']=='failed'
        assert tests['["configured","actual-model"]']['status']=='failed'
        with patch.object(serve,'run_model_probe',side_effect=passed):assert _req(port,'POST','/settings/local-agents/test',dict(body,model='',source='runtime_default'))[1]['ok']
        tests=serve._inspection_snapshot('codex',serve._strict_config())['tests']
        assert tests['["catalog","actual-model"]']['status']=='failed'


if __name__ == '__main__':
    for name, test in list(globals().items()):
        if name.startswith('test_'):
            test()
            print('PASS', name)
    print('PASS 4 model test HTTP checks; no inference')
