import os, sys, tempfile, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hub.router import Router
from hub.executors import MockExecutor, ExecResult
from hub.models import Plan
from test_gate_plan import _setup, TEST_GATE_TOKEN

def test_executor_isolation():
    r = Router('hub/capability_matrix.yaml')
    default = r.executor_for('cc-sonnet')
    selected = r.executor_with_options('cc-sonnet', {'model':'custom', 'effort':'high'})
    assert selected.model == 'custom' and selected.reasoning_effort == 'high'
    assert selected.allowed_tools == default.allowed_tools
    assert default.model == 'sonnet' and default.reasoning_effort == ''
    spec = r.profile_spec('cc-sonnet'); spec['model']='changed'
    assert r.model('cc-sonnet') == 'sonnet'
    r = Router('hub/capability_matrix.yaml', mock_all=True)
    assert isinstance(r.executor_with_options('cc-sonnet', {'model':'custom'}), MockExecutor)

def test_gate_snapshot_and_failures():
    with tempfile.TemporaryDirectory() as td:
        eng, bus, default = _setup(td, gate_mode='ask', reply={'decision':'approve','overrides':{'t1':'first'},'execution_options':{'t1':{'model':'custom','effort':'high'}}})
        eng.router.resolved_options = lambda name, options: dict(model='base',effort='',**{}) if not options else dict(options)
        class Failed:
            calls=0
            async def run(self,*args,**kwargs):
                self.calls += 1
                return ExecResult(ok=False,error='model not found',error_kind='unavailable')
        failed=Failed()
        eng.router.executor_with_options = lambda name, opts: failed
        plan=asyncio.run(eng.run()); bus.close()
        assert failed.calls == 1 and not default.calls
        assert not eng.router.circuit_open(), eng.router.circuit_open()
        assert Plan.from_blackboard(eng.board).tasks['t1'].execution_options == {'model':'custom','effort':'high'}

if __name__=='__main__':
    test_executor_isolation(); test_gate_snapshot_and_failures()
    print('Task selection runtime tests passed')
