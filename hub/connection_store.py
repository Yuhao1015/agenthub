"""Atomic project metadata + immutable, privately stored credential revisions."""
from __future__ import annotations
from contextlib import contextmanager
import ctypes
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid
import yaml
from .api_connections import validate_connection, validate_connections, public_connection


def _windows_security():
    from ctypes import wintypes as w
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    ptr = ctypes.c_void_p
    adv.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    adv.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ptr, w.DWORD, ctypes.POINTER(w.DWORD)]
    adv.ConvertSidToStringSidW.argtypes = [ptr, ctypes.POINTER(w.LPWSTR)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD, ctypes.POINTER(ptr), ctypes.POINTER(w.DWORD)]
    adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ptr, w.DWORD, w.DWORD, ctypes.POINTER(w.LPWSTR), ctypes.POINTER(w.DWORD)]
    adv.SetFileSecurityW.argtypes = [w.LPCWSTR, w.DWORD, ptr]
    adv.GetFileSecurityW.argtypes = [w.LPCWSTR, w.DWORD, ptr, w.DWORD, ctypes.POINTER(w.DWORD)]
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.LocalFree.argtypes = [ptr]
    handle, size, sid = w.HANDLE(), w.DWORD(), w.LPWSTR()
    if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(handle)):
        raise OSError('无法取得当前用户访问令牌')
    try:
        adv.GetTokenInformation(handle, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not adv.GetTokenInformation(handle, 1, buffer, size, ctypes.byref(size)):
            raise OSError('无法读取当前用户标识')
        if not adv.ConvertSidToStringSidW(ctypes.cast(buffer, ctypes.POINTER(ptr))[0], ctypes.byref(sid)):
            raise OSError('无法转换用户标识')
        user = sid.value
        kernel.LocalFree(sid)
        return adv, kernel, user
    finally:
        kernel.CloseHandle(handle)


def protect_private(path):
    if os.name != 'nt':
        os.chmod(path, 0o600)
        return
    adv, kernel, user = _windows_security()
    sd = ctypes.c_void_p()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            'D:P(A;;FA;;;SY)(A;;FA;;;' + user + ')', 1, ctypes.byref(sd), None):
        raise OSError('无法构造私密文件权限')
    try:
        if not adv.SetFileSecurityW(str(path), 0x80000004, sd):
            raise OSError('无法设置私密文件权限')
    finally:
        kernel.LocalFree(sd)
    verify_private(path)


def verify_private(path):
    if os.name != 'nt':
        if os.stat(path).st_mode & 0o777 != 0o600:
            raise OSError('私密文件权限不符合要求')
        return
    from ctypes import wintypes as w
    adv, kernel, user = _windows_security()
    size = w.DWORD()
    adv.GetFileSecurityW(str(path), 4, None, 0, ctypes.byref(size))
    buf = ctypes.create_string_buffer(size.value)
    if not adv.GetFileSecurityW(str(path), 4, buf, size, ctypes.byref(size)):
        raise OSError('无法检查私密文件权限')
    result = w.LPWSTR()
    if not adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(buf, 1, 4, ctypes.byref(result), None):
        raise OSError('无法检查私密文件权限')
    try:
        # Requesting only DACL gives exactly these two non-inherited allow ACEs.
        text = result.value
        import re
        if text[:text.find('(')] != 'D:P' or set(re.findall(r'\([^)]*\)', text)) != {
                '(A;;FA;;;SY)', '(A;;FA;;;' + user + ')'}:
            raise OSError('私密文件允许了当前用户与系统之外的访问')
    finally:
        kernel.LocalFree(result)


def secure_write(path, data):
    path = str(path)
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.credentials-', dir=os.path.dirname(path) or '.')
    try:
        protect_private(temp)  # Empty file: no secret bytes exist before ACL verification.
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
            fd = -1
            handle.write(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if fd != -1:
            os.close(fd)
        if os.path.exists(temp):
            os.unlink(temp)


_locks_guard = threading.Lock()
_locks = {}


@contextmanager
def file_lock(path, timeout=5):
    path = os.path.abspath(str(path)) + '.lock'
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _locks_guard:
        lock = _locks.setdefault(os.path.normcase(path), threading.RLock())
    if not lock.acquire(timeout=timeout):
        raise ValueError('配置正在保存，请重试')
    try:
        with open(path, 'a+b') as handle:
            # Windows byte-range locks can extend past EOF. Do not initialize a
            # byte before locking: a second creator could hold that region already.
            deadline = time.monotonic() + timeout
            while True:
                try:
                    handle.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ValueError('配置正在保存，请重试') from None
                    time.sleep(.03)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        lock.release()


def read_mapping(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding='utf-8') as handle:
            value = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError):
        raise ValueError('配置文件不可读，原文件已保留') from None
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError('配置文件格式无效，原文件已保留')
    return value


