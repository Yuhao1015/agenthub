import sys, threading, time, tempfile, os
from pathlib import Path
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).parent)]
import serve
from goal_queue_fixtures import *
from test_gate_serve import _req

def reply(port,gid,tid='d'*12,message='开始'):
    return _req(port,'POST',f'/goals/{gid}/message',{'request_id':tid,'message':message,'choice_answers':{}})

def test_reservation_and_passive_requests():
    with queue_app() as port:
        submit(port,'a'*12,'A');submit(port,'b'*12,'B')
        with patch.object(serve,'_run_busy',return_value=True): serve._goal_tick()
        assert not manager_calls
        entered=threading.Event();release=threading.Event()
        def slow(*args,**kwargs):
            entered.set(); assert release.wait(5); return fake_turn(*args,**kwargs)
        with patch.object(serve,'_manager_turn',side_effect=slow):
            t=threading.Thread(target=serve._goal_tick);t.start();assert entered.wait(3)
            assert _req(port,'GET','/goals')[0]==200
            assert submit(port,'c'*12,'C')[0]==202
            assert _req(port,'POST','/shutdown',{})[0]==409
            assert _req(port,'POST','/chat',{'message':'legacy'})[0]==409
            assert serve._spawn_main_if_idle(['--mock','--goal=other']) is None
            assert _req(port,'POST','/goals/'+'a'*12+'/cancel',{})[0]==409
            serve._goal_tick(); assert not manager_calls
            release.set();t.join(5);assert not t.is_alive()
        assert manager_calls==['a'*12]
        assert serve._goal_store().get('a'*12)['state']=='awaiting_input'
        serve._goal_tick();assert len(manager_calls)==1
        assert _req(port,'POST','/goals/'+'a'*12+'/cancel',{})[0]==202
        serve._goal_tick(); assert manager_calls==['a'*12,'b'*12]
        try:
            with serve._connection_call(): raise AssertionError('test should be blocked')
        except serve.InspectionConflict:pass

def test_clarification_confirmation_and_checkpoints():
    with queue_app(real_manager=True) as port:
        manager_answers.extend([{'status':'asking','question':'哪个目录？'},
                                {'status':'ready','goal':'A','constraints':''},
                                {'status':'ready','goal':'B','constraints':''}])
        submit(port,'a'*12,'A');submit(port,'b'*12,'B')
        tick_until('a'*12,'awaiting_input'); assert not spawned_calls
        assert reply(port,'a'*12,message='使用原目录')[0]==202
        tick_until('a'*12,'awaiting_input'); assert not spawned_calls
        assert reply(port,'a'*12,tid='e'*12)[0]==202
        tick_until('a'*12,'running'); assert len(spawned_calls)==1
        rid=serve._goal_store().get('a'*12)['run_id'];run_states[rid]='done'
        serve._checkpoint_lock.acquire()
        try:serve._goal_tick(); assert serve._goal_store().get('a'*12)['state']=='running'
        finally:serve._checkpoint_lock.release()
        tick_until('a'*12,'done');tick_until('b'*12,'awaiting_input')
        assert len(spawned_calls)==1
        assert _req(port,'GET','/goals/'+'a'*12)[0]==200

def test_failure_and_configuration_pause():
    with queue_app() as port:
        submit(port,'a'*12,'A')
        with patch.object(serve,'_manager_turn',side_effect=RuntimeError('raw token')):serve._goal_tick()
        assert serve._goal_store().get('a'*12)['state']=='interrupted'
        serve._goal_tick();assert not manager_calls
        body={'request_id':'d'*12,'use_current_settings':True,'workdir':serve.WORKDIR,'prefs':{}}
        assert _req(port,'POST','/goals/'+'a'*12+'/retry',body)[0]==202
        tick_until('a'*12,'awaiting_input')
    with queue_app() as port:
        submit(port,'a'*12,'A');submit(port,'b'*12,'B')
        cfg=serve._load_config();cfg['budget']={'max_usd_per_run':4};serve._save_config(cfg);serve._write_merged_matrix()
        tick_until('a'*12,'blocked');assert not manager_calls
        serve._goal_tick();assert not manager_calls

def test_legacy_occupancy_and_shutdown():
    with queue_app() as port:
        submit(port,'a'*12,'A')
        with patch.object(serve,'_active_session_id',return_value='b'*12):serve._goal_tick()
        assert not manager_calls
        with patch.object(serve,'_connection_inflight',1):serve._goal_tick()
        assert not manager_calls
        stop=threading.Event()
        with patch.object(serve,'_goal_stop',stop):
            assert _req(port,'POST','/shutdown',{})[0]==200
            assert stop.is_set();serve._goal_tick();assert not manager_calls

def test_three_goal_end_to_end():
    with queue_app(real_manager=True) as port, tempfile.TemporaryDirectory() as root:
        ids=['a'*12,'b'*12,'c'*12];paths=[]
        for name,gid,cost in zip(('A','B','C'),ids,(.01,.02,.03)):
            workdir=os.path.join(root,'团队 '+name);os.mkdir(workdir);paths.append(os.path.realpath(workdir))
            assert submit(port,gid,name,workdir=workdir)[0]==202
            manager_answers.append({'status':'ready','goal':name,'constraints':'','_cost_usd':cost})
        assert not manager_calls and not spawned_calls
        cfg=serve._load_config();cfg['workdir']=root;cfg['ui_prefs']={'gate_mode':'ask'}
        serve._save_config(cfg);serve._write_merged_matrix()
        for gid,tid,cost in zip(ids,('d'*12,'e'*12,'f'*12),(.01,.02,.03)):
            tick_until(gid,'awaiting_input')
            record=serve._goal_store().get(gid);assert record['response']['manager_cost_usd']==cost
            assert len(spawned_calls)==ids.index(gid)
            assert reply(port,gid,tid)[0]==202;tick_until(gid,'running')
            assert serve._goal_store().get(gid)['response']['manager_cost_usd']==cost
            rid=serve._goal_store().get(gid)['run_id'];run_states[rid]='done';tick_until(gid,'done')
        assert manager_calls==['A','B','C']
        assert [serve._tail_goal(tail) for tail,context,owner in spawned_calls]==['A','B','C']
        assert [context['workdir'] for tail,context,owner in spawned_calls]==paths
        assert all('--gate-mode=auto' in tail for tail,context,owner in spawned_calls)

if __name__=='__main__':
    test_reservation_and_passive_requests();test_clarification_confirmation_and_checkpoints()
    test_failure_and_configuration_pause();test_legacy_occupancy_and_shutdown();test_three_goal_end_to_end();print('goal dispatch: PASS')
