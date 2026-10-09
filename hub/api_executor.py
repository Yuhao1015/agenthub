"""Text-only Chat Completions / Messages adapters with explicit credentials."""
from __future__ import annotations
import asyncio
import copy
import json
import math
import httpx
from .executors import Executor, ExecResult
from .api_connections import validate_connection


def request_spec(connection, key, prompt, context, stream):
    c = validate_connection(connection)
    text = prompt if not context else prompt + '\n\n<context>\n' + context + '\n</context>'
    body = {'model': c['model'], 'messages': [{'role': 'user', 'content': text}],
            'stream': bool(stream), 'max_tokens': c['max_output_tokens']}
    if c['format'] == 'anthropic':
        endpoint = '/messages'
        headers = {'x-api-key': key, 'anthropic-version': '2023-06-01'}
    else:
        endpoint = '/chat/completions'
        headers = {'Authorization': 'Bearer ' + key}
    return c['base_url'] + endpoint, headers, body


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ''.join(str(c.get('text', '')) for c in content
                       if isinstance(c, dict) and c.get('type') == 'text' and isinstance(c.get('text'), str))
    return ''


def _usage(fmt, raw):
    raw = raw if isinstance(raw, dict) else {}
    result = {}
    for target, source in (('input_tokens', 'input_tokens' if fmt == 'anthropic' else 'prompt_tokens'),
                           ('output_tokens', 'output_tokens' if fmt == 'anthropic' else 'completion_tokens'),
                           ('cache_read_tokens', 'cache_read_input_tokens'),
                           ('cache_write_tokens', 'cache_creation_input_tokens')):
        value = raw.get(source)
        if type(value) is int and value >= 0:
            result[target] = value
    if fmt != 'anthropic':
        details = raw.get('prompt_tokens_details')
        cached = details.get('cached_tokens') if isinstance(details, dict) else None
        if type(cached) is int and cached >= 0 and cached <= result.get('input_tokens', -1):
            result['cache_read_tokens'] = cached
            result['input_tokens'] -= cached
    return result


def _result(fmt, text, raw):
    raw = raw if isinstance(raw, dict) else {}
    cost = raw.get('cost')
    return {'text': text, 'usage': _usage(fmt, raw),
            'cost_usd': float(cost) if _number(cost) else 0.0,
            'cost_status': 'reported' if _number(cost) else 'unknown'}


class ApiFailure(Exception):
    def __init__(self, kind, message, partial=None):
        super().__init__(message)
        self.kind, self.partial = kind, partial


def _error_kind(error):
    error = error if isinstance(error, dict) else {}
    code, typ = error.get('code'), error.get('type')
    if code in (500, 502, 503, 504, 529) or typ in ('overloaded_error', 'api_error'):
        return 'transient'
    return 'unavailable'


def parse_api_response(fmt, body):
    if not isinstance(body, dict):
        raise ApiFailure('task_error', 'API 返回了无法识别的数据')
    if body.get('error'):
        raise ApiFailure(_error_kind(body['error']), 'API 返回错误', _result(fmt, '', body.get('usage')))
    if fmt == 'anthropic':
        text = _text(body.get('content'))
    else:
        choices = body.get('choices')
        first = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        message = first.get('message')
        text = _text(message.get('content')) if isinstance(message, dict) else ''
    result = _result(fmt, text, body.get('usage'))
    if not text:
        raise ApiFailure('task_error', 'API 没有返回文本', result)
    return result


