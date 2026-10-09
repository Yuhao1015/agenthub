"""Bounded local CLI discovery. No processes, authentication reads, or network."""
from __future__ import annotations

import glob
import itertools
import ntpath
import os
import re
import shutil
import stat
import sys

PLATFORM = sys.platform
EXTRA_BIN_DIRS = ('/usr/local/bin', '/opt/homebrew/bin', '/opt/local/bin',
                  '~/.local/bin', '~/.bun/bin', '~/.deno/bin', '~/.cargo/bin',
                  '~/.npm-global/bin', '~/bin')
KINDS = ('claude_code', 'codex', 'codebuddy')


def _check_file(path: str, executable=False) -> bool:
    try:
        info = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    if not os.access(path, os.R_OK):
        raise PermissionError(path)
    return not executable or PLATFORM == 'win32' or os.access(path, os.X_OK)


def _children(root: str) -> list[str]:
    try:
        with os.scandir(root) as items:
            return sorted(e.path for e in itertools.islice(items, 64)
                          if e.is_dir(follow_symlinks=False) and not e.name.startswith('.'))
    except (FileNotFoundError, NotADirectoryError):
        return []


def install_root_from_record(record: dict) -> str | None:
    location = str(record.get('InstallLocation') or '').strip().strip('"')
    if location and ntpath.isabs(location):
        return location
    icon = re.sub(r',\s*-?\d+\s*$', '', str(record.get('DisplayIcon') or '')).strip().strip('"')
    if ntpath.isabs(icon) and icon.lower().endswith('.exe'):
        return ntpath.dirname(icon)
    return None


def windows_install_roots(product: str) -> list[str]:
    if PLATFORM != 'win32':
        return []
    import winreg
    found = []
    branches = ('Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall',
                'Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall')
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for branch in branches:
            try:
                with winreg.OpenKey(hive, branch) as key:
                    for index in range(min(winreg.QueryInfoKey(key)[0], 2048)):
                        try:
                            with winreg.OpenKey(key, winreg.EnumKey(key, index)) as app:
                                name = str(winreg.QueryValueEx(app, 'DisplayName')[0])
                                if not re.fullmatch(re.escape(product) + r'(?:\s+[\d.]+)?', name, re.I):
                                    continue
                                row = {}
                                for field in ('InstallLocation', 'DisplayIcon'):
                                    try:
                                        row[field] = winreg.QueryValueEx(app, field)[0]
                                    except OSError:
                                        pass
                                root = install_root_from_record(row)
                                if root and root not in found:
                                    found.append(root)
                        except OSError:
                            continue
            except OSError:
                continue
    return found


def _bin_dirs(extra_dirs=None) -> list[str]:
    dirs = [p for p in os.environ.get('AGENT_HUB_EXTRA_PATH', '').split(os.pathsep) if p]
    dirs += [os.path.expanduser(p) for p in (EXTRA_BIN_DIRS if extra_dirs is None else extra_dirs)]
    if PLATFORM == 'win32':
        for env, suffix in (('APPDATA', 'npm'), ('LOCALAPPDATA', 'Microsoft/WinGet/Links')):
            if os.environ.get(env):
                dirs.append(os.path.join(os.environ[env], suffix))
    return dirs


def command_candidates(name: str, extra_dirs=None) -> list[tuple[str, str]]:
    out = []
    hit = shutil.which(name)
    if hit:
        out.append((hit, 'PATH'))
    extensions = ('', '.exe', '.cmd', '.bat') if PLATFORM == 'win32' else ('',)
    for directory in _bin_dirs(extra_dirs):
        for ext in extensions:
            candidate = os.path.join(directory, name + ext)
            if _check_file(candidate, True):
                out.append((candidate, 'installation'))
    if name == 'codex' and PLATFORM == 'win32' and os.environ.get('LOCALAPPDATA'):
        root = os.path.join(os.environ['LOCALAPPDATA'], 'OpenAI', 'Codex', 'bin')
        for version in _children(root):
            exe = os.path.join(version, 'codex.exe')
            if _check_file(exe, True):
                out.append((exe, 'installation'))
    seen = set()
    unique = []
    for path, source in out:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            unique.append((path, source))
    return unique


def which(name: str, extra_dirs=None) -> str | None:
    # Return PATH immediately; old callers may supply a mocked executable path.
    hit = shutil.which(name)
    if hit:
        return hit
    try:
        candidates = command_candidates(name, extra_dirs)
        return candidates[0][0] if candidates else None
    except OSError:
        return None


