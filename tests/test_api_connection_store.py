"""Fake credentials only. No HTTP, CLI or real credential reads."""
import json
import os
from pathlib import Path
import sys
import tempfile
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import yaml
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hub.api_connections import validate_connection, public_connection
from hub.connection_store import ConnectionStore, secure_write, verify_private


def row(name='api-a'):
    return dict(id=name, agent_id=name, name='我的服务', format='anthropic',
                base_url='https://example.invalid/v1/', model='model-a', enabled=False,
                roles=[], tier=None, max_output_tokens=4096, pricing=None)


def refuses(fn):
    try:
        fn()
    except (ValueError, OSError):
        return
    raise AssertionError('invalid operation accepted')


def test_schema_and_urls():
    r = validate_connection(row())
    assert r['base_url'] == 'https://example.invalid/v1'
    assert validate_connection(dict(row(), base_url='http://localhost:9000/v1/messages'))['base_url'] == 'http://localhost:9000/v1'
    for url in ('ftp://a', 'https://u:p@a/v1', 'https://a/v1?k=x', 'https://a/#x', 'https://a/\nx', 'https://a:bad'):
        refuses(lambda: validate_connection(dict(row(), base_url=url)))
    for extra in ({'unexpected': 1}, {'enabled': 'yes'}, {'max_output_tokens': True}, {'roles': ['image']}, {'pricing': {'input': float('nan'), 'output': 1}}):
        refuses(lambda: validate_connection(dict(row(), **extra)))
    public = public_connection(dict(r, credential_ref='connection/a/r1'), True)
    assert public['has_key'] and 'credential_ref' not in public


def test_key_lifecycle_and_public_projection():
    with tempfile.TemporaryDirectory() as td:
        config, credentials = str(Path(td)/'p/config.yaml'), str(Path(td)/'credentials.yaml')
        s = ConnectionStore(config, credentials)
        assert s.snapshot()['items'] == {}
        a = s.save(row(), 'replace', 'fake-A&<secret>', 0)
        assert a['revision'] == 1 and a['items']['api-a']['has_key']
        s.save(row('api-b'), 'replace', 'fake-B', 1)
        raw = yaml.safe_load(Path(config).read_text(encoding='utf8'))
        ref = raw['api_connections']['items']['api-a']['credential_ref']
        assert s.resolve_secret(ref) == 'fake-A&<secret>'
        assert 'fake-' not in json.dumps(s.snapshot()) + Path(config).read_text(encoding='utf8')
        verify_private(credentials)
        s.save(dict(row(), name='改名'), 'keep', '', 2)
        assert s.resolve_secret(ref) == 'fake-A&<secret>'
        refuses(lambda: s.save(row(), 'keep', '', 2))
        s.save(row(), 'clear', '', 3)
        assert not s.snapshot()['items']['api-a']['has_key']
        s.delete('api-b', 4)
        assert set(s.snapshot()['items']) == {'api-a'}


def test_failed_publish_keeps_previous_key_and_configuration():
    with tempfile.TemporaryDirectory() as td:
        config, credentials = str(Path(td)/'config.yaml'), str(Path(td)/'credentials.yaml')
        s = ConnectionStore(config, credentials)
        s.save(row(), 'replace', 'fake-original', 0)
        before = Path(config).read_bytes()
        ref = yaml.safe_load(before)['api_connections']['items']['api-a']['credential_ref']
        real_replace = os.replace
        for target in (config, credentials):
            def fail(src, dest):
                if str(dest) == target:
                    raise OSError('injected disk failure')
                return real_replace(src, dest)
            with patch('hub.connection_store.os.replace', side_effect=fail):
                refuses(lambda: s.save(row(), 'replace', 'fake-new', 1))
            assert Path(config).read_bytes() == before and s.resolve_secret(ref) == 'fake-original'
        with patch('hub.connection_store.protect_private', side_effect=OSError('ACL failure')):
            refuses(lambda: s.save(row(), 'replace', 'must-never-write', 1))
        assert 'must-never-write' not in Path(credentials).read_text(encoding='utf8')


def test_concurrent_projects_and_corrupt_files():
    with tempfile.TemporaryDirectory() as td:
        credentials = str(Path(td)/'credentials.yaml')
        stores = [ConnectionStore(str(Path(td)/p/'config.yaml'), credentials) for p in ('a', 'b')]
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda pair: pair[1].save(row(), 'replace', 'fake-'+str(pair[0]), 0), enumerate(stores)))
        refs = [yaml.safe_load(Path(s.config_path).read_text())['api_connections']['items']['api-a']['credential_ref'] for s in stores]
        assert refs[0] != refs[1]
        assert [s.resolve_secret(ref) for s, ref in zip(stores, refs)] == ['fake-0', 'fake-1']
        Path(stores[0].config_path).write_text('api_connections: [broken', encoding='utf8')
        refuses(stores[0].snapshot)
        refuses(lambda: stores[0].save(row(), 'clear', '', 1))
        assert '[broken' in Path(stores[0].config_path).read_text()


def _save_worker(config, credentials, queue):
    try:
        ConnectionStore(config, credentials).save(row(), 'replace', 'fake-process-key', 0)
        queue.put('saved')
    except ValueError:
        queue.put('conflict')


def test_process_conflict_and_environment_reference():
    with tempfile.TemporaryDirectory() as td:
        config, credentials = str(Path(td)/'config.yaml'), str(Path(td)/'credentials.yaml')
        ctx = multiprocessing.get_context('spawn')
        queue = ctx.Queue()
        workers = [ctx.Process(target=_save_worker, args=(config, credentials, queue)) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(15)
            assert worker.exitcode == 0
        assert sorted([queue.get(timeout=3), queue.get(timeout=3)]) == ['conflict', 'saved']
        store = ConnectionStore(config, credentials)
        with patch.dict(os.environ, {'AH_TEST_KEY': 'fake-env'}):
            assert store.resolve_secret('env/AH_TEST_KEY') == 'fake-env'
            assert os.environ['AH_TEST_KEY'] == 'fake-env'
        refuses(lambda: store.save(dict(row(), agent_id='different'), 'keep', '', 1))


if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_'):
            fn()
            print('PASS', name)
