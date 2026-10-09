"""Read selected CLI metadata without starting a model turn.

Secrets and raw process output never form part of the public snapshot.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import signal
import subprocess
import tempfile
import threading
import time
import types

from .agent_options import normalize_options
from .local_discovery import cli_argv

MAX_LINE = 1024 * 1024
MAX_OUTPUT = 4 * MAX_LINE
MAX_MODELS = 10000
MAX_PAGES = 100
METHODS = {'initialize', 'account/read', 'model/list', 'session/new'}


class MetadataError(ValueError):
    pass


class Unsupported(MetadataError):
    pass


def process_environment():
    env = dict(os.environ)
    for name in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'DEEPSEEK_API_KEY',
                 'OPENROUTER_API_KEY', 'ANTHROPIC_API_KEY'):
        env.pop(name, None)
    return env


def toml_config_blocked(content, keys):
    """Parse keys, including dotted/quoted spellings. Unknown syntax fails closed."""
    try:
        import tomllib
        pending = [tomllib.loads(content)]
    except (ImportError, ValueError, RecursionError):
        return True
    count = 0
    while pending:
        node = pending.pop()
        count += 1
        if count > 10000:
            return True
        if isinstance(node, dict):
            if any(key in keys for key in node):
                return True
            pending.extend(value for value in node.values() if isinstance(value, (dict, list)))
        elif isinstance(node, list):
            pending.extend(value for value in node if isinstance(value, (dict, list)))
    return False


def customizations_blocked(kind):
    """Conservatively avoid configured helper scripts and ACP startup tools."""
    directories = {'claude_code': ['~/.claude'], 'codex': ['~/.codex'],
                   'codebuddy': ['~/.codebuddy', '~/.workbuddy']}[kind]
    overrides = {'claude_code': 'CLAUDE_CONFIG_DIR', 'codex': 'CODEX_HOME',
                 'codebuddy': 'CODEBUDDY_CONFIG_DIR'}
    if os.environ.get(overrides[kind]):
        directories.append(os.environ[overrides[kind]])
    for root in directories:
        for filename in ('settings.json', 'managed-settings.json', 'config.toml'):
            path = os.path.join(os.path.expanduser(root), filename)
            try:
                with open(path, encoding='utf-8') as handle:
                    content = handle.read(2 * MAX_LINE + 1)
                if len(content) > 2 * MAX_LINE:
                    return True
                if filename.endswith('.toml'):
                    if toml_config_blocked(content, {'api_key_helper', 'credential_helper', 'hooks'}):
                        return True
                else:
                    data = json.loads(content)
                    if not isinstance(data, dict):
                        return True
                    keys = ('apiKeyHelper', 'hooks', 'statusLine', 'fileSuggestion')
                    if kind == 'codebuddy':
                        keys += ('mcpServers', 'plugins', 'enabledPlugins')
                    if any(data.get(key) for key in keys):
                        return True
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                return True
    return False


def project_models(kind, payload):
    if not isinstance(payload, dict):
        raise MetadataError('Invalid catalog')
    if kind == 'codex':
        rows = payload.get('data')
    elif kind == 'codebuddy':
        models = payload.get('models')
        rows = models.get('availableModels') if isinstance(models, dict) else None
    else:
        raise Unsupported('No catalog interface')
    if not isinstance(rows, list):
        raise Unsupported('No catalog interface')
    if len(rows) > MAX_MODELS:
        raise MetadataError('Catalog limit exceeded')
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise MetadataError('Invalid model')
        model = row.get('model') or row.get('id') if kind == 'codex' else row.get('modelId')
        try:
            normalize_options(kind, {'model': model})
        except (TypeError, ValueError):
            raise MetadataError('Invalid model') from None
        if not model:
            raise MetadataError('Missing model ID')
        if model in seen:
            continue
        seen.add(model)
        name = row.get('displayName', model) if kind == 'codex' else row.get('name', model)
        if not isinstance(name, str) or len(name) > 500:
            raise MetadataError('Invalid display name')
        efforts = row.get('supportedReasoningEfforts')
        levels = []
        if isinstance(efforts, list):
            for item in efforts:
                value = item.get('reasoningEffort') if isinstance(item, dict) else None
                if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,31}', value):
                    raise MetadataError('Invalid effort')
                if value not in levels:
                    levels.append(value)
        result.append({'id': model, 'name': name,
                       'is_default': row.get('isDefault') is True or (
                           kind == 'codebuddy' and payload['models'].get('currentModelId') == model),
                       'hidden': row.get('hidden') is True,
                       'effort_levels': levels,
                       'default_effort': row.get('defaultReasoningEffort')
                       if row.get('defaultReasoningEffort') in levels else None})
    return result


def installation_fingerprint(kind, argv, revision):
    files = []
    for path in argv:
        try:
            stat = os.stat(path)
            files.append([path, stat.st_size, stat.st_mtime_ns])
        except OSError:
            files.append([path, None, None])
    return hashlib.sha256(json.dumps([kind, files, revision], ensure_ascii=False).encode('utf-8')).hexdigest()


def load_snapshot(path, fingerprint):
    try:
        with open(path, encoding='utf-8') as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) and data.get('fingerprint') == fingerprint else {}
    except (OSError, ValueError):
        return {}


class JsonRpcPipe:
    """Bounded metadata-only JSONL transport; agent tool requests are denied."""
    def __init__(self, argv, cwd, timeout, *, json_lines=True):
        self.argv, self.cwd = argv, cwd
        self.json_lines = json_lines
        self.deadline = time.monotonic() + timeout
        self.messages = queue.Queue(maxsize=256)
        self.sequence = 0
        self.total = 0
        self.failure = None
        self.guard = threading.Lock()
        self.proc = None
        self.threads = []
        self.windows_job = None
        self.process_group = None

    def __enter__(self):
        self.proc = subprocess.Popen(self.argv, cwd=self.cwd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env=process_environment(), shell=False,
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                                     **({'start_new_session': True} if os.name != 'nt' else {}))
        try:
            if os.name == 'nt' and hasattr(self.proc, '_handle'):
                from .executors import _create_windows_kill_job
                adapter = types.SimpleNamespace(_transport=types.SimpleNamespace(
                    get_extra_info=lambda _: self.proc))
                self.windows_job = _create_windows_kill_job(adapter)
            elif os.name != 'nt':
                self.process_group = getattr(self.proc, 'pid', None)
        except OSError:
            self.proc.kill()
            self.proc.wait(timeout=1)
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                stream.close()
            raise
        for stream, stdout in ((self.proc.stdout, True), (self.proc.stderr, False)):
            worker = threading.Thread(target=self._read, args=(stream, stdout), daemon=True)
            self.threads.append(worker)
            worker.start()
        return self

    def _read(self, stream, stdout):
        try:
            while True:
                line = stream.readline(MAX_LINE + 1)
                if not line:
                    if stdout:
                        self.messages.put_nowait(None)
                    return
                with self.guard:
                    self.total += len(line)
                    if len(line) > MAX_LINE or self.total > MAX_OUTPUT:
                        self.failure = MetadataError('Process output limit exceeded')
                        return
                if stdout:
                    value = line
                    if self.json_lines:
                        try:
                            value = json.loads(line)
                        except (ValueError, UnicodeError):
                            self.failure = MetadataError('Invalid protocol response')
                            return
                    self.messages.put_nowait(value)
        except (OSError, ValueError, queue.Full):
            self.failure = MetadataError('Process output unavailable')

    def _send(self, value):
        if time.monotonic() >= self.deadline:
            raise TimeoutError('Metadata timeout')
        data = (json.dumps(value, ensure_ascii=False) + '\n').encode('utf-8')
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except (OSError, ValueError):
            raise MetadataError('Process input unavailable') from None

    def notify(self, method, params):
        if method != 'initialized':
            raise MetadataError('Notification not allowed')
        self._send({'jsonrpc': '2.0', 'method': method, 'params': params})

    def call(self, method, params):
        if method not in METHODS:
            raise MetadataError('Inference requests not allowed')
        self.sequence += 1
        request_id = self.sequence
        self._send({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
        while True:
            if self.failure:
                raise self.failure
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Metadata timeout')
            try:
                message = self.messages.get(timeout=min(remaining, .1))
            except queue.Empty:
                continue
            if not isinstance(message, dict):
                raise MetadataError('Process exited without a response')
            if 'method' in message:
                if 'id' in message:
                    self._send({'jsonrpc': '2.0', 'id': message['id'],
                                'error': {'code': -32601, 'message': 'Metadata client denies tools'}})
                continue
            if message.get('id') != request_id:
                continue
            error = message.get('error')
            if isinstance(error, dict):
                if error.get('code') == -32601:
                    raise Unsupported('Interface not supported')
                # ACP defines auth-required error -32000; no raw error reaches UI.
                if error.get('code') == -32000:
                    raise PermissionError('Authentication required')
                raise MetadataError('Metadata request rejected')
            result = message.get('result')
            if not isinstance(result, dict):
                raise MetadataError('Invalid protocol result')
            return result

    def __exit__(self, *args):
        if not self.proc:
            return
        try:
            if self.windows_job:
                self.windows_job.terminate()
            elif self.process_group:
                try:
                    os.killpg(self.process_group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.proc.stdin.close()
            if self.proc.poll() is None:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=1)
        finally:
            if self.windows_job:
                self.windows_job.close()
            for worker in self.threads:
                worker.join(timeout=.2)
            for stream in (self.proc.stdout, self.proc.stderr):
                stream.close()


def run_metadata_command(argv, *, cwd, timeout=15, **kwargs):
    """Bound output while it is produced, rather than after communicate()."""
    chunks = []
    with JsonRpcPipe(argv, cwd, timeout, json_lines=False) as pipe:
        while True:
            if pipe.failure:
                raise pipe.failure
            remaining = pipe.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Metadata timeout')
            try:
                value = pipe.messages.get(timeout=min(remaining, .1))
            except queue.Empty:
                continue
            if value is None:
                break
            chunks.append(value)
        stdout = b''.join(chunks).decode('utf-8')
        return types.SimpleNamespace(stdout=stdout, stderr='', returncode=pipe.proc.poll())


def inspect_agent(kind, argv, *, rpc_factory=None, command=None, timeout=15):
    if kind not in ('codex', 'claude_code', 'codebuddy') or not isinstance(argv, list) or not argv:
        raise ValueError('Invalid agent entry')
    if any(not isinstance(p, str) or not p or any(ord(c) < 32 for c in p) for p in argv):
        raise ValueError('Invalid agent entry')
    result = {'kind': kind, 'installation': 'found', 'checked_at': time.time(),
              'auth': {'status': 'unsupported', 'detail': '工具暂未提供可验证的登录状态'},
              'catalog': {'status': 'unsupported', 'source': '', 'read_at': None, 'models': []}}
    if customizations_blocked(kind):
        result['auth']['detail'] = '检测到自定义认证或启动配置，请在原 Agent 中检查'
        return result
    # Saved entry is a file path, not a command string or shell shim invocation.
    prefix = list(argv)
    if os.name == 'nt' and prefix[0].lower().endswith(('.bat', '.cmd')):
        prefix = cli_argv(prefix[0], kind)
        if not prefix:
            result['installation'] = 'error'
            return result
    rpc_factory = rpc_factory or JsonRpcPipe
    try:
        with tempfile.TemporaryDirectory(prefix='agenthub-metadata-') as cwd:
            if kind == 'claude_code':
                response = (command or run_metadata_command)(prefix + ['auth', 'status'], cwd=cwd,
                                shell=False, capture_output=True, timeout=timeout,
                                encoding='utf-8', text=True, env=process_environment(),
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                stdout = response.stdout or ''
                if len(stdout.encode('utf-8')) > MAX_OUTPUT:
                    raise MetadataError('Output limit')
                try:
                    data = json.loads(stdout)
                except ValueError:
                    return result
                if isinstance(data, dict) and type(data.get('loggedIn')) is bool:
                    result['auth'] = {'status': 'logged_in' if data['loggedIn'] else 'not_logged_in', 'detail': ''}
                return result
            suffix = ['app-server'] if kind == 'codex' else ['--acp']
            with rpc_factory(prefix + suffix, cwd, timeout) as rpc:
                if kind == 'codex':
                    rpc.call('initialize', {'clientInfo': {'name': 'agenthub', 'version': '1.0'}})
                    rpc.notify('initialized', {})
                    account = rpc.call('account/read', {'refreshToken': False})
                    info = account.get('account')
                    if isinstance(info, dict) and info.get('type') in ('chatgpt', 'apiKey'):
                        result['auth'] = {'status': 'logged_in', 'detail': ''}
                    elif info is None and account.get('requiresOpenaiAuth') is True:
                        result['auth'] = {'status': 'not_logged_in', 'detail': ''}
                    models, cursor, seen = [], None, set()
                    for page in range(MAX_PAGES):
                        params = {'limit': 100, 'includeHidden': True}
                        if cursor:
                            params['cursor'] = cursor
                        data = rpc.call('model/list', params)
                        models.extend(project_models(kind, data))
                        if len(models) > MAX_MODELS:
                            raise MetadataError('Catalog limit')
                        cursor = data.get('nextCursor')
                        if cursor is None:
                            break
                        if not isinstance(cursor, str) or not cursor or cursor in seen:
                            raise MetadataError('Invalid pagination')
                        seen.add(cursor)
                    else:
                        raise MetadataError('Page limit')
                else:
                    rpc.call('initialize', {'protocolVersion': 1,
                            'clientCapabilities': {'fs': {'readTextFile': False, 'writeTextFile': False}, 'terminal': False},
                            'clientInfo': {'name': 'agenthub', 'version': '1.0'}})
                    data = rpc.call('session/new', {'cwd': cwd, 'mcpServers': []})
                    models = project_models(kind, data)
                unique = {row['id']: row for row in models}
                result['catalog'] = {'status': 'ready', 'source': kind + ':' + (
                    'model/list' if kind == 'codex' else 'acp/session/new'),
                    'read_at': time.time(), 'models': list(unique.values())}
    except Unsupported:
        pass
    except PermissionError:
        result['auth'] = {'status': 'not_logged_in', 'detail': '请在原 Agent 中登录'}
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
        if result['auth']['status'] == 'unsupported':
            result['auth'] = {'status': 'error', 'detail': '状态检查失败或超时，请重试'}
        result['catalog']['status'] = 'error'
    return result
