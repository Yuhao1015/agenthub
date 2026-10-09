"""Protocol fixtures only; real socket connects are forbidden."""
import asyncio
import json
from pathlib import Path
import socket
import sys
import tempfile
import yaml
from unittest.mock import patch
import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hub.api_executor import ApiExecutor, request_spec, parse_api_response, parse_api_stream
from hub.models import Task, TaskType
from test_api_connection_store import row
from hub.router import Router
from hub.connection_store import ConnectionStore
from hub.api_connections import project_api_connections

TASK = Task(id='test', title='test', type=TaskType.LIGHT, prompt='hello')


def connection(fmt='openai_compatible', key='a'):
    return dict(row('api-'+key), format=fmt, credential_ref='connection/'+key+'/r1',
                base_url='https://example.invalid/v1', enabled=True)


def factory(handler):
    return lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw)


def payload(fmt):
    if fmt == 'anthropic':
        return {'content':[{'type':'text','text':'你好'},{'type':'thinking','thinking':'private'},{'type':'text','text':'!'}], 'usage':{'input_tokens':10,'output_tokens':2}}
    return {'choices':[{'message':{'content':[{'type':'text','text':'你好'},{'type':'text','text':'!'}]},'finish_reason':'stop'}], 'usage':{'prompt_tokens':10,'completion_tokens':2,'cost':0.03}}


def sse(fmt, complete=True):
    if fmt == 'anthropic':
        rows = [{'type':'message_start','message':{'usage':{'input_tokens':10,'output_tokens':0}}},
                {'type':'content_block_delta','delta':{'type':'text_delta','text':'你好'}},
                {'type':'message_delta','usage':{'output_tokens':1}},
                {'type':'message_delta','usage':{'output_tokens':2}}]
        if complete: rows.append({'type':'message_stop'})
    else:
        rows = [{'choices':[{'delta':{'content':'你好'}}]}, {'choices':[], 'usage':{'prompt_tokens':10,'completion_tokens':2,'cost':0.03}}]
    text=': comment\n\n'+''.join('data: '+json.dumps(r,ensure_ascii=False)+'\n\n' for r in rows)
    if fmt != 'anthropic' and complete: text += 'data: [DONE]\n\n'
    return text


class Bytes(httpx.AsyncByteStream):
    def __init__(self, text, hang=False):
        self.data=text.encode();self.closed=False;self.hang=hang
    async def __aiter__(self):
        for byte in self.data:
            yield bytes([byte])  # splits UTF-8 characters deliberately
        if self.hang: await asyncio.sleep(100)
    async def aclose(self): self.closed=True


async def test_three_formats_and_secret_isolation():
    seen=[]
    for fmt in ('openai_compatible','anthropic','openrouter'):
        for streaming in (False, True):
            def handler(req):
                body=json.loads(req.content);seen.append(req)
                assert body['messages'][0]['content']=='hello' and body['model']=='model-a'
                assert 'tools' not in body and 'stream_options' not in body
                if fmt=='anthropic':
                    assert req.url.path=='/v1/messages' and req.headers['anthropic-version']=='2023-06-01'
                    assert req.headers['x-api-key'].startswith('fake-')
                else:
                    assert req.url.path=='/v1/chat/completions' and req.headers['authorization'].startswith('Bearer fake-')
                if streaming:return httpx.Response(200, stream=Bytes(sse(fmt)), headers={'content-type':'text/event-stream'})
                return httpx.Response(200,json=payload(fmt))
            executors=[ApiExecutor(connection(fmt,k), lambda ref:'fake-'+ref.split('/')[1], streaming, factory(handler)) for k in ('a','b')]
            events=[]
            results=await asyncio.gather(*(e.run(TASK,'',events.append) for e in executors))
            assert all(r.ok and r.output.startswith('你好') for r in results)
            assert results[0].usage['output_tokens']==2
            if streaming: assert events and '你好' in ''.join(events)
            keys=[r.headers.get('x-api-key',r.headers.get('authorization')) for r in seen[-2:]]
            assert keys[0]!=keys[1]
    assert len(seen)==12