class ConnectionStore:
    def __init__(self, config_path: str, credentials_path: str, publisher=None):
        self.config_path, self.credentials_path = str(config_path), str(credentials_path)
        self.publisher = publisher

    def _write_config(self, cfg):
        existed = os.path.exists(self.config_path)
        old = read_mapping(self.config_path)
        secure_write(self.config_path, cfg)
        try:
            if self.publisher:
                self.publisher()
        except Exception:
            if existed:
                secure_write(self.config_path, old)
            else:
                os.unlink(self.config_path)
            raise

    @contextmanager
    def locked(self):
        # Shared credential lock always precedes project lock, across all projects.
        with file_lock(self.credentials_path), file_lock(self.config_path):
            yield

    def raw_snapshot(self):
        return validate_connections(read_mapping(self.config_path).get('api_connections'))

    def snapshot(self):
        with self.locked():
            raw = self.raw_snapshot()
            return {**raw, 'items': {key: public_connection(value, self.has_secret(value.get('credential_ref', '')))
                                    for key, value in raw['items'].items()}}

    def has_secret(self, ref):
        return bool(self.resolve_secret(ref))

    def resolve_secret(self, ref):
        if not ref:
            return ''
        if ref.startswith('env/'):
            return os.environ.get(ref[4:], '')
        data = read_mapping(self.credentials_path)
        if ref.startswith('legacy/'):
            return str(data.get(ref[7:], '') or '')
        namespace = data.get('connections', {})
        if not isinstance(namespace, dict):
            raise ValueError('凭据存储格式无效')
        value = namespace.get(ref, '')
        if not isinstance(value, str):
            raise ValueError('凭据格式无效')
        return value

    def _current(self, expected_revision):
        cfg = read_mapping(self.config_path)
        state = validate_connections(cfg.get('api_connections'))
        if type(expected_revision) is not int or state['revision'] != expected_revision:
            raise ValueError('配置已更新，请刷新后重试')
        return cfg, state

    def save(self, connection, key_action, key_value, expected_revision):
        if key_action not in ('keep', 'replace', 'clear'):
            raise ValueError('请选择保留、更换或清除密钥')
        if not isinstance(key_value, str) or len(key_value) > 16000 or any(ord(c) < 32 for c in key_value):
            raise ValueError('密钥格式无效')
        if key_action != 'replace' and key_value:
            raise ValueError('保留或清除密钥时不能传入新密钥')
        item = validate_connection(connection)
        if 'credential_ref' in item:
            raise ValueError('不能直接修改凭据引用')
        with self.locked():
            cfg, state = self._current(expected_revision)
            old = state['items'].get(item['id'], {})
            if old and old['agent_id'] != item['agent_id']:
                raise ValueError('不能修改历史 Agent 标识')
            ref = old.get('credential_ref', '') if key_action == 'keep' else ''
            if key_action == 'replace':
                if not key_value.strip():
                    raise ValueError('请填写新密钥')
                ref = 'connection/' + uuid.uuid4().hex + '/' + uuid.uuid4().hex
                credentials = read_mapping(self.credentials_path)
                namespace = credentials.setdefault('connections', {})
                if not isinstance(namespace, dict):
                    raise ValueError('凭据存储格式无效')
                namespace[ref] = key_value
                secure_write(self.credentials_path, credentials)
            item['credential_ref'] = ref
            if item['enabled'] and not self.has_secret(ref):
                raise ValueError('启用连接前请配置密钥')
            state['items'][item['id']] = item
            state['revision'] += 1
            cfg['api_connections'] = validate_connections(state)
            self._write_config(cfg)
        return self.snapshot()

    def delete(self, connection_id, expected_revision):
        with self.locked():
            cfg, state = self._current(expected_revision)
            if connection_id not in state['items']:
                raise ValueError('连接不存在')
            del state['items'][connection_id]
            state['revision'] += 1
            cfg['api_connections'] = state
            self._write_config(cfg)
            # Immutable refs are retained: other projects/backup configs may reference them.
            # An explicit data-directory cleanup may reclaim unreferenced credentials.
        return self.snapshot()

    def import_legacy(self, proposal, expected_revision):
        """Trusted server-generated proposal, never a raw browser credential ref."""
        proposal = validate_connections(proposal)
        with self.locked():
            cfg, state = self._current(expected_revision)
            credentials = read_mapping(self.credentials_path)
            copied = {}
            for key, item in proposal['items'].items():
                if key in state['items']:
                    continue  # Retry never overwrites a user's edited connection.
                ref = item.get('credential_ref', '')
                if ref.startswith('legacy/'):
                    if ref not in copied:
                        value = self.resolve_secret(ref)
                        if not value:
                            raise ValueError('旧凭据已不可用，请重新配置')
                        copied[ref] = 'connection/' + uuid.uuid4().hex + '/' + uuid.uuid4().hex
                        credentials.setdefault('connections', {})[copied[ref]] = value
                    item['credential_ref'] = copied[ref]
                elif not ref.startswith('env/') or not self.has_secret(ref):
                    raise ValueError('旧凭据已不可用，请重新配置')
                state['items'][key] = item
            if copied:
                secure_write(self.credentials_path, credentials)
            state['revision'] += 1
            cfg['api_connections'] = state
            self._write_config(cfg)
        return self.snapshot()