def cli_argv(path: str, kind: str) -> list[str]:
    """Resolve a supported entry without invoking cmd.exe or any candidate."""
    path = os.path.abspath(path)
    if not _check_file(path, True):
        return []
    if PLATFORM != 'win32' or not path.lower().endswith(('.cmd', '.bat')):
        return [path]
    base = os.path.dirname(path)
    candidates = [os.path.splitext(path)[0] + '.exe']
    if kind == 'codex':
        package = os.path.join(base, 'node_modules', '@openai', 'codex')
        for pattern in (('node_modules', '@openai', 'codex-win32-*', 'vendor', '*-pc-windows-msvc', 'bin', 'codex.exe'),
                        ('vendor', '*-pc-windows-msvc', 'bin', 'codex.exe')):
            candidates.extend(sorted(glob.glob(os.path.join(package, *pattern)))[:64])
    elif kind == 'claude_code':
        candidates.append(os.path.join(base, 'node_modules', '@anthropic-ai', 'claude-code', 'bin', 'claude.exe'))
    for exe in candidates:
        if _check_file(exe, True):
            return [exe]
    return []


def _result(kind, status, argv=None, source='', candidates=None):
    descriptions = {'found': '已发现命令行入口，账户与模型未验证', 'missing': '未找到命令行入口',
                    'incomplete': '缺少命令行组件或运行时', 'unreadable': '无法检查安装位置',
                    'invalid_override': '指定路径无效，请修复路径'}
    return dict(kind=kind, status=status, argv=argv or [], source=source,
                detail=descriptions[status], candidates=candidates or [])


def discover_local(kind: str, overrides: dict | None = None) -> dict:
    if kind not in KINDS:
        raise ValueError('unsupported local agent')
    overrides = overrides or {}
    try:
        if kind == 'codebuddy':
            return _workbuddy(overrides)
        env = 'CLAUDE_PATH' if kind == 'claude_code' else 'CODEX_PATH'
        explicit = overrides.get('executable') or os.environ.get(env)
        sources = [(explicit, 'configuration' if overrides.get('executable') else 'environment')] if explicit else \
            command_candidates('claude' if kind == 'claude_code' else 'codex')
        candidates = []
        for path, source in sources:
            argv = cli_argv(path, kind)
            if argv:
                candidates.append(dict(id=argv[0], label=os.path.basename(argv[0]),
                                       paths={'executable': argv[0]}, source=source))
        if candidates:
            first = candidates[0]
            return _result(kind, 'found', [first['paths']['executable']], first['source'], candidates)
        return _result(kind, 'invalid_override' if explicit else ('incomplete' if sources else 'missing'))
    except (OSError, ValueError):
        return _result(kind, 'unreadable')


def _workbuddy(overrides: dict) -> dict:
    node = overrides.get('node') or os.environ.get('WORKBUDDY_NODE_PATH')
    script = overrides.get('script') or os.environ.get('WORKBUDDY_SCRIPT_PATH')
    if (node and not _check_file(node, True)) or (script and not _check_file(script)):
        return _result('codebuddy', 'invalid_override')
    roots = []
    if not script:
        if PLATFORM == 'darwin':
            roots = ['/Applications/WorkBuddy.app/Contents']
            scripts = [os.path.join(roots[0], 'Resources/app.asar.unpacked/cli/dist/codebuddy.js')]
        elif PLATFORM == 'win32':
            roots = windows_install_roots('WorkBuddy')
            if os.environ.get('LOCALAPPDATA'):
                roots += [os.path.join(os.environ['LOCALAPPDATA'], 'Programs', 'WorkBuddy')]
            scripts = [os.path.join(r, 'resources/app.asar.unpacked/cli/dist/codebuddy.js') for r in roots]
        else:
            scripts = []
        scripts = [s for s in scripts if _check_file(s)]
    else:
        scripts = [script]
    if not node:
        versions = _children(os.path.expanduser('~/.workbuddy/binaries/node/versions'))
        for version in reversed(versions):
            candidate = os.path.join(version, 'node.exe' if PLATFORM == 'win32' else 'bin/node')
            if _check_file(candidate, True):
                node = candidate
                break
        node = node or which('node')
    if node and node.lower().endswith(('.cmd', '.bat')):
        node = None
    if not node or not _check_file(node, True) or not scripts:
        exists = bool(scripts or any(os.path.isdir(r) for r in roots))
        return _result('codebuddy', 'incomplete' if exists else 'missing')
    source = 'configuration' if overrides else ('environment' if os.environ.get('WORKBUDDY_SCRIPT_PATH') else 'installation')
    node = os.path.abspath(node)
    scripts = [os.path.abspath(s) for s in scripts]
    candidates = [dict(id=s, label='WorkBuddy', paths={'node': node, 'script': s}, source=source) for s in scripts]
    return _result('codebuddy', 'found', [node, scripts[0]], source, candidates)
