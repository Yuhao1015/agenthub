"""Cached models feed all selectors and validation without launching a CLI."""
import copy
import sys
from pathlib import Path
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).parent)]
import serve
from test_local_agent_settings import app
from test_local_agent_inspection import enable, RESULT
from test_gate_serve import _req

def test_directory_is_cache_only_and_validates_current_capability():
    result=copy.deepcopy(RESULT);result['catalog']['models'][0]['effort_levels']=['high']
    with app() as (port,_),patch.object(serve,'inspect_agent',return_value=result) as inspect:
        revision=enable(port)
        assert _req(port,'POST','/settings/local-agents/check',{'kind':'codex','revision':revision})[0]==200
        status,data=_req(port,'GET','/agents')
        assert status==200
        agent=next(a for a in data['agents'] if a['kind']=='codex')
        assert agent['runtime_model_catalog'][0]['id']=='actual-model'
        assert agent['runtime_model_capabilities']['actual-model']['effort_levels']==['high']
        assert inspect.call_count==1
        assert _req(port,'POST','/settings/agent-options',{'agent':agent['name'],'options':{'model':'actual-model','effort':'low'}})[0]==400
        assert _req(port,'POST','/settings/agent-options',{'agent':agent['name'],'options':{'model':'actual-model','effort':'high'}})[0]==200
        # Saving defaults changes the identity; old directory must disappear.
        agent=next(a for a in _req(port,'GET','/agents')[1]['agents'] if a['name']==agent['name'])
        assert agent['runtime_model_catalog']==[]
        assert inspect.call_count==1
    print('PASS cached runtime directory and capability validation')

if __name__=='__main__':test_directory_is_cache_only_and_validates_current_capability()
