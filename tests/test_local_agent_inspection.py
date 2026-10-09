"""Inspection HTTP uses fake metadata; GETs and revoked consent execute nothing."""
import copy
import http.client
import json
import os
import sys
import threading
from pathlib import Path
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).parent)]
import serve
from test_local_agent_settings import app
from test_gate_serve import _req

RESULT = {'kind': 'codex', 'installation': 'found', 'checked_at': 1,
          'auth': {'status': 'logged_in', 'detail': ''},
          'catalog': {'status': 'ready', 'source': 'codex:model/list', 'read_at': 1,
                      'models': [{'id': 'actual-model', 'name': 'Actual', 'is_default': True,
                                  'hidden': False, 'effort_levels': [], 'default_effort': None}]}}


def enable(port):
    settings = _req(port, 'GET', '/settings/local-agents')[1]['settings']
    settings['confirmed'] = True
    settings['agents']['codex']['enabled'] = True
    code, data = _req(port, 'POST', '/settings/local-agents',
                      {'settings': settings, 'expected_revision': settings['revision']})
    assert code == 200, (code, data)
    return data['settings']['revision']


def test_consent_revision_and_cached_get():
    with app() as (port, _), patch.object(serve, 'inspect_agent', return_value=RESULT, create=True) as inspect:
        body = {'kind': 'codex', 'revision': 0}
        code, data = _req(port, 'POST', '/settings/local-agents/check', body)
        assert code == 409, (code, data)
        inspect.assert_not_called()
        revision = enable(port)
        assert _req(port, 'POST', '/settings/local-agents/check', body)[0] == 409
        body['revision'] = revision
        code, data = _req(port, 'POST', '/settings/local-agents/check', body)
        assert code == 200, (code, data)
        assert data['inspection']['catalog']['models'][0]['id'] == 'actual-model'
        assert data['inspection']['tests'] == {}
        assert inspect.call_count == 1
        rows = _req(port, 'GET', '/settings/local-agents')[1]['agents']
        codex = next(row for row in rows if row['kind'] == 'codex')
        assert codex['inspection']['auth']['status'] == 'logged_in'
        assert inspect.call_count == 1, 'GET must not launch the Agent'
        assert _req(port, 'POST', '/settings/local-agents/check',
                    {'kind': 'claude_code', 'revision': revision})[0] == 409
        assert inspect.call_count == 1
        assert _req(port, 'POST', '/settings/local-agents/check', {'kind': 'invalid', 'revision': revision})[0] == 400


def test_busy_failure_corrupt_cache_and_stale_response():
    with app() as (port, _):
        revision = enable(port)
        body = {'kind': 'codex', 'revision': revision}
        with patch.object(serve, 'inspect_agent', return_value=RESULT, create=True) as inspect:
            lock = serve._inspection_locks['codex']
            lock.acquire()
            try: assert _req(port, 'POST', '/settings/local-agents/check', body)[0] == 409
            finally: lock.release()
            inspect.assert_not_called()
        with patch.object(serve, 'inspect_agent', side_effect=RuntimeError('SECRET')):
            code, data = _req(port, 'POST', '/settings/local-agents/check', body)
            assert code == 502 and 'SECRET' not in json.dumps(data), (code, data)
        assert not serve._inspection_locks['codex'].locked()
        Path(serve.RUNTIME_DIR, 'local-agent-metadata.json').write_text('{broken', encoding='utf-8')
        assert _req(port, 'GET', '/settings/local-agents')[0] == 200
        ready, release = threading.Event(), threading.Event()
        def inspect(*args, **kwargs):
            ready.set()
            assert release.wait(4)
            return RESULT
        response = []
        with patch.object(serve, 'inspect_agent', side_effect=inspect):
            worker = threading.Thread(target=lambda: response.append(
                _req(port, 'POST', '/settings/local-agents/check', body)))
            worker.start()
            assert ready.wait(2)
            cfg = serve._load_config()
            cfg['local_agents']['revision'] += 1
            cfg['local_agents']['agents']['codex']['enabled'] = False
            serve._save_config(cfg)
            release.set()
            worker.join(5)
        assert response[0][0] == 409, response
        assert not serve._inspection_locks['codex'].locked()


def test_origin_and_login_change_invalidate_tests():
    with app() as (port, _):
        revision = enable(port)
        request = {'kind': 'codex', 'revision': revision}
        conn = http.client.HTTPConnection('127.0.0.1', port)
        conn.request('POST', '/settings/local-agents/check', json.dumps(request),
                     {'Content-Type': 'application/json', 'Origin': 'https://evil.example'})
        assert conn.getresponse().status == 403
        conn.close()
        with patch.object(serve, 'inspect_agent', return_value=RESULT):
            data = _req(port, 'POST', '/settings/local-agents/check', request)[1]['inspection']
        path = Path(serve.RUNTIME_DIR, 'local-agent-metadata.json')
        saved = json.loads(path.read_text(encoding='utf-8'))
        saved['codex']['tests'] = {'["catalog","actual-model"]': {'status': 'passed'}}
        saved['codex']['auth']['accessToken'] = 'SECRET'
        saved['codex']['catalog']['models'][0]['apiKey'] = 'SECRET'
        path.write_text(json.dumps(saved), encoding='utf-8')
        assert 'SECRET' not in json.dumps(_req(port, 'GET', '/settings/local-agents')[1]), 'Cached secrets leaked'
        changed = copy.deepcopy(RESULT)
        changed['auth']['status'] = 'not_logged_in'
        with patch.object(serve, 'inspect_agent', return_value=changed):
            data = _req(port, 'POST', '/settings/local-agents/check', request)[1]['inspection']
        assert data['tests'] == {}, 'Observed auth change must invalidate prior passes'
        cfg = serve._load_config()
        cfg['matrix_patch']['agents']['codex-local']['model'] = 'different'
        serve._save_config(cfg)
        assert serve._inspection_snapshot('codex', cfg)['tests'] == {}


def test_failed_refresh_retains_last_directory_and_expired_passes():
    with app() as (port,_):
        revision=enable(port);body={'kind':'codex','revision':revision}
        with patch.object(serve,'inspect_agent',return_value=RESULT):
            assert _req(port,'POST','/settings/local-agents/check',body)[0]==200
        error=copy.deepcopy(RESULT);error['catalog']={'status':'error','models':[]};error['auth']['status']='error'
        with patch.object(serve,'inspect_agent',return_value=error):
            data=_req(port,'POST','/settings/local-agents/check',body)[1]['inspection']
        assert data['catalog']['models'][0]['id']=='actual-model'
        assert data['catalog']['stale'] is True and data['catalog']['read_at']==1
        path=Path(serve.RUNTIME_DIR,'local-agent-metadata.json');saved=json.loads(path.read_text(encoding='utf-8'))
        saved['codex']['tests']={'["catalog","actual-model"]':{'status':'passed','tested_at':2}}
        path.write_text(json.dumps(saved),encoding='utf-8')
        cfg=serve._load_config();cfg['local_agents']['revision']+=1;serve._save_config(cfg)
        snapshot=serve._inspection_snapshot('codex',cfg)
        assert snapshot['catalog']['models']==[]
        assert snapshot['tests']['["catalog","actual-model"]']['status']=='expired'


if __name__ == '__main__':
    for name, test in list(globals().items()):
        if name.startswith('test_'):
            test()
            print('PASS', name)
    print('PASS 4 inspection HTTP tests; no inference')
