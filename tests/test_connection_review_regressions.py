"""Whole-branch review regressions: fake models, temporary settings only."""
import asyncio
import contextlib
import json
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).parent)]
import httpx
import serve
import main as cli
from hub.api_budget import PaidLedger, BudgetedApiExecutor
from hub.api_executor import ApiExecutor
from hub.blackboard import Blackboard
from hub.executors import ExecResult, _codex_cmd, _resolve_codebuddy_paths
from hub.models import EventBus
from test_api_executor import connection, factory, Bytes, TASK
from test_api_connection_endpoints import connection as form, save
from test_api_connection_store import row
from test_local_agent_settings import app
from test_gate_serve import _req, authorize_local_agents


def test_partial_stream_usage_blocks_next_paid_call():
    async def run():
        frames=[{'type':'message_start','message':{'usage':{'input_tokens':10,'output_tokens':0}}},
                {'type':'content_block_delta','delta':{'type':'text_delta','text':'partial'}},
                {'type':'error','error':{'type':'overloaded_error'}}]
        stream=''.join('data: '+json.dumps(x)+'\n\n' for x in frames)
        calls=[]
        def response(req):calls.append(req);return httpx.Response(200,stream=Bytes(stream))
        inner=ApiExecutor(dict(connection('anthropic'),pricing={'input':2,'output':4}),lambda ref:'fake',True,factory(response))
        ledger=PaidLedger(2)
        executor=BudgetedApiExecutor(inner,lambda:ledger)
        result=await executor.run(TASK,'')
        assert not result.ok and result.usage['cost_status']=='unknown'
        assert result.cost_usd>0,'Keep known input expense'
        assert not (await executor.run(TASK,'')).ok and len(calls)==1
    asyncio.run(run())


def historical_task(agent='cc-sonnet'):
    serve._runs_add('history','old goal');serve._adopt_latest_run()
    Path(serve.BOARD_PATH).parent.mkdir(parents=True,exist_ok=True)
    board=Blackboard(serve.BOARD_PATH)
    for key,value in {'spec':{'title':'old','prompt':'old'},'agent':agent,'session_id':'old',
                      'status':'done','output':'old answer'}.items():
        board.put('tasks/t/'+key,value,author='engine')
    return board


def test_corrupt_source_blocks_historical_local_call():
    with app() as (port,_):
        authorize_local_agents();serve._write_merged_matrix();historical_task()
        Path(serve.CONFIG_PATH).write_text('broken: [',encoding='utf8')
        async def called(*a,**kw):raise AssertionError('must not invoke a local agent')
        with patch.object(serve.conversation,'continue_task_conversation',called):
            code,data=_req(port,'POST','/task/t/message',{'message':'again'})
        assert code==409,(code,data)


def test_saved_discovery_is_the_runtime_argv():
    with app() as (port,discover):
        expected={'codex':['C:/fixture/desktop/codex.exe'], 'claude_code':['C:/fixture/claude.exe'],
                  'codebuddy':['C:/fixture/node.exe','C:/fixture/codebuddy.js']}
        discover.side_effect=lambda kind,overrides=None:dict(kind=kind,status='found',argv=expected[kind],source='fixture',detail='found',candidates=[])
        state=_req(port,'GET','/settings/local-agents')[1]['settings'];state['confirmed']=True
        for item in state['agents'].values():item['enabled']=True
        assert _req(port,'POST','/settings/local-agents',{'settings':state,'expected_revision':0})[0]==200
        specs=serve._load_yaml_dict(serve.MATRIX)['agents']
        assert _codex_cmd(specs['codex-local'].get('codex_path'))==expected['codex']
        buddy=next(v for v in specs.values() if v['kind']=='codebuddy')
        assert list(_resolve_codebuddy_paths(buddy.get('node_path'),buddy.get('script_path')))==expected['codebuddy']


