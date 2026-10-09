"""A confirmed, isolated call to one model. No routing or paid retry."""
from __future__ import annotations
import asyncio
import json
import math
import os
import queue
import re
import tempfile
import time

from .agent_options import normalize_options
from .local_agent_metadata import JsonRpcPipe, customizations_blocked, run_metadata_command, toml_config_blocked
from .local_discovery import cli_argv
from .executors import _codebuddy_raw_usage

PROMPT = 'Reply with exactly: ok'
TOOLS_HELP = '--tools --disallowedTools --strict-mcp-config --mcp-config --setting-sources --safe-mode --restricted'
USAGE_KEYS = ('input_tokens', 'output_tokens', 'cached_input_tokens', 'total_tokens',
              'cache_read_input_tokens', 'cache_creation_input_tokens', 'reasoning_output_tokens',
              'prompt_tokens', 'completion_tokens', 'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens')


def build_probe_argv(kind, argv, model, *, help_text=''):
    normalize_options(kind, {'model': model})
    if kind not in ('codex', 'claude_code', 'codebuddy') or not argv:
        raise ValueError('Unsupported probe')
    prefix = list(argv)
    if os.name == 'nt' and prefix[0].lower().endswith(('.cmd', '.bat')):
        prefix = cli_argv(prefix[0], kind)
        if not prefix:
            raise ValueError('Safe CLI entry unavailable')
    if kind == 'codex':
        cmd = prefix + ['exec', '--json', '--ephemeral', '--sandbox', 'read-only',
                        '--skip-git-repo-check', '--color', 'never',
                        '--disable', 'shell_tool', '--disable', 'unified_exec',
                        '-c', 'web_search="disabled"', '-c', 'mcp_servers={}',
                        '-c', 'features.skill_mcp_dependency_install=false',
                        '-c', 'features.multi_agent=false']
    else:
        required = ['--tools', '--strict-mcp-config', '--mcp-config', '--setting-sources']
        if kind == 'claude_code':
            required += ['--disallowedTools', '--safe-mode', '--restricted']
        if any(flag not in help_text for flag in required):
            raise ValueError('工具版本未提供安全测试所需选项，请在原 Agent 中测试')
        cmd = prefix + ['-p'] + ([PROMPT] if kind == 'codebuddy' else []) + [
                        '--output-format', 'json', '--tools', '',
                        '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                        '--setting-sources', '']
        if kind == 'claude_code':
            cmd += ['--disallowedTools', 'mcp__*', '--safe-mode', '--restricted']
    if model:
        cmd += ['--model', model]
    if kind == 'codex':
        cmd += ['-']
    return cmd


def _probe_config_blocked(kind):
    if customizations_blocked(kind):
        return True
    # Empty tables may merge rather than replace provider configuration. Refuse
    # external tool/plugin/hook configuration until a version can disable it.
    if kind == 'codex':
        root = os.environ.get('CODEX_HOME') or os.path.expanduser('~/.codex')
        try:
            with open(os.path.join(root, 'config.toml'), encoding='utf-8') as handle:
                text = handle.read(2 * 1024 * 1024 + 1)
            return len(text) > 2 * 1024 * 1024 or toml_config_blocked(text,
                {'mcp_servers', 'hooks', 'plugins', 'apps', 'api_key_helper', 'credential_helper'})
        except FileNotFoundError:
            pass
        except OSError:
            return True
    return False


def public_probe_result(result, selected_model=''):
    ok = isinstance(result, dict) and result.get('ok') is True
    result = result if isinstance(result, dict) else {}
    actual = result.get('model')
    if selected_model and isinstance(actual, str) and actual and actual != selected_model:
        ok = False
    out = {'ok': ok, 'usage': {}, 'cost_status': 'unknown'}
    if not ok:
        out['error'] = '调用失败，请检查账号、模型配置或稍后重试'
        if selected_model and actual and actual != selected_model:
            out['error'] = '实际返回的模型与所选模型不同，未标记通过'
    usage = result.get('usage', {})
    if isinstance(usage, dict):
        out['usage'] = {key: value for key, value in usage.items() if key in USAGE_KEYS
                        and type(value) in (int, float) and math.isfinite(value) and value >= 0}
    if result.get('cost_status') in ('reported', 'subscription', 'estimated'):
        out['cost_status'] = result['cost_status']
        for key in ('cost_usd', 'cost_credits'):
            value = result.get(key)
            if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                out[key] = value
    return out


def _parse_result(raw):
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, list):
        if not data or not isinstance(data[-1], dict):
            return {'ok': False, 'usage': {}, 'cost_status': 'unknown'}
        result = _parse_result(json.dumps(data[-1]))
        usage = _codebuddy_raw_usage(data)
        if usage is not None:
            result['usage'] = usage
            if type(usage.get('credit')) in (int, float):
                result.update(cost_status='reported', cost_credits=usage['credit'])
        return result
    if isinstance(data, dict):
        result = {'ok': (data.get('type') == 'result' or isinstance(data.get('result'), str))
                  and data.get('is_error') is not True and data.get('error') is None,
                  'usage': data.get('usage', {}), 'model': data.get('model'), 'cost_status': 'unknown'}
        if type(data.get('total_cost_usd')) in (int, float):
            result.update(cost_status='reported', cost_usd=data['total_cost_usd'])
        return result
    result = {'ok': False, 'usage': {}, 'cost_status': 'unknown'}
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get('type') == 'turn.completed':
            result.update(ok=True, usage=event.get('usage', {}))
        elif event.get('type') in ('turn.failed', 'error'):
            result['ok'] = False
    return result


