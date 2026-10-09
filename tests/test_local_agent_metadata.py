"""Metadata is a directory/status query, never a model turn."""
import contextlib
import io
import json
import os
import sys
import tempfile
import types
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from hub import local_agent_metadata as meta
except ImportError:
    meta = None


class Rpc:
    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []
        self.closed = False

    def __enter__(self): return self
    def __exit__(self, *args): self.closed = True
    def notify(self, method, params): self.calls.append((method, params))
    def call(self, method, params):
        self.calls.append((method, params))
        value = self.results.get(method, {})
        if isinstance(value, Exception): raise value
        return value(params) if callable(value) else value


def test_model_projection():
    assert meta is not None, 'Metadata reader is not implemented'
    rows = meta.project_models('codex', {'data': [
        {'id': 'vendor/new-id', 'displayName': '<img onerror=alert(1)>',
         'accessToken': 'SECRET', 'isDefault': True,
         'supportedReasoningEfforts': [{'reasoningEffort': 'high'}]},
        {'id': 'unknown-model', 'displayName': 'same', 'hidden': True}]})
    assert [r['id'] for r in rows] == ['vendor/new-id', 'unknown-model']
    assert rows[0]['effort_levels'] == ['high']
    assert rows[1]['hidden'] is True
    assert 'SECRET' not in json.dumps(rows)
    assert rows[0]['name'] == '<img onerror=alert(1)>'
    for bad in ({'data': {}}, {'data': [{'id': '-bad'}]}, {'data': [{'id': 'has space'}]}):
        try: meta.project_models('codex', bad)
        except ValueError: pass
        else: raise AssertionError('Invalid directory must not be accepted')


def test_codex_pages_and_no_inference():
    rpc = Rpc({'account/read': {'account': {'type': 'chatgpt', 'email': 'SECRET'}},
               'model/list': lambda p: {'data': [{'id': 'm2' if p.get('cursor') else 'm1'}],
                                       'nextCursor': None if p.get('cursor') else 'page2'}})
    with mock.patch('hub.local_agent_metadata.customizations_blocked', return_value=False):
        result = meta.inspect_agent('codex', ['fixture.exe'], rpc_factory=lambda *a: rpc)
    assert result['auth']['status'] == 'logged_in'
    assert [r['id'] for r in result['catalog']['models']] == ['m1', 'm2']
    assert 'SECRET' not in json.dumps(result)
    assert rpc.closed
    assert set(m for m, _ in rpc.calls) <= {'initialize', 'initialized', 'account/read', 'model/list'}
    assert next(p for m, p in rpc.calls if m == 'account/read')['refreshToken'] is False
    assert all(p['includeHidden'] for m, p in rpc.calls if m == 'model/list')


def test_failure_does_not_publish_partial_directory():
    rpc = Rpc({'account/read': {'account': None, 'requiresOpenaiAuth': True},
               'model/list': {'data': [{'id': 'm1'}], 'nextCursor': 'loop'}})
    with mock.patch('hub.local_agent_metadata.customizations_blocked', return_value=False):
        result = meta.inspect_agent('codex', ['fixture.exe'], rpc_factory=lambda *a: rpc)
    assert result['auth']['status'] == 'not_logged_in'
    assert result['catalog']['status'] == 'error'
    assert result['catalog']['models'] == [] and rpc.closed
    rpc = Rpc({'account/read': TimeoutError('SECRET')})
    with mock.patch('hub.local_agent_metadata.customizations_blocked', return_value=False):
        result = meta.inspect_agent('codex', ['fixture.exe'], rpc_factory=lambda *a: rpc)
    assert result['auth']['status'] == 'error'
    assert 'SECRET' not in json.dumps(result)
    assert rpc.closed


def test_claude_auth_is_not_a_model_list():
    calls = []
    def command(argv, **kwargs):
        calls.append((argv, kwargs))
        return types.SimpleNamespace(returncode=0, stdout='{"loggedIn":true,"email":"SECRET"}')
    with mock.patch('hub.local_agent_metadata.customizations_blocked', return_value=False):
        result = meta.inspect_agent('claude_code', ['C:/Program Files/团队/claude.exe'], command=command)
    assert result['auth']['status'] == 'logged_in'
    assert result['catalog']['status'] == 'unsupported'
    assert result['catalog']['models'] == []
    assert calls[0][0][-2:] == ['auth', 'status']
    assert calls[0][1]['shell'] is False
    assert calls[0][1]['timeout'] == 15
    assert 'SECRET' not in json.dumps(result)
    with mock.patch('hub.local_agent_metadata.customizations_blocked', return_value=False):
        old = meta.inspect_agent('claude_code', ['fixture'], command=lambda *a, **kw:
                    types.SimpleNamespace(returncode=1, stdout='', stderr='unknown command'))
    assert old['auth']['status'] == 'unsupported'


