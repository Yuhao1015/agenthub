import sys, tempfile, os, copy
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).parent)]
from hub.goal_queue import GoalStore, GoalConflict

def test_evidence():
    for proof,expected in [({},'interrupted'),
        ({'session':{'session_id':'a'*12,'history':[{'role':'user','text':'A','ts':2},
          {'role':'manager','text':'确认','ts':3}], 'awaiting_confirmation':True,
          'proposal':{'goal':'A','constraints':''},'cost_usd':0}},'awaiting_input'),
        ({'session':{'session_id':'b'*12,'history':[{'role':'user','text':'A','ts':2},
          {'role':'manager','text':'确认','ts':3}]}},'interrupted')]:
        with tempfile.TemporaryDirectory() as root:
            s=GoalStore(os.path.join(root,'q.json'));c={'workdir':root,'prefs':{},'binding':'a'*64}
            s.enqueue('a'*12,'A',c,1);s.claim_next(2);s.recover({'a'*12:proof},4)
            assert s.get('a'*12)['state']==expected,s.get('a'*12)
            before=s.snapshot();s.recover({'a'*12:proof},5);assert s.snapshot()==before

def test_bound_run_never_replayed():
    for run_state in ('running','done','failed','interrupted'):
        with tempfile.TemporaryDirectory() as root:
            s=GoalStore(os.path.join(root,'q.json'));c={'workdir':root,'prefs':{},'binding':'a'*64}
            s.enqueue('a'*12,'A',c,1);s.claim_next(2);s.bind_run('a'*12,'12345678',3)
            s.recover({'a'*12:{'run_id':'12345678','run_state':run_state}},4)
            assert s.get('a'*12)['state']==run_state
            assert s.claim_next(5) is None
            try:s.retry('a'*12,c,6)
            except GoalConflict:pass
            else:raise AssertionError('bound run replayed')

if __name__=='__main__':test_evidence();test_bound_run_never_replayed();print('goal recovery: PASS')
