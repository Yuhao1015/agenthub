"""Final-review regressions; only fake inference, temporary records and local HTTP."""
import json, os, sys, traceback
from pathlib import Path
from unittest.mock import patch
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).parent)]
import serve
from goal_queue_fixtures import queue_app, submit, manager_calls, tick_until
from test_gate_serve import _req
from hub.blackboard import Blackboard
from hub.executors import ExecResult

real_run_state = serve._goal_run_state

def test_bound_interruption_can_be_explicitly_resolved_without_replay():
    with queue_app() as port:
        submit(port, 'a'*12, 'A'); submit(port, 'b'*12, 'B')
        store = serve._goal_store(); store.claim_next(2); store.bind_run('a'*12, '12345678', 3)
        store.recover({}, 4)
        assert store.get('a'*12)['state'] == 'interrupted'
        assert _req(port, 'GET', '/goals')[0] == 200, 'bound run without directory must remain visible'
        serve._goal_tick(); assert not manager_calls
        path = '/goals/'+'a'*12+'/resolve'
        assert _req(port, 'POST', path, {'confirm_stopped': False})[0] != 202
        with patch.object(serve, '_run_busy', return_value=True):
            assert _req(port, 'POST', path, {'confirm_stopped': True})[0] != 202
        code, data = _req(port, 'POST', path, {'confirm_stopped': True})
        assert code == 202, (code, data)
        assert store.get('a'*12)['state'] == 'cancelled'
        assert store.get('a'*12)['run_id'] == '12345678'
        tick_until('b'*12, 'awaiting_input'); assert manager_calls == ['b'*12]

def test_historical_run_read_is_scoped_and_passive():
    with queue_app() as port:
        submit(port, 'a'*12, 'A'); submit(port, 'b'*12, 'B')
        store = serve._goal_store(); store.claim_next(2); store.bind_run('a'*12, '12345678', 3)
        directory = Path(serve.RUNS_DIR)/'12345678'; directory.mkdir()
        event = {'type':'plan_created','goal':'only A','tasks':[]}
        (directory/'events.jsonl').write_text(json.dumps(event)+'\n', encoding='utf8')
        code, data = _req(port, 'GET', '/goals/'+'a'*12+'/run')
        assert code == 200 and data['run_id'] == '12345678' and data['events'] == [event]
        assert _req(port, 'GET', '/goals/'+'b'*12+'/run')[0] == 404
        assert not manager_calls

def test_terminal_event_recovers_stale_index_but_not_new_attempt():
    with queue_app():
        directory = Path(serve.RUNS_DIR)/'12345678'; directory.mkdir()
        events = directory/'events.jsonl'
        events.write_text('\n'.join(json.dumps(e) for e in [
            {'type':'plan_created'}, {'type':'run_finished','statuses':{'t1':'done'}}])+'\n', encoding='utf8')
        assert real_run_state('12345678') == 'done'
        with events.open('a', encoding='utf8') as handle:
            handle.write(json.dumps({'type':'plan_created','resumed':True})+'\n')
        assert real_run_state('12345678') == 'interrupted', 'old completion cannot prove resumed attempt finished'

def test_terminal_reconciliation_releases_queue_manager_pointer():
    with queue_app() as port:
        submit(port, 'a'*12, 'A'); submit(port, 'b'*12, 'B')
        store = serve._goal_store(); store.claim_next(2); store.bind_run('a'*12, '12345678', 3)
        store.recover({}, 4); serve._set_active_session('a'*12)
        with patch.object(serve, '_goal_run_state', return_value='failed'):
            serve._goal_tick()
        assert store.get('a'*12)['state'] == 'failed'
        assert serve._active_session_id() == '', 'terminal owner must not remain a legacy draft'
        tick_until('b'*12, 'awaiting_input'); assert manager_calls == ['b'*12]

def test_historical_followup_stale_events_stay_in_original_run():
    with queue_app() as port:
        boards = []
        for rid in ('12345678', '87654321'):
            directory = Path(serve.RUNS_DIR)/rid; directory.mkdir()
            board = Blackboard(str(directory/'blackboard.jsonl'), on_write=None)
            board.put('run/workdir', serve.WORKDIR, author='system', kind='fact')
            for tid, deps in [('t1', []), ('t2', ['t1'])]:
                board.put('tasks/'+tid+'/spec', {'title':tid, 'depends_on':deps}, author='system', kind='spec')
                board.put('tasks/'+tid+'/status', 'done', author='engine', kind='fact')
            boards.append(board)
        serve.BOARD_PATH = boards[1].path
        serve.EVENTS_PATH = str(Path(boards[1].path).with_name('events.jsonl'))
        async def fake_continue(*args):
            return ExecResult(ok=True, output='historical answer', cost_usd=0), False
        with patch.object(serve.conversation, 'continue_task_conversation', side_effect=fake_continue), \
             patch.object(serve, '_record_followup_api_cost'):
            code, data = _req(port, 'POST', '/task/t1/message', {'run_id':'12345678', 'message':'revise'})
        assert code == 200, (code, data)
        assert data['stale_downstream'] == ['t2']
        assert Blackboard(boards[0].path, on_write=None).get('tasks/t2/status') == 'stale'
        assert Blackboard(boards[1].path, on_write=None).get('tasks/t2/status') == 'done'
        old_events = Path(boards[0].path).with_name('events.jsonl')
        assert old_events.exists(), 'stale event missing from selected run'
        assert json.loads(old_events.read_text(encoding='utf8').splitlines()[-1])['type'] == 'task_stale'
        assert not Path(serve.EVENTS_PATH).exists(), 'latest unrelated run received old stale event'

if __name__ == '__main__':
    failures = []
    for name in sorted(n for n in globals() if n.startswith('test_')):
        try: globals()[name](); print(name+': PASS')
        except Exception:
            failures.append(name); traceback.print_exc()
    assert not failures, failures
