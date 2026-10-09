"""Migration/projection uses synthetic presence flags, never real credentials."""
import copy
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
import yaml
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hub.api_connections import project_api_connections, propose_legacy_api_migration
from hub.connection_store import ConnectionStore
from hub.local_agents import apply_local_policy
from hub.router import Router
from hub.models import Task, TaskType


def base():
    return yaml.safe_load((Path(__file__).resolve().parents[1]/'hub/capability_matrix.yaml').read_text(encoding='utf8'))


def test_empty_and_confirmed_projection():
    matrix = base()
    assert propose_legacy_api_migration(matrix, {}, {})['items'] == {}
    empty = project_api_connections(matrix, {})
    assert not any(v['kind'] in ('api', 'openrouter') for v in empty['agents'].values())
    assert empty['agents']['dsh-pro'] == matrix['agents']['dsh-pro']
    proposal = propose_legacy_api_migration(matrix, {'openrouter': True, 'deepseek': True}, {})
    assert proposal['items']['or-deepseek']['agent_id'] == 'or-deepseek'
    assert all(v['format'] == 'openrouter' for v in proposal['items'].values())
    assert not any(v['agent_id'] == 'dsh-pro' for v in proposal['items'].values())
    result = project_api_connections(matrix, proposal)
    assert result['routes'] == matrix['routes'] and result['tiers'] == matrix['tiers']
    assert result['agents']['or-deepseek']['kind'] == 'api'
    assert 'secret-key' not in yaml.safe_dump(result)
    assert matrix == base()
    patched = propose_legacy_api_migration(matrix, {'openrouter': True}, {'agents': {'or-deepseek': {'model': 'custom-model'}}})
    assert patched['items']['or-deepseek']['model'] == 'custom-model'


def test_independent_consent_and_stable_identity():
    matrix = base()
    proposal = propose_legacy_api_migration(matrix, {'openrouter': True}, {})
    entry = proposal['items']['or-deepseek']
    entry['name'] = '新名称'
    entry['roles'] = ['manager', 'writing']
    result = apply_local_policy(project_api_connections(matrix, proposal), {}, sys.platform)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)/'matrix.yaml'
        p.write_text(yaml.safe_dump(result), encoding='utf8')
        router = Router(str(p), mock_all=True)
        assert router.agent_enabled('or-deepseek')
        assert not router.agent_enabled('cc-sonnet')
        assert router.agent_metadata('or-deepseek')['group'] == '新名称'
        assert [n for n, _ in router.chain_for(Task(id='w', title='w', type=TaskType.WRITING, prompt='w'))] == ['or-deepseek']
        assert 'or-deepseek' in result['routes']['manager'] and 'or-deepseek' not in result['routes']['coding']
        entry['enabled'] = False
        off = apply_local_policy(project_api_connections(matrix, proposal), {}, sys.platform)
        p.write_text(yaml.safe_dump(off), encoding='utf8')
        assert not Router(str(p), mock_all=True).agent_enabled('or-deepseek')


def test_migration_failure_retry_and_legacy_key_preserved():
    with tempfile.TemporaryDirectory() as td:
        config, creds = str(Path(td)/'project/config.yaml'), str(Path(td)/'credentials.yaml')
        Path(creds).write_text('OPENROUTER_API_KEY: fake-legacy\nDEEPSEEK_API_KEY: fake-dsh\n', encoding='utf8')
        store = ConnectionStore(config, creds)
        proposal = propose_legacy_api_migration(base(), {'openrouter': True}, {})
        for item in proposal['items'].values():
            item['credential_ref'] = 'legacy/OPENROUTER_API_KEY'
        real_replace = os.replace
        def fail(src, dest):
            if str(dest) == config:
                raise OSError('disk error')
            return real_replace(src, dest)
        with patch('hub.connection_store.os.replace', side_effect=fail):
            try:
                store.import_legacy(proposal, 0)
            except OSError:
                pass
            else:
                raise AssertionError('publish failure ignored')
        assert store.snapshot()['items'] == {}
        imported = store.import_legacy(proposal, 0)
        assert set(imported['items']) == set(proposal['items'])
        store.import_legacy(proposal, 1)
        assert len(store.snapshot()['items']) == len(proposal['items'])
        assert yaml.safe_load(Path(creds).read_text())['DEEPSEEK_API_KEY'] == 'fake-dsh'
        assert 'fake-' not in Path(config).read_text(encoding='utf8')


if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_'):
            fn()
            print('PASS', name)
