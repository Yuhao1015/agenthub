import sys, os
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).parent)]
import serve
from goal_queue_fixtures import queue_app
from test_gate_serve import _req
from hub.blackboard import Blackboard

def test_explicit_run():
    with queue_app() as port:
        boards=[]
        for rid in ('12345678','87654321'):
            directory=Path(serve.RUNS_DIR)/rid;directory.mkdir()
            board=Blackboard(str(directory/'blackboard.jsonl'),on_write=None)
            board.put('tasks/t1/spec',{'title':'same id'},author='system',kind='spec');boards.append(board)
        serve.BOARD_PATH=boards[1].path
        assert _req(port,'POST','/task/t1/message',{'run_id':'12345678','message':'old only'})[0]==200
        assert Blackboard(boards[0].path,on_write=None).get('tasks/t1/inbox/0')=='old only'
        assert Blackboard(boards[1].path,on_write=None).get('tasks/t1/inbox/0') is None
        for rid in ('abcdefab','../12345678'):
            assert _req(port,'POST','/task/t1/message',{'run_id':rid,'message':'X'})[0]==404

if __name__=='__main__':test_explicit_run();print('goal followup identity: PASS')
