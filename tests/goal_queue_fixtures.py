"""Queue fixtures: all model/engine execution is fake, no worker threads."""
import contextlib, os, time, uuid
from unittest.mock import patch
import serve
from test_local_agent_settings import app
from test_gate_serve import _req, authorize_local_agents
from test_serve_chat import _stub_router

manager_calls=[]; spawned_calls=[]; manager_answers=[]; run_states={}

def submit(port,gid,message,workdir=None,prefs=None):
    return _req(port,'POST','/goals',{'request_id':gid,'message':message,
        'workdir':workdir or serve.WORKDIR,'prefs':prefs or dict(serve.UI_PREF_DEFAULTS)})

def fake_turn(body,sid,message,context=None):
    manager_calls.append(sid)
    return 200,{'status':'confirm','session_id':sid,'goal':message,'constraints':'','manager_cost_usd':0}

def fake_spawn(tail,context=None,owner_goal_id=''):
    assert all(v!='running' for v in run_states.values()), 'two live engines'
    spawned_calls.append((tail,context,owner_goal_id)); rid=uuid.uuid4().hex[:8]
    os.makedirs(os.path.join(serve.RUNS_DIR,rid))
    serve._run_meta={'id':rid}
    if owner_goal_id: serve._goal_store().bind_run(owner_goal_id,rid,time.time())
    run_states[rid]='running'
    return 4242

def tick_until(gid,state):
    for _ in range(10):
        serve._goal_tick()
        if serve._goal_store().get(gid)['state']==state: return
    raise AssertionError(serve._goal_store().get(gid))

@contextlib.contextmanager
def queue_app(real_manager=False):
    saved={key:getattr(serve,key) for key in ('_sessions','_active_sid','_chat_inflight','_progress')}
    stop=getattr(serve,'_goal_stop',None)
    serve._sessions={}; serve._active_sid=''; serve._chat_inflight=set(); serve._progress={}
    if stop is not None:
        import threading
        serve._goal_stop=threading.Event()
    for values in (manager_calls,spawned_calls,manager_answers,run_states): values.clear()
    async def step(history,router,workdir,**kwargs):
        manager_calls.append(history[-1]['text'])
        assert manager_answers, 'unscripted inference forbidden'
        return manager_answers.pop(0)
    try:
        with app() as (port,_), contextlib.ExitStack() as stack:
            authorize_local_agents()
            stack.enter_context(patch.object(serve,'Router',side_effect=_stub_router))
            stack.enter_context(patch.object(serve,'_spawn_main',side_effect=fake_spawn))
            stack.enter_context(patch.object(serve,'_goal_run_state',side_effect=lambda rid:run_states.get(rid,'interrupted'),create=True))
            if real_manager: stack.enter_context(patch.object(serve.manager,'manager_step',side_effect=step))
            else: stack.enter_context(patch.object(serve,'_manager_turn',side_effect=fake_turn))
            yield port
    finally:
        for key,value in saved.items(): setattr(serve,key,value)
        if stop is not None: serve._goal_stop=stop