async def parse_api_stream(fmt, lines, on_event):
    chunks, usage, data = [], {}, []
    terminal = False
    def partial():
        return _result(fmt, ''.join(chunks), usage)
    def consume():
        nonlocal terminal
        if not data:
            return
        frame = '\n'.join(data)
        data.clear()
        if frame.strip() == '[DONE]':
            terminal = True
            return
        try:
            row = json.loads(frame)
        except ValueError:
            raise ApiFailure('transient', 'API 流式数据不完整', partial()) from None
        if not isinstance(row, dict):
            raise ApiFailure('transient', 'API 流式数据格式无效', partial())
        if row.get('error') or row.get('type') == 'error':
            raise ApiFailure(_error_kind(row.get('error')), 'API 流式请求失败', partial())
        text = ''
        if fmt == 'anthropic':
            typ = row.get('type')
            if typ == 'message_start':
                message = row.get('message', {})
                u = message.get('usage') if isinstance(message, dict) else None
                if isinstance(u, dict): usage.update(u)
            elif typ == 'message_delta':
                if isinstance(row.get('usage'), dict): usage.update(row['usage'])
            elif typ == 'content_block_delta':
                delta = row.get('delta')
                if isinstance(delta, dict) and delta.get('type') == 'text_delta':
                    text = _text(delta.get('text'))
            elif typ == 'message_stop':
                terminal = True
        else:
            if isinstance(row.get('usage'), dict): usage.update(row['usage'])
            choices = row.get('choices', [])
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                delta = choices[0].get('delta')
                if isinstance(delta, dict): text = _text(delta.get('content'))
                if choices[0].get('finish_reason') is not None: terminal = True
        if text:
            chunks.append(text)
            if on_event: on_event(text)
    try:
        async for line in lines:
            if not line:
                consume()
            elif line.startswith('data:'):
                data.append(line[5:].lstrip(' '))
        if data: consume()
    except (httpx.HTTPError, OSError):
        raise ApiFailure('transient', 'API 流式连接中断', partial()) from None
    if not terminal:
        raise ApiFailure('transient', 'API 流式响应未完成', partial())
    result = partial()
    if not result['text']:
        raise ApiFailure('task_error', 'API 没有返回文本', result)
    return result


def _priced(result, pricing):
    if result['cost_status'] != 'unknown' or not pricing:
        return result
    usage = result['usage']
    if not {'input_tokens', 'output_tokens'} <= set(usage):
        return result
    if any(usage.get(token, 0) and rate not in pricing for token, rate in
           (('cache_read_tokens', 'cache_read'), ('cache_write_tokens', 'cache_write'))):
        return result
    result['cost_usd'] = sum(usage.get(token, 0) * pricing.get(rate, 0) / 1e6 for token, rate in
                             (('input_tokens', 'input'), ('output_tokens', 'output'),
                              ('cache_read_tokens', 'cache_read'), ('cache_write_tokens', 'cache_write')))
    result['cost_status'] = 'estimated'
    return result


class ApiExecutor(Executor):
    name = 'api'
    def __init__(self, connection, secret_provider, stream=True, client_factory=None):
        self.connection = copy.deepcopy(connection)
        self.secret_provider, self.stream = secret_provider, stream
        self.client_factory = client_factory or httpx.AsyncClient

    async def run(self, task, context, on_event=None):
        try:
            key = self.secret_provider(self.connection.get('credential_ref', '')) if self.secret_provider else ''
            if not isinstance(key, str) or not key:
                raise ValueError()
            url, headers, body = request_spec(self.connection, key, task.prompt, context, self.stream)
        except (ValueError, OSError):
            return ExecResult(ok=False, error='API 连接或凭据不可用，请检查接入设置', error_kind='unavailable',
                              usage={'cost_status': 'reported', 'request_sent': False})
        parsed = _result(self.connection['format'], '', {})
        ok, error, kind = False, '', ''
        try:
            async with self.client_factory(timeout=httpx.Timeout(120, connect=15), follow_redirects=False, trust_env=False) as client:
                async with client.stream('POST', url, headers=headers, json=body) as response:
                    status = response.status_code
                    if status >= 300:
                        kind = 'transient' if status >= 500 or status == 429 and response.headers.get('retry-after') else 'unavailable'
                        raise ApiFailure(kind, 'API 请求失败（HTTP ' + str(status) + '）')
                    if self.stream:
                        parsed = await parse_api_stream(self.connection['format'], response.aiter_lines(), on_event)
                    else:
                        await response.aread()
                        parsed = parse_api_response(self.connection['format'], response.json())
                    ok = True
        except ApiFailure as exc:
            error, kind = str(exc), exc.kind
            if exc.partial is not None: parsed = exc.partial
        except (httpx.HTTPError, OSError):
            error, kind = 'API 网络请求失败或超时，请检查连接', 'transient'
        except (ValueError, TypeError, KeyError):
            error, kind = 'API 响应格式无效', 'task_error'
        parsed = _priced(parsed, self.connection.get('pricing'))
        if self.stream and not ok:
            # message_start usage is only a snapshot. Keep its known expense,
            # but a failed/unfinished stream cannot prove the final charge.
            parsed['cost_status'] = 'unknown'
        return ExecResult(ok=ok, output=parsed['text'], error=error, error_kind=kind,
                          cost_usd=parsed['cost_usd'],
                          usage={**parsed['usage'], 'cost_status': parsed['cost_status'], 'request_sent': True})