async def test_errors_disconnect_no_retries_or_redirects():
    for status,headers,expected in [(401,{},'unavailable'),(403,{},'unavailable'),(404,{},'unavailable'),(429,{},'unavailable'),(429,{'retry-after':'1'},'transient'),(503,{},'transient'),(302,{'location':'https://other.invalid/'},'unavailable')]:
        calls=[]
        def handler(req):
            calls.append(req)
            return httpx.Response(status,headers=headers,json={'error':'fake-secret'})
        result=await ApiExecutor(connection(),lambda ref:'fake-secret',False,factory(handler)).run(TASK,'')
        assert not result.ok and result.error_kind==expected and 'fake-secret' not in result.error
        assert len(calls)==1
    def timeout(req): raise httpx.ReadTimeout('fake-secret', request=req)
    timed=await ApiExecutor(connection(),lambda ref:'fake-secret',False,factory(timeout)).run(TASK,'')
    assert not timed.ok and timed.error_kind=='transient' and 'fake-secret' not in timed.error
    for fmt in ('openai_compatible','anthropic','openrouter'):
        stream=Bytes(sse(fmt,False))
        result=await ApiExecutor(connection(fmt),lambda ref:'fake',True,factory(lambda req:httpx.Response(200,stream=stream))).run(TASK,'')
        assert not result.ok and result.error_kind=='transient' and stream.closed
        assert result.usage['output_tokens']==2
        error=sse(fmt,False)+'data: {"type":"error","error":{"type":"authentication_error","message":"fake-secret"}}\n\n'
        result=await ApiExecutor(connection(fmt),lambda ref:'fake-secret',True,factory(lambda req:httpx.Response(200,stream=Bytes(error)))).run(TASK,'')
        assert not result.ok and result.error_kind=='unavailable' and 'fake-secret' not in result.error


async def test_usage_estimation_and_cancellation():
    conn=connection('anthropic');conn['pricing']={'input':2,'output':4}
    result=await ApiExecutor(conn,lambda ref:'fake',False,factory(lambda req:httpx.Response(200,json=payload('anthropic')))).run(TASK,'')
    assert result.usage['cost_status']=='estimated' and abs(result.cost_usd-.000028)<1e-10
    for usage in ({}, {'prompt_tokens':'bad','completion_tokens':5,'cost':True}, {'cost':float('nan')}):
        parsed=parse_api_response('openai_compatible',{'choices':[{'message':{'content':'ok'}}], 'usage':usage})
        assert parsed['cost_status']=='unknown'
    assert parse_api_response('openrouter',{'choices':[{'message':{'content':'ok'}}], 'usage':{'cost':0}})['cost_status']=='reported'
    async def lines():
        for l in ['data: {"choices":', 'data: [{"delta":{"content":"ok"}}]}', '', 'data: [DONE]', '']:
            yield l
    assert (await parse_api_stream('openai_compatible',lines(),None))['text']=='ok'
    stream=Bytes(sse('anthropic',False),True)
    task=asyncio.create_task(ApiExecutor(connection('anthropic'),lambda ref:'fake',True,factory(lambda req:httpx.Response(200,stream=stream))).run(TASK,''))
    await asyncio.sleep(.03);task.cancel()
    try:await task
    except asyncio.CancelledError:pass
    else:raise AssertionError('cancellation swallowed')
    assert stream.closed


async def test_router_binding_options_and_current_consent():
    with tempfile.TemporaryDirectory() as td:
        store=ConnectionStore(str(Path(td)/'config.yaml'),str(Path(td)/'credentials.yaml'))
        cfg=dict(row(),format='openai_compatible',enabled=True,roles=['manager'])
        store.save(cfg,'replace','fake-router',0)
        matrix=project_api_connections({'agents':{},'routes':{},'tiers':{}},store.raw_snapshot())
        p=Path(td)/'matrix.yaml';p.write_text(yaml.safe_dump(matrix),encoding='utf8')
        router=Router(str(p),stream=False)
        assert not (await router.executor_for('api-a').run(TASK,'')).ok
        router.bind_api_store(store)
        calls=[]
        def handler(req):
            calls.append(req);assert json.loads(req.content)['model']=='custom'
            return httpx.Response(200,json=payload('openai_compatible'))
        executor=router.executor_with_options('api-a',{'model':'custom'})
        executor.inner.client_factory=factory(handler)
        assert (await executor.run(TASK,'')).ok
        store.save(dict(cfg,enabled=False),'keep','',1)
        # A migrated second model may share the same key; it cannot grant permission to the first.
        from hub.connection_store import secure_write
        saved=yaml.safe_load(Path(store.config_path).read_text(encoding='utf8'))
        sibling=dict(saved['api_connections']['items']['api-a'],id='api-b',agent_id='api-b',enabled=True)
        saved['api_connections']['items']['api-b']=sibling
        secure_write(store.config_path,saved)
        assert not (await executor.run(TASK,'')).ok
        assert len(calls)==1,'A cached executor cannot reuse a disabled connection'
        store.save(dict(cfg,base_url='https://changed.invalid/v1'),'keep','',2)
        assert not (await executor.run(TASK,'')).ok
        assert len(calls)==1,'A cached executor cannot send credentials to its previous target'


async def main():
    with patch.object(socket.socket,'connect',side_effect=AssertionError('real network forbidden')):
        for fn in (test_three_formats_and_secret_isolation,test_errors_disconnect_no_retries_or_redirects,test_usage_estimation_and_cancellation,test_router_binding_options_and_current_consent):
            await fn();print('PASS',fn.__name__)


if __name__=='__main__':asyncio.run(main())