def _run_process(command, prompt, cwd, timeout):
    chunks = []
    with JsonRpcPipe(command, cwd, timeout, json_lines=False) as pipe:
        if PROMPT not in command:
            pipe.proc.stdin.write((prompt + '\n').encode('utf-8'))
            pipe.proc.stdin.flush()
        pipe.proc.stdin.close()
        while True:
            if pipe.failure:
                raise pipe.failure
            remaining = pipe.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Probe timeout')
            try:
                value = pipe.messages.get(timeout=min(remaining, .1))
            except queue.Empty:
                continue
            if value is None:
                break
            chunks.append(value)
        pipe.proc.wait(timeout=max(.01, pipe.deadline - time.monotonic()))
        result = _parse_result(b''.join(chunks).decode('utf-8'))
        if pipe.proc.returncode != 0:
            result['ok'] = False
        return result


async def run_model_probe(kind, argv, model, *, runner=None, timeout=60):
    normalize_options(kind, {'model': model})
    if _probe_config_blocked(kind):
        return {'ok': False, 'error': '当前自定义工具或认证配置无法安全关闭，请在原 Agent 中测试',
                'usage': {}, 'cost_status': 'unknown'}
    with tempfile.TemporaryDirectory(prefix='agenthub-model-test-') as cwd:
        help_text = TOOLS_HELP if runner is not None else ''
        if kind != 'codex' and runner is None:
            help_result = await asyncio.to_thread(run_metadata_command, list(argv) + ['--help'],
                                                 cwd=cwd, timeout=15)
            help_text = help_result.stdout
        try:
            command = build_probe_argv(kind, argv, model, help_text=help_text)
        except ValueError:
            return {'ok': False, 'error': '工具版本未提供安全测试所需选项，请在原 Agent 中测试',
                    'usage': {}, 'cost_status': 'unknown'}
        try:
            result = (await runner(command, PROMPT, cwd, timeout) if runner is not None else
                      await asyncio.to_thread(_run_process, command, PROMPT, cwd, timeout))
        except (OSError, ValueError, TimeoutError):
            return {'ok': False, 'error': '模型测试失败或超时，请重试', 'usage': {}, 'cost_status': 'unknown'}
        return public_probe_result(result, model)
