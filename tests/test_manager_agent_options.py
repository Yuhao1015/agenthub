import os, sys, tempfile, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serve
from hub import manager
from hub.executors import ExecResult
from test_gate_serve import _snap, _restore, _start, _req

def test_manager_settings():
    saved=_snap()
    with tempfile.TemporaryDirectory() as td:
        server, port=_start(td,with_run=False)
        try:
            prefs={'manager_agent':'cc-sonnet-mgr','manager_model':'custom','manager_effort':'high','manager_model_mode':'explicit','manager_effort_mode':'explicit'}
            code,data=_req(port,'POST','/settings/config',{'ui_prefs':prefs})
            assert code==200,(code,data)
            assert serve._manager_execution_options(serve._ui_prefs())=={'model':'custom','effort':'high'}
            session={}; old=serve._session_router(session)
            old.open_circuit('cc-opus')
            old_executor=old.executor_for('cc-sonnet-mgr')
            code,data=_req(port,'POST','/settings/agent-options',{'agent':'cc-sonnet-mgr','options':{'model':'new-default'}})
            assert code==200,(code,data)
            new=serve._session_router(session)
            assert new is not old and old_executor.model=='sonnet'
            assert new.executor_for('cc-sonnet-mgr').model=='new-default'
            assert 'cc-opus' in new.circuit_open()
            code,data=_req(port,'POST','/settings/config',{'ui_prefs':{'manager_agent':''}})
            assert code==200 and serve._manager_execution_options(serve._ui_prefs())=={}
        finally:
            server.shutdown(); server.server_close(); _restore(saved)

def test_manager_turn_snapshot():
    captured=[]
    class Executor:
        async def run(self,*args,**kwargs):
            return ExecResult(ok=True,output='{"status":"asking","question":"q"}')
    class Router:
        def chain_for(self,task): return [('mgr',Executor())]
        def executor_with_options(self,name,options):
            captured.append(dict(options)); return Executor()
    asyncio.run(manager.manager_step([{'role':'user','text':'hello'}],Router(),'.',execution_options={'model':'custom','effort':'high'}))
    asyncio.run(manager.manager_step([{'role':'user','text':'again'}],Router(),'.',execution_options={'model':'new','effort':'low'}))
    assert captured==[{'model':'custom','effort':'high'},{'model':'new','effort':'low'}],captured

if __name__=='__main__':
    test_manager_settings(); test_manager_turn_snapshot()
    print('Manager agent options tests passed')
