"""Routing and HTTP consent gates; all execution uses mocks."""
from __future__ import annotations
import asyncio
import contextlib
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
import yaml

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).parent)]
import serve
from hub.router import Router
from hub.models import Task, TaskType
from hub.local_agents import validate_local_settings, apply_local_policy
from test_local_agent_settings import app
from test_gate_serve import _req


BASE = {'agents': {'claude': {'kind': 'claude_code'}, 'codex-local': {'kind': 'codex'},
                   'buddy': {'kind': 'codebuddy', 'model': 'test'},
                   'paid': {'kind': 'openrouter', 'model': 'test'}},
        'routes': {'manager': ['claude', 'buddy'], 'coding': ['claude', 'codex-local', 'paid']},
        'tiers': {'mid': ['claude', 'buddy'], 'economy': ['buddy']}}


@contextlib.contextmanager
def router_fixture(enabled=('codex',), confirmed=True, legacy=False, mock=True):
    with tempfile.TemporaryDirectory() as root:
        state = validate_local_settings({}, sys.platform)
        state['confirmed'] = confirmed
        for kind in enabled:
            state['agents'][kind]['enabled'] = True
        matrix = BASE if legacy else apply_local_policy(BASE, state, sys.platform)
        path = Path(root, 'matrix.yaml')
        path.write_text(yaml.safe_dump(matrix), encoding='utf-8')
        yield Router(str(path), mock_all=mock)


def task(kind):
    return Task(id='task', title='test', type=kind, prompt='mock')


def test_disabled_direct_and_option_calls_refused():
    with router_fixture() as router:
        assert not router.agent_enabled('claude')
        for call in (lambda: router.executor_for('claude'),
                     lambda: router.executor_with_options('claude', {'model': 'x'})):
            try:
                call()
            except ValueError:
                pass
            else:
                raise AssertionError('disabled executor returned')
        assert 'claude' in router.all_agents()  # metadata remains visible


def test_codex_manager_tier_fallback_and_circuit():
    with router_fixture() as router:
        for kind in (TaskType.MANAGER, TaskType.CODING, TaskType.LIGHT, TaskType.WRITING):
            chain = router.chain_for(task(kind))
            assert [name for name, _ in chain] == ['codex-local'], (kind, chain)
            assert asyncio.run(chain[0][1].run(task(kind), '')).ok
        assert router.tier_of('codex-local') is None
        router.open_circuit('codex-local')
        try:
            router.chain_for(task(TaskType.MANAGER))
        except ValueError:
            pass
        else:
            raise AssertionError('circuit-open fallback returned')


def test_pending_blocks_paid_default_and_preflight():
    with router_fixture(confirmed=False, mock=False) as router:
        assert not router.agent_enabled('paid')
        assert router.claude_code_agents() == []
        assert router.codex_agents() == []
        assert router.codebuddy_agents() == []
    with router_fixture(legacy=True) as router:
        assert router.agent_enabled('claude') and router.agent_enabled('paid')
        assert [n for n, _ in router.chain_for(task(TaskType.MANAGER))] == ['claude', 'buddy']


def test_http_chat_pending_never_calls_manager():
    with app() as (port, _), patch.object(serve.manager, 'manager_step', side_effect=AssertionError('called')) as step:
        code, data = _req(port, 'POST', '/chat', {'message': 'hello'})
        assert code == 409, (code, data)
        step.assert_not_called()


def test_pinned_disabled_manager_does_not_fallback():
    with router_fixture() as router:
        try:
            serve._apply_manager_pref(router, {'manager_agent': 'claude'})
        except ValueError:
            pass
        else:
            raise AssertionError('disabled manager accepted')


def test_old_router_reloads_after_disable_and_busy_saves_refused():
    with app() as (port, _):
        state = _req(port, 'GET', '/settings/local-agents')[1]['settings']
        state['confirmed'] = True
        state['agents']['claude_code']['enabled'] = True
        assert _req(port, 'POST', '/settings/local-agents',
                    {'settings': state, 'expected_revision': 0})[0] == 200
        session = {}
        old = serve._session_router(session)
        assert old.agent_enabled('cc-sonnet')
        state = _req(port, 'GET', '/settings/local-agents')[1]['settings']
        state['agents']['claude_code']['enabled'] = False
        body = {'settings': state, 'expected_revision': 1}
        with serve._connection_call():
            assert _req(port, 'POST', '/settings/local-agents', body)[0] == 409
        assert _req(port, 'POST', '/settings/local-agents', body)[0] == 200
        new = serve._session_router(session)
        assert new is not old and not new.agent_enabled('cc-sonnet')


def test_preflight_and_start_do_not_bypass_pending():
    with app() as (port, _), patch.object(serve, '_real_preflight', side_effect=AssertionError('called')) as preflight:
        assert _req(port, 'POST', '/settings/preflight', {})[0] == 409
        preflight.assert_not_called()
        try:
            serve._spawn_main(['--goal=test'])
        except OSError as exc:
            assert '接入' in str(exc)
        else:
            raise AssertionError('engine started before onboarding')


def test_history_followup_disabled_refuses_before_callback():
    from hub.blackboard import Blackboard
    with app() as (port, _):
        serve._runs_add('history', 'old goal')
        serve._adopt_latest_run()
        Path(serve.BOARD_PATH).parent.mkdir(parents=True, exist_ok=True)
        board = Blackboard(serve.BOARD_PATH)
        for key, value in {'spec': {'title': 'old'}, 'agent': 'cc-sonnet',
                           'session_id': 'old-session', 'status': 'done', 'output': 'old answer'}.items():
            board.put('tasks/t/' + key, value, author='engine')
        code, data = _req(port, 'POST', '/task/t/message', {'message': 'again'})
        assert code == 409, (code, data)
        board.refresh()
        assert board.get('tasks/t/output') == 'old answer'


if __name__ == '__main__':
    for name in sorted(n for n in globals() if n.startswith('test_')):
        globals()[name]()
        print('PASS', name)