def test_deleted_pinned_manager_returns_json_conflict():
    with app() as (port,_):
        save(port,form(),0);state=serve._api_store().snapshot();first=next(iter(state['items']))
        save(port,form(),1)
        cfg=serve._load_config();cfg['ui_prefs']={'manager_agent':first};serve._save_config(cfg)
        assert _req(port,'POST','/settings/api-connections/delete',{'id':first,'revision':2})[0]==200
        code,data=_req(port,'POST','/chat',{'message':'hello'})
        assert code==409 and '经理' in data['error'],(code,data)


def followup_cost_survives_export(existing_ledger):
    with app() as (port,_):
        save(port,form(),0);agent=next(iter(serve._api_store().snapshot()['items']))
        board=historical_task(agent)
        with contextlib.closing(EventBus(serve.EVENTS_PATH)) as bus:
            bus.emit('run_finished',total_cost_usd=.3,paid_api_cost_usd=.1,statuses={'t':'done'})
        if existing_ledger:
            ledger=PaidLedger(2,.1,path=serve.board_budget_path(board.path));ledger.persist()
        async def follow(task,message,router,board):
            router.paid_ledger.record(ExecResult(ok=False,cost_usd=.2,usage={'cost_status':'unknown'}))
            return ExecResult(ok=False,error='fixture',cost_usd=.2,usage={'cost_status':'unknown'}),False
        with patch.object(serve.conversation,'continue_task_conversation',follow):
            assert _req(port,'POST','/task/t/message',{'message':'again'})[0]==502
        events=[json.loads(line) for line in Path(serve.EVENTS_PATH).read_text(encoding='utf8').splitlines()]
        updates=[e for e in events if e['type']=='api_cost_updated']
        assert updates and updates[-1]['cost_complete'] is False
        assert abs(updates[-1]['paid_api_cost_usd']-.3)<1e-8
        assert abs(updates[-1]['total_cost_usd']-.5)<1e-8
        summary=serve._render_export_summary('history',board,events,[],[],[])
        assert '费用未知' in summary and '$0.5000' in summary


def test_followup_cost_survives_export():
    followup_cost_survives_export(True)


def test_historical_cost_seeds_missing_ledger():
    followup_cost_survives_export(False)


def test_failed_planning_persists_unknown_cost():
    async def run():
        with tempfile.TemporaryDirectory() as td:
            events=Path(td)/'events.jsonl'
            async def fail(goal,router,*a,**kw):
                router.paid_ledger.record(ExecResult(ok=False,cost_usd=.12,usage={'cost_status':'unknown'}))
                raise RuntimeError('fixture')
            argv=['main.py','--mock','--goal','fixture','--workdir',td,'--board',str(Path(td)/'board.jsonl'),'--events',str(events)]
            with patch.object(sys,'argv',argv),patch.object(cli,'make_plan',fail):assert await cli.amain()==1
            rows=[json.loads(x) for x in events.read_text(encoding='utf8').splitlines()]
            assert any(r.get('cost_complete') is False and r.get('total_cost_usd')==.12 for r in rows)
    asyncio.run(run())


def test_legacy_config_transaction_preserves_concurrent_api_save():
    with app() as (port,_):
        ready,release,stored=threading.Event(),threading.Event(),threading.Event()
        original=serve._save_config;errors=[]
        def delayed(cfg):ready.set();assert release.wait(5);original(cfg)
        def legacy():
            try:assert _req(port,'POST','/settings/config',{'budget':3})[0]==200
            except Exception as e:errors.append(e)
        def api():
            try:serve._api_store().save(row(),'replace','fake',0)
            except Exception as e:errors.append(e)
            finally:stored.set()
        with patch.object(serve,'_save_config',delayed):
            a=threading.Thread(target=legacy);a.start();assert ready.wait(3)
            b=threading.Thread(target=api);b.start();stored.wait(.2);release.set();a.join(5);b.join(5)
        assert not errors,errors
        assert serve._api_store().snapshot()['items'] and serve._load_config()['budget']['max_usd_per_run']==3


