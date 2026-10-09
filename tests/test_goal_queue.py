"""Durable identities, FIFO and no replay without evidence. No model calls."""
import os, sys, tempfile, json, concurrent.futures
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from hub.goal_queue import GoalStore, GoalConflict, GoalDataError

def rejects(kind, fn):
    try: fn()
    except kind: return
    raise AssertionError('expected ' + kind.__name__)

def test_queue():
    with tempfile.TemporaryDirectory(prefix='团队 A ') as root:
        path=os.path.join(root,'goals.json'); s=GoalStore(path)
        c={'workdir':root,'prefs':{},'binding':'a'*64}
        a,b='a'*12,'b'*12
        s.enqueue(a,'A',c,1); s.enqueue(b,'B',c,2)
        rev=s.snapshot()['revision']
        assert s.enqueue(a,'A',dict(c,binding='b'*64),3)['id']==a
        assert s.snapshot()['revision']==rev
        rejects(GoalConflict,lambda:s.enqueue(a,'other',c,3))
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results=list(pool.map(s.claim_next,[4,5]))
        assert sum(x is not None for x in results)==1
        assert s.get(a)['state']=='processing'
        s.complete_turn(a,a,200,{'status':'confirm','goal':'A'},6)
        assert s.claim_next(7) is None
        s.submit_turn(a,'c'*12,'确认',{},8)
        s.submit_turn(a,'c'*12,'确认',{},9)
        rejects(GoalConflict,lambda:s.submit_turn(a,'c'*12,'别的',{},9))
        assert s.claim_next(10)['pending_turn']['id']=='c'*12
        s.bind_run(a,'12345678',11)
        rejects(GoalConflict,lambda:s.bind_run(a,'87654321',11))
        s.complete_turn(a,'c'*12,200,{'status':'started'},12)
        assert s.claim_next(13) is None
        s.finish_run(a,'done',14)
        assert s.claim_next(15)['id']==b
        rejects(GoalConflict,lambda:s.submit_turn(a,'d'*12,'late',{},16))
        assert GoalStore(path).snapshot()==s.snapshot()
        data=json.load(open(path,encoding='utf-8'))
        for mutate in [lambda d:d.update(extra=1), lambda d:d['items'][0].update(state='bad'),
                       lambda d:d['items'][0].update(created_at=float('nan')),
                       lambda d:d['items'][0]['context'].update(api_key='secret')]:
            d=json.loads(json.dumps(data)); mutate(d)
            open(path,'w',encoding='utf-8').write(json.dumps(d))
            before=open(path,'rb').read()
            rejects(GoalDataError,lambda:s.enqueue('e'*12,'E',c,20))
            assert open(path,'rb').read()==before
        open(path,'w').write('{ broken')
        rejects(GoalDataError,s.snapshot)

def test_cancel_and_bounds():
    with tempfile.TemporaryDirectory() as root:
        s=GoalStore(os.path.join(root,'q.json')); c={'workdir':root,'prefs':{},'binding':'a'*64}
        s.enqueue('a'*12,'A',c,1); s.enqueue('b'*12,'B',c,2)
        s.cancel('a'*12,3); assert s.claim_next(4)['id']=='b'*12
        rejects(ValueError,lambda:s.enqueue('c'*12,'x'*20001,c,5))
        rejects(ValueError,lambda:s.enqueue('x'*12,'X',c,5))
        rejects(GoalConflict,lambda:s.cancel('b'*12,6))
        s.complete_turn('b'*12,'b'*12,500,{'error':'raw secret'},7)
        assert s.get('b'*12)['state']=='interrupted'
        assert 'raw secret' not in json.dumps(s.snapshot())
        s.retry('b'*12,c,8)
        assert s.get('b'*12)['state']=='waiting'

if __name__=='__main__':
    test_queue(); test_cancel_and_bounds(); print('goal queue: PASS')
