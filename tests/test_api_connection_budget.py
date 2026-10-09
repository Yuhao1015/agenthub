"""Paid scopes are local ledgers; every executor below is a fake."""
import asyncio
from pathlib import Path
import sys
import tempfile
import json
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from hub.api_budget import PaidLedger, BudgetedApiExecutor
from hub.executors import ExecResult
from hub.models import Task, TaskType
from hub.manager import manager_step
from hub.summarizer import summarize
from hub.blackboard import Blackboard
from hub.conversation import continue_task_conversation

TASK=Task(id='x',title='x',type=TaskType.LIGHT,prompt='fixture')

class Fake:
    def __init__(self,result): self.result=result;self.calls=0
    async def run(self,*args,**kwargs):
        self.calls+=1
        await asyncio.sleep(.02)
        return self.result


async def test_serial_cost_and_unknown_gate():
    ledger=PaidLedger(.5,spent_usd=.49)
    inner=Fake(ExecResult(ok=True,cost_usd=.02,usage={'cost_status':'reported'}))
    executor=BudgetedApiExecutor(inner,lambda:ledger)
    results=await asyncio.gather(executor.run(TASK,''),executor.run(TASK,''))
    assert sum(r.ok for r in results)==1 and inner.calls==1
    assert ledger.snapshot()['spent_usd']==.51
    ledger=PaidLedger(2)
    inner=Fake(ExecResult(ok=False,cost_usd=.04,error='partial',usage={'cost_status':'unknown'}))
    wrapped=BudgetedApiExecutor(BudgetedApiExecutor(inner,lambda:ledger),lambda:ledger)
    await wrapped.run(TASK,'');await wrapped.run(TASK,'')
    assert ledger.spent_usd==.04 and ledger.unknown and inner.calls==1
    separate=PaidLedger(2)
    await BudgetedApiExecutor(inner,lambda:separate).run(TASK,'')
    assert inner.calls==2


async def test_persistent_restore_reservation_and_no_request():
    with tempfile.TemporaryDirectory() as td:
        path=str(Path(td)/'ledger.json')
        ledger=PaidLedger(1,path=path)
        inner=Fake(ExecResult(ok=False,usage={'cost_status':'reported','request_sent':False}))
        result=await BudgetedApiExecutor(inner,lambda:ledger).run(TASK,'')
        assert not ledger.unknown and ledger.spent_usd==0
        ledger.record(ExecResult(ok=False,cost_usd=.3,usage={'cost_status':'estimated'}))
        restored=PaidLedger(1,path=path)
        assert restored.spent_usd==.3 and not restored.unknown
        ledger.reserve()
        interrupted=PaidLedger(1,path=path)
        assert interrupted.unknown,'A crash after request reservation cannot reset unknown cost'
        inner=Fake(ExecResult(ok=True))
        assert not (await BudgetedApiExecutor(inner,lambda:interrupted).run(TASK,'')).ok
        assert inner.calls==0


async def test_cancel_records_uncertain_charge():
    with tempfile.TemporaryDirectory() as td:
        ledger=PaidLedger(1,path=str(Path(td)/'ledger.json'))
        inner=Fake(ExecResult(ok=True))
        pending=asyncio.create_task(BudgetedApiExecutor(inner,lambda:ledger).run(TASK,''))
        await asyncio.sleep(.005);pending.cancel()
        try:await pending
        except asyncio.CancelledError:pass
        assert ledger.unknown
        assert PaidLedger(1,path=ledger.path).unknown


async def test_independent_routers_share_persisted_budget():
    with tempfile.TemporaryDirectory() as td:
        path=str(Path(td)/'ledger.json')
        first=PaidLedger(.5,.49,path=path);first.persist()
        second=PaidLedger(.5,path=path)
        inner=Fake(ExecResult(ok=True,cost_usd=.02,usage={'cost_status':'reported'}))
        a,b=await asyncio.gather(BudgetedApiExecutor(inner,lambda:first).run(TASK,''),
                                 BudgetedApiExecutor(inner,lambda:second).run(TASK,''))
        assert sum([a.ok,b.ok])==1 and inner.calls==1
        assert PaidLedger(.5,path=path).spent_usd==.51


async def test_manager_summary_and_task_share_one_gate():
    ledger=PaidLedger(.5)
    inner=Fake(ExecResult(ok=True,output='{"status":"ready","goal":"fixture"}',cost_usd=.3,usage={'cost_status':'reported'}))
    wrapped=BudgetedApiExecutor(inner,lambda:ledger)
    class Routes:
        def chain_for(self,task):return [('api-fixture',wrapped)]
        def open_circuit(self,name):pass
    router=Routes()
    step=await manager_step([{'role':'user','text':'fixture'}],router,'.')
    assert step['_cost_usd']==.3
    summary=await summarize('x'*5000,'fixture',router)
    assert summary[2]==.3
    assert not (await wrapped.run(TASK,'')).ok
    assert ledger.spent_usd==.6 and inner.calls==2


