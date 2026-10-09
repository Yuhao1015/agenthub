import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serve
from hub.models import Task, TaskType, Plan
from hub.blackboard import Blackboard
from test_gate_serve import _snap, _restore, _start, _req

def test_roundtrip():
    t = Task(id='t1', title='t', type=TaskType.CODING, prompt='p', pinned_agent='cc-sonnet', execution_options={'model':'custom', 'effort':'high'})
    p = Plan.from_json({'goal':'g', 'tasks':[t.to_dict()]})
    assert p.tasks['t1'].execution_options == t.execution_options
    with tempfile.TemporaryDirectory() as td:
        b = Blackboard(os.path.join(td,'b.jsonl'), on_write=lambda e:None)
        b.put('tasks/t1/spec',t.to_dict(),author='system')
        assert Plan.from_blackboard(b).tasks['t1'].execution_options == t.execution_options

def test_gate_options():
    saved = _snap()
    with tempfile.TemporaryDirectory() as td:
        server, port = _start(td)
        try:
            b = Blackboard(serve.BOARD_PATH,on_write=lambda e:None)
            meta = {'name':'cc-sonnet', 'kind':'claude_code', 'effort_supported':True}
            b.put('run/gate/plan',{'kind':'plan','gate_token':'a'*16,'available_agents':[meta]},author='engine')
            b.put('tasks/t1/spec',{'type':'coding'},author='system')
            base = {'gate_id':'plan','gate_token':'a'*16,'decision':'approve','overrides':{'t1':'cc-sonnet'}}
            for opts in ({'ghost':{'model':'x'}}, {'t1':{'allowed_tools':'Bash'}}, {'t1':{'model':'bad model'}}):
                code, data = _req(port,'POST','/gate',dict(base,execution_options=opts))
                assert code == 400, (code,data)
            code, data = _req(port,'POST','/gate',dict(base,overrides={},execution_options={'t1':{'model':'x'}}))
            assert code == 400, (code,data)
            code, data = _req(port,'POST','/gate',dict(base,overrides={'t1':'cc-opus'}))
            assert code == 400, (code,data)
            code, data = _req(port,'POST','/gate',dict(base,execution_options={'t1':{'model':'custom','effort':'high'}}))
            assert code == 200, (code,data)
            fresh = Blackboard(serve.BOARD_PATH,on_write=lambda e:None)
            assert fresh.get('run/gate_reply/plan')['execution_options']['t1']['effort'] == 'high'
        finally:
            server.shutdown(); server.server_close(); _restore(saved)

if __name__ == '__main__':
    test_roundtrip(); test_gate_options()
    print('Task execution options tests passed')
