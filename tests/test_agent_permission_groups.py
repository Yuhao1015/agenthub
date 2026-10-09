"""Grouping keeps per-profile ceilings. No CLI calls."""
import sys
from pathlib import Path
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).parent)]
import serve
from test_local_agent_settings import app
from test_gate_serve import _req
from test_api_connection_endpoints import connection,save

def test_groups_preserve_role_limits_and_readonly_kinds():
    with app() as (port,_):
        matrix=Path(serve.RUNTIME_DIR)/'roles.yaml'
        matrix.write_text('agents:\n  reader:\n    kind: claude_code\n    allowed_tools: Read\n  writer:\n    kind: claude_code\n    allowed_tools: Read, Edit\n  code:\n    kind: codex\n  buddy:\n    kind: codebuddy\n  api-one:\n    kind: api\n    label: My service\n',encoding='utf-8')
        with patch.object(serve,'MATRIX_BASE',str(matrix)):
            serve._write_merged_matrix()
            status,data=_req(port,'GET','/settings/tools')
            assert status==200
            groups=data['groups']
            claude=next(g for g in groups if g['kind']=='claude_code')
            by_name={p['name']:p for p in claude['profiles']}
            assert by_name['reader']['allowed']==['Read']
            assert by_name['writer']['allowed']==['Read','Edit']
            assert all(not g['editable'] for g in groups if g['kind']!='claude_code')
            status,saved=_req(port,'POST','/settings/tools',{'agent':'writer','allowed':['Read']})
            assert status==200 and 'groups' in saved
            assert serve._effective_allowed_tools('reader')[0]==['Read']
            assert serve._effective_allowed_tools('writer')[0]==['Read']
            assert _req(port,'POST','/settings/tools',{'agent':'reader','allowed':['Read','Bash']})[0]==400
            assert _req(port,'POST','/settings/tools',{'agent':'code','allowed':['Read']})[0]==400
            assert _req(port,'POST','/settings/tools',{'agent':'writer','allowed':[]})[1]['clamped']
    print('PASS groups preserve individual ceilings and read-only controls')

def test_user_api_groups_match_effective_connections():
    with app() as (port,_):
        assert save(port,connection(),0)[0]==200
        items=_req(port,'GET','/settings/api-connections')[1]['items']
        item=next(iter(items.values()))
        groups=_req(port,'GET','/settings/tools')[1]['groups']
        api=next(g for g in groups if g['kind']=='api')
        assert api['label']==item['name'] and not api['editable']
        assert api['profiles']==[{'name':item['agent_id']}]
        assert all(g['kind']!='openrouter' for g in groups)
    print('PASS user API permission groups match effective matrix')

if __name__=='__main__':
    test_groups_preserve_role_limits_and_readonly_kinds()
    test_user_api_groups_match_effective_connections()