async def test_api_followup_without_cli_session_uses_paid_scope():
    with tempfile.TemporaryDirectory() as td:
        board=Blackboard(str(Path(td)/'board.jsonl'),on_write=None)
        board.put('tasks/x/agent','api-fixture',author='engine')
        board.put('tasks/x/spec',{'prompt':'previous task'},author='engine')
        board.put('tasks/x/output','previous answer',author='api-fixture',task_id='x')
        ledger=PaidLedger(.2)
        inner=Fake(ExecResult(ok=True,output='follow-up answer',cost_usd=.2,usage={'cost_status':'reported'}))
        wrapped=BudgetedApiExecutor(inner,lambda:ledger)
        class Routes:
            def kind(self,name):return 'api'
            def executor_for(self,name):return wrapped
        result,compacted=await continue_task_conversation('x','continue',Routes(),board)
        assert result.ok and not compacted and ledger.spent_usd==.2
        assert board.get('tasks/x/output')=='follow-up answer'
        result,_=await continue_task_conversation('x','again',Routes(),board)
        assert not result.ok and inner.calls==1


async def test_manager_to_run_transfer_once_and_resume():
    import main as cli
    from hub.api_budget import board_budget_path
    with tempfile.TemporaryDirectory() as td:
        source=str(Path(td)/'manager-paid.json');seed=PaidLedger(2,.2,True,path=source);seed.persist()
        plan=Path(td)/'plan.json'
        plan.write_text(json.dumps({'goal':'fixture','tasks':[{'id':'x','title':'x','type':'light','prompt':'fixture','depends_on':[]}]}))
        board=str(Path(td)/'board.jsonl');events=str(Path(td)/'events.jsonl')
        argv=['main.py','--plan',str(plan),'--mock','--no-summarize','--workdir',td,'--board',board,'--events',events,'--paid-budget-source',source]
        for resume in (False,True):
            with patch.object(sys,'argv',argv+(['--resume'] if resume else [])):
                assert await cli.amain()==0
            state=PaidLedger(2,path=board_budget_path(board)).snapshot()
            assert state=={'spent_usd':.2,'unknown':True}
            finished=[json.loads(line) for line in Path(events).read_text(encoding='utf8').splitlines() if 'run_finished' in line][-1]
            assert finished['paid_api_cost_usd']==.2 and finished['cost_complete'] is False


async def main():
    for fn in (test_serial_cost_and_unknown_gate,test_persistent_restore_reservation_and_no_request,test_cancel_records_uncertain_charge,test_independent_routers_share_persisted_budget,test_manager_summary_and_task_share_one_gate,test_api_followup_without_cli_session_uses_paid_scope,test_manager_to_run_transfer_once_and_resume):
        await fn();print('PASS',fn.__name__)


def test_saved_connection_test_requires_explicit_cost_consent():
    import serve
    from test_local_agent_settings import app
    from test_gate_serve import _req
    from test_api_connection_store import row
    calls=[]
    class Probe:
        def __init__(self,connection,secret_provider,**kw):
            self.connection=connection
        async def run(self,task,context,on_event=None):
            calls.append((task.prompt,context,self.connection['max_output_tokens']))
            return ExecResult(ok=True,output='ok',usage={'cost_status':'unknown'})
    with app() as (port,_), patch.object(serve,'ApiExecutor',Probe):
        store=serve._api_store();store.save(row(),'replace','fake-test',0)
        code,_=_req(port,'POST','/settings/api-connections/test',{'id':'api-a','revision':1})
        assert code==400 and not calls
        code,_=_req(port,'POST','/settings/api-connections/test',{'id':'api-a','revision':0,'confirm_cost':True})
        assert code==409 and not calls
        code,result=_req(port,'POST','/settings/api-connections/test',{'id':'api-a','revision':1,'confirm_cost':True})
        assert code==200 and result['ok'] and result['cost_complete'] is False
        assert calls==[('Reply with exactly: ok','',16)]
        assert 'fake-test' not in str(result)
        sess=serve._new_manager_session('abcdef123456')
        router=serve._session_router(sess)
        router.paid_ledger.record(ExecResult(ok=False,cost_usd=.1,usage={'cost_status':'unknown'}))
        sess.pop('router')
        restored=serve._session_router(sess).paid_ledger
        assert restored.spent_usd==.1 and restored.unknown
        assert serve._session_public(sess)['cost_complete'] is False
        assert serve._manager_budget_argv(sess['session_id'])

if __name__=='__main__':
    asyncio.run(main())
    test_saved_connection_test_requires_explicit_cost_consent()
    print('PASS explicit connection test consent')
