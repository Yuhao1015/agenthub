"""HTTP connection CRUD uses only temporary fake credentials and stubbed execution."""
import sys
from pathlib import Path
from unittest.mock import patch
import json
import os
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).parent)]
import serve
from test_local_agent_settings import app
from test_gate_serve import _req


def connection(**kw):
    return dict(name='My <img src=x> API', format='openai_compatible',
                base_url='https://example.invalid/v1', model='test-model',
                enabled=True, roles=['manager'], **kw)


def save(port, item, revision, action='replace', key='fake-<&"$-key'):
    return _req(port,'POST','/settings/api-connections',dict(
        connection=item,expected_revision=revision,key_action=action,key_value=key))


def test_crud_redaction_and_live_policy():
    with app() as (port,_):
        code,state=_req(port,'GET','/settings/api-connections')
        assert code==200 and state['items']=={}, (code,state)
        assert save(port,connection(),0)[0]==200
        _,state=_req(port,'GET','/settings/api-connections')
        first=next(iter(state['items'].values()))
        assert first['id'].startswith('api-') and first['has_key'] and not first['verified']
        assert 'credential_ref' not in str(state) and 'fake-' not in str(state)
        assert save(port,connection(),1)[0]==200
        _,state=_req(port,'GET','/settings/api-connections')
        assert len(state['items'])==2
        raw={k:v for k,v in first.items() if k not in ('has_key','verified','test_result','impact')}
        assert save(port,raw,1,'keep','')[0]==409
        assert save(port,raw,2,'keep','')[0]==200
        raw['enabled']=False
        assert save(port,raw,3,'clear','')[0]==200
        assert not serve._api_store().snapshot()['items'][raw['id']]['has_key']
        with patch.object(serve,'_connection_inflight',1):
            assert _req(port,'POST','/settings/api-connections/delete',{'id':raw['id'],'revision':4})[0]==409
        assert _req(port,'POST','/settings/api-connections/delete',{'id':raw['id'],'revision':4})[0]==200
        assert raw['agent_id'] not in serve._load_yaml_dict(serve.MATRIX)['agents']


def test_invalid_requests_and_publish_failure():
    with app() as (port,_):
        assert save(port,connection(credential_ref='env/OTHER_SECRET'),0)[0]==400
        assert save(port,connection(id='api-forged',agent_id='cc-opus'),0)[0]==400
        item=connection();item['roles']=[{}]
        assert save(port,item,0)[0]==400
        item=connection();item['model']='two words'
        assert save(port,item,0)[0]==400
        assert _req(port,'POST','/settings/api-connections',{'connection':connection(),'expected_revision':0})[0]==400
        with patch.object(serve,'_write_merged_matrix',side_effect=OSError('fake secret internal path')):
            code,result=save(port,connection(),0)
            assert code==500 and 'fake secret' not in str(result)
        assert serve._api_store().snapshot()['items']=={}
        assert save(port,connection(),0)[0]==200
        creds=Path(serve.CREDENTIALS_PATH)
        creds.write_text('connections: []',encoding='utf8')
        code,result=_req(port,'GET','/settings/api-connections')
        assert code==409 and 'error' in result


def test_migration_and_revision_bound_test_status():
    from hub.executors import ExecResult
    class Probe:
        def __init__(self,*args,**kwargs):pass
        async def run(self,*args,**kwargs):return ExecResult(ok=True,output='ok',cost_usd=.001,usage={'cost_status':'reported'})
    with app() as (port,_), patch.dict(os.environ,{'OPENROUTER_API_KEY':'fake-legacy'}), patch.object(serve,'ApiExecutor',Probe):
        # Fixture base contains a legacy profile; no import on merely reading.
        matrix=serve._load_yaml_dict(serve.MATRIX_BASE)
        matrix.setdefault('agents',{})['old-api']={'kind':'openrouter','model':'old/model'}
        import yaml
        fixture_matrix=Path(serve.CONFIG_PATH).parent/'fixture-matrix.yaml'
        fixture_matrix.write_text(yaml.safe_dump(matrix),encoding='utf8')
        serve.MATRIX_BASE=str(fixture_matrix)
        _,state=_req(port,'GET','/settings/api-connections')
        assert 'old-api' in state['migration'] and not state['items']
        assert 'credential_ref' not in json.dumps(state)
        assert _req(port,'POST','/settings/api-connections/migrate',{'revision':0,'confirm':True})[0]==200
        state=serve._api_store().snapshot();assert state['items']['old-api']['agent_id']=='old-api'
        code,result=_req(port,'POST','/settings/api-connections/test',{'id':'old-api','revision':1,'confirm_cost':True})
        assert code==200 and result['ok']
        _,public=_req(port,'GET','/settings/api-connections')
        assert public['items']['old-api']['verified']
        item={k:v for k,v in state['items']['old-api'].items() if k not in ('has_key','verified')}
        item['model']='another-model'
        assert save(port,item,1,'keep','')[0]==200
        _,public=_req(port,'GET','/settings/api-connections')
        assert not public['items']['old-api']['verified']


def test_legacy_writer_preserves_private_api_credentials():
    from hub.connection_store import verify_private
    with app() as (port,_), patch.dict(os.environ,{},clear=False):
        assert save(port,connection(),0)[0]==200
        serve._credentials_write({'OPENROUTER_API_KEY':'fake-legacy'})
        verify_private(serve.CREDENTIALS_PATH)
        assert all(v['has_key'] for v in serve._api_store().snapshot()['items'].values())
        with patch.object(serve,'_credentials_write',side_effect=OSError('secret details')):
            code,result=_req(port,'POST','/settings/credentials',{'openrouter':'fake'})
            assert code==500 and 'secret details' not in str(result)


if __name__=='__main__':
    for fn in (test_crud_redaction_and_live_policy,test_invalid_requests_and_publish_failure,test_migration_and_revision_bound_test_status,test_legacy_writer_preserves_private_api_credentials):
        fn();print('PASS',fn.__name__)
