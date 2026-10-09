import os, sys, threading
from pathlib import Path
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).parent)]
import serve
from goal_queue_fixtures import queue_app,submit,manager_calls,spawned_calls
from test_gate_serve import _req

def test_passive_idempotent_submission():
    with queue_app() as port, patch.object(serve,'_run_busy',return_value=True):
        a='a'*12
        assert submit(port,a,'A')[0]==202
        assert submit(port,a,'A')[0]==202
        assert submit(port,a,'changed')[0]==409
        codes=[]
        ts=[threading.Thread(target=lambda:codes.append(submit(port,'b'*12,'B')[0])) for _ in range(2)]
        for t in ts:t.start()
        for t in ts:t.join()
        assert codes==[202,202]
        cfg=serve._load_config();cfg['budget']={'max_usd_per_run':7};serve._save_config(cfg);serve._write_merged_matrix()
        assert submit(port,a,'A')[0]==202
        code,data=_req(port,'GET','/goals'); assert code==200 and len(data['items'])==2
        assert _req(port,'GET','/goals/'+a)[1]['state']=='waiting'
        assert not manager_calls and not spawned_calls
        assert _req(port,'POST','/goals/'+a+'/message',{'request_id':'c'*12,'message':'回复','choice_answers':{}})[0]==409
        assert _req(port,'POST','/goals/'+a+'/cancel',{})[0]==202
        assert _req(port,'POST','/goals/'+a+'/cancel',{'extra':1})[0]==400

def test_invalid_and_corrupt_preserved():
    with queue_app() as port:
        assert submit(port,'x'*12,'X')[0]==400
        assert submit(port,'a'*12,'')[0]==400
        assert submit(port,'a'*12,'A',prefs={'api_key':'x'})[0]==400
        assert _req(port,'POST','/goals',{'extra':1})[0]==400
        assert _req(port,'GET','/goals/'+'b'*12)[0]==404
        assert submit(port,'a'*12,'A')[0]==202
        path=Path(serve.RUNTIME_DIR)/'goals.json';path.write_text('{ broken',encoding='utf-8');before=path.read_bytes()
        assert _req(port,'GET','/goals')[0]==500
        assert submit(port,'b'*12,'B')[0]==500
        assert path.read_bytes()==before

if __name__=='__main__':
    test_passive_idempotent_submission();test_invalid_and_corrupt_preserved();print('goal endpoints: PASS')