def test_acp_only_reads_returned_models():
    rpc = Rpc({'session/new': {'sessionId': 'private', 'models': {
        'currentModelId': 'my-model', 'availableModels': [{'modelId': 'my-model', 'name': 'Custom'}]}}})
    with mock.patch('hub.local_agent_metadata.customizations_blocked', return_value=False):
        result = meta.inspect_agent('codebuddy', ['node.exe', 'codebuddy.js'], rpc_factory=lambda *a: rpc)
    assert result['catalog']['models'][0]['id'] == 'my-model'
    assert result['auth']['status'] == 'unsupported', 'A directory does not prove login'
    assert set(m for m, _ in rpc.calls) == {'initialize', 'session/new'}
    assert next(p for m, p in rpc.calls if m == 'session/new')['mcpServers'] == []
    assert rpc.closed


def test_cache_binds_installation_and_revision():
    with tempfile.TemporaryDirectory() as temp:
        exe = os.path.join(temp, 'tool.exe')
        with open(exe, 'w') as f: f.write('one')
        fingerprint = meta.installation_fingerprint('codex', [exe], 1)
        assert fingerprint != meta.installation_fingerprint('codex', [exe], 2)
        path = os.path.join(temp, 'cache.json')
        with open(path, 'w') as f: json.dump({'fingerprint': fingerprint, 'auth': {'status': 'logged_in'}}, f)
        assert meta.load_snapshot(path, fingerprint)['auth']['status'] == 'logged_in'
        assert meta.load_snapshot(path, 'other') == {}
        with open(exe, 'a') as f: f.write('two')
        assert fingerprint != meta.installation_fingerprint('codex', [exe], 1)
        with open(path, 'w') as f: f.write('{broken')
        assert meta.load_snapshot(path, fingerprint) == {}


def test_unsafe_customizations_are_not_executed():
    with tempfile.TemporaryDirectory() as home:
        os.makedirs(os.path.join(home, '.claude'))
        with open(os.path.join(home, '.claude', 'settings.json'), 'w') as f:
            json.dump({'apiKeyHelper': 'arbitrary script'}, f)
        with mock.patch('os.path.expanduser', side_effect=lambda p: p.replace('~', home)):
            assert meta.customizations_blocked('claude_code')
        with mock.patch('hub.local_agent_metadata.customizations_blocked', return_value=True), \
             mock.patch('subprocess.Popen', side_effect=AssertionError('No process allowed')):
            result = meta.inspect_agent('claude_code', ['fixture.exe'])
        assert result['auth']['status'] == 'unsupported'


class FakeProcess:
    def __init__(self, output):
        self.args = ['fixture.exe']
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(output)
        self.stderr = io.BytesIO(b'')
        self.returncode = None
        self.stopped = False
    def poll(self): return self.returncode
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def communicate(self, input=None, timeout=None):
        self.returncode = 0
        return self.stdout.read().decode(), ''
    def terminate(self): self.stopped = True; self.returncode = 0
    def kill(self): self.stopped = True; self.returncode = -1
    def wait(self, timeout=None): self.returncode = 0; return 0


def test_transport_denies_tools_and_closes_process():
    responses = [{'id': 1, 'result': {}},
                 {'id': 99, 'method': 'fs/readFile', 'params': {'path': 'private'}},
                 {'id': 2, 'result': {'account': None}}]
    proc = FakeProcess(b''.join((json.dumps(r) + '\n').encode() for r in responses))
    with mock.patch('subprocess.Popen', return_value=proc) as popen:
        with meta.JsonRpcPipe(['fixture.exe'], '.', 1) as rpc:
            rpc.call('initialize', {})
            rpc.call('account/read', {})
            sent = proc.stdin.getvalue().decode()
            assert 'Metadata client denies tools' in sent
            try: rpc.call('turn/start', {})
            except ValueError: pass
            else: raise AssertionError('Inference request accepted')
        assert proc.stopped and proc.stdout.closed and proc.stderr.closed
        assert popen.call_args.kwargs['shell'] is False


def test_windows_transport_owns_descendants():
    proc=FakeProcess(b'{"id":1,"result":{}}\n');proc._handle=123
    job=mock.Mock()
    with mock.patch.object(meta.os,'name','nt'),mock.patch('subprocess.Popen',return_value=proc),mock.patch('hub.executors._create_windows_kill_job',return_value=job) as attach:
        with meta.JsonRpcPipe(['fixture.exe'],'.',1) as rpc:rpc.call('initialize',{})
    attach.assert_called_once()
    job.terminate.assert_called_once();job.close.assert_called_once()


def test_status_output_is_bounded_before_capture():
    proc = FakeProcess(b'x' * (meta.MAX_LINE + 1))
    with mock.patch('hub.local_agent_metadata.customizations_blocked', return_value=False), \
         mock.patch('subprocess.Popen', return_value=proc):
        result = meta.inspect_agent('claude_code', ['fixture.exe'])
    assert proc.stopped, 'Bounded command reader must stop excess output'
    assert result['auth']['status'] == 'error'


if __name__ == '__main__':
    tests = [v for k, v in list(globals().items()) if k.startswith('test_')]
    for test in tests:
        test()
        print('PASS', test.__name__)
    print(f'PASS {len(tests)} metadata tests; no inference')