def test_interrupted_cli_manager_keeps_paid_scope():
    async def run():
        with tempfile.TemporaryDirectory() as td:
            original=cli.Router;seen=[]
            async def interrupt(ask,router,*a,**kw):
                seen.append(router.paid_ledger.snapshot())
                if len(seen)==1:router.paid_ledger.record(ExecResult(ok=False,cost_usd=.2,usage={'cost_status':'unknown'}))
                raise RuntimeError('fixture interruption')
            argv=['main.py','--interactive','--goal','fixture','--workdir',td,'--board',str(Path(td)/'board.jsonl'),'--events',str(Path(td)/'events.jsonl')]
            with patch.object(sys,'argv',argv),patch.object(cli,'Router',lambda matrix,**kw:original(matrix,workdir=td,mock_all=True)),patch('hub.manager.run_clarification_loop',interrupt):
                assert await cli.amain()==1
                assert await cli.amain()==1
            assert seen[1]=={'spent_usd':.2,'unknown':True},seen
    asyncio.run(run())


def test_engine_includes_other_router_cost():
    from hub.engine import Engine
    from hub.models import Plan
    from test_paid_budget import _make_router
    with tempfile.TemporaryDirectory() as td:
        ledger=PaidLedger(2,.1,path=str(Path(td)/'paid.json'));ledger.persist()
        router=_make_router({});router.paid_ledger=ledger
        events=Path(td)/'events.jsonl'
        with contextlib.closing(EventBus(str(events))) as bus:
            engine=Engine(Plan(goal='fixture',tasks={}),router,bus,
                          Blackboard(str(Path(td)/'board.jsonl')),preflight=False)
            other=PaidLedger(2,path=ledger.path)
            other.record(ExecResult(ok=False,cost_usd=.2,usage={'cost_status':'unknown'}))
            asyncio.run(engine.run())
        finished=[json.loads(x) for x in events.read_text(encoding='utf8').splitlines() if 'run_finished' in x][-1]
        assert abs(finished['paid_api_cost_usd']-.3)<1e-8 and abs(finished['total_cost_usd']-.3)<1e-8
        assert finished['cost_complete'] is False


def test_run_finish_cannot_overwrite_concurrent_followup_cost():
    from hub.engine import Engine
    from hub.models import Plan
    from hub.connection_store import file_lock
    from test_paid_budget import _make_router
    with tempfile.TemporaryDirectory() as td:
        ledger=PaidLedger(2,.1,path=str(Path(td)/'paid.json'));ledger.persist()
        router=_make_router({});router.paid_ledger=ledger
        events=Path(td)/'events.jsonl';workers=[];done=threading.Event()
        with contextlib.closing(EventBus(str(events))) as bus:
            original=bus.emit
            def followup():
                with file_lock(ledger.path + '.events'):
                    other=PaidLedger(2,path=ledger.path)
                    other.record(ExecResult(ok=True,cost_usd=.2,usage={'cost_status':'reported'}))
                    original('api_cost_updated',total_cost_usd=.3,paid_api_cost_usd=.3,cost_complete=True)
                done.set()
            def emit(kind,**payload):
                if kind=='run_finished':
                    worker=threading.Thread(target=followup);workers.append(worker);worker.start()
                    done.wait(.2)
                original(kind,**payload)
            with patch.object(bus,'emit',emit):
                asyncio.run(Engine(Plan(goal='fixture',tasks={}),router,bus,
                                   Blackboard(str(Path(td)/'board.jsonl')),preflight=False).run())
            for worker in workers:worker.join(3)
            assert done.is_set()
        costs=[json.loads(x) for x in events.read_text(encoding='utf8').splitlines()
               if json.loads(x)['type'] in ('run_finished','api_cost_updated')]
        assert abs(costs[-1]['total_cost_usd']-.3)<1e-8,costs


if __name__=='__main__':
    failed=[]
    for name,fn in list(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:fn();print('PASS',name)
            except Exception:failed.append(name);traceback.print_exc()
    if failed:raise SystemExit('FAILED: '+', '.join(failed))
