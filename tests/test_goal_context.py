"""Captured execution context, without subprocesses or inference."""
import os, sys, tempfile
from pathlib import Path
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).parent)]
import serve
from test_serve_chat import isolated_chat_state, _stub_router
from test_local_agent_settings import app
from test_gate_serve import authorize_local_agents
from types import SimpleNamespace

def test_captured_context():
    calls=[]
    async def step(history,router,workdir,**kwargs):
        calls.append((workdir,kwargs)); return {'status':'ready','goal':'A','constraints':''}
    with isolated_chat_state(), tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b, \
         patch.object(serve,'Router',side_effect=_stub_router), \
         patch.object(serve.manager,'manager_step',side_effect=step), \
         patch.object(serve,'WORKDIR',b):
        prefs=dict(serve.UI_PREF_DEFAULTS,gate_mode='ask',assign_mode='manual')
        c=serve._capture_goal_context(a,prefs)
        status,result=serve._manager_turn({},'a'*12,'A',c)
        assert status==200 and result['status']=='confirm'
        assert calls[0][0]==os.path.realpath(a)
        assert serve._gate_argv(prefs=c['prefs'])==['--gate-mode=ask','--assign-mode=manual']
        assert set(c)=={'workdir','prefs','binding'}
        with patch.object(serve,'_goal_binding',return_value='b'*64):
            try: serve._validate_goal_context(c)
            except ValueError: pass
            else: raise AssertionError('config change accepted')
        assert len(calls)==1
        with patch.object(serve,'_spawn_manager_main_if_idle',return_value=(4242,'12345678')) as spawn:
            assert serve._manager_turn({},'a'*12,'对',c)[1]['status']=='started'
            assert spawn.call_args.kwargs['context']==c
            assert spawn.call_args.kwargs['owner_goal_id']=='a'*12

def test_path_and_prefs():
    with tempfile.TemporaryDirectory() as root:
        c=serve._capture_goal_context(root,{})
        assert c['prefs']==serve.UI_PREF_DEFAULTS
    try: serve._validate_goal_context(c)
    except ValueError: pass
    else: raise AssertionError('missing path accepted')
    try: serve._capture_goal_context(os.getcwd(),{'api_key':'secret'})
    except ValueError: pass
    else: raise AssertionError('secret preference accepted')

def test_engine_captured_path():
    with app(), tempfile.TemporaryDirectory() as original, tempfile.TemporaryDirectory() as later:
        authorize_local_agents()
        c=serve._capture_goal_context(original,{})
        with patch.object(serve,'WORKDIR',later), \
             patch.object(serve.subprocess,'Popen',return_value=SimpleNamespace(pid=4242)) as popen, \
             patch.object(serve.threading.Thread,'start'), patch.object(serve,'_prune_checkpoints'):
            serve._spawn_main(['--goal=A'],context=c)
        argv=popen.call_args.args[0]
        assert '--workdir='+os.path.realpath(original) in argv
        assert '--checkpoint-store='+serve.checkpoints.store_path(serve.RUNTIME_DIR,os.path.realpath(original)) in argv
        cfg=serve._load_config(); cfg['ui_prefs']={'gate_mode':'ask'}; cfg['workdir']=later
        serve._save_config(cfg); serve._write_merged_matrix()
        serve._validate_goal_context(c)

def test_disabled_api_manager_rejected_before_queue_acceptance():
    with app():
        authorize_local_agents()
        matrix={'agents':{'api-fixture':{'kind':'api','enabled':False,
                'connection':{'enabled':False,'credential_ref':''}}}}
        with patch.object(serve,'_load_yaml_dict',return_value=matrix):
            try:serve._capture_goal_context(serve.WORKDIR,{'manager_agent':'api-fixture'})
            except ValueError:pass
            else:raise AssertionError('disabled API manager accepted')

if __name__=='__main__':
    test_captured_context(); test_path_and_prefs(); test_engine_captured_path();test_disabled_api_manager_rejected_before_queue_acceptance(); print('goal context: PASS')
