"""Local discovery: file fixtures only, no child processes or network."""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hub import local_discovery as D


def file(root, name):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('', encoding='utf-8')
    path.chmod(0o700)
    return str(path)


@contextlib.contextmanager
def sandbox(platform='win32'):
    with tempfile.TemporaryDirectory(prefix='ah_连接 ') as tmp:
        root = Path(tmp)
        env = {'HOME': tmp, 'USERPROFILE': tmp, 'PATH': '',
               'LOCALAPPDATA': str(root / 'local'), 'APPDATA': str(root / 'roaming')}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(D, 'PLATFORM', platform), \
             patch.object(D.shutil, 'which', return_value=None), \
             patch.object(D, 'windows_install_roots', return_value=[]), \
             patch.object(subprocess, 'Popen', side_effect=AssertionError('process forbidden')), \
             patch.object(socket, 'create_connection', side_effect=AssertionError('network forbidden')):
            yield root


def test_codex_desktop_outside_path():
    with sandbox() as root:
        exe = file(root, 'local/OpenAI/Codex/bin/version-a/codex.exe')
        result = D.discover_local('codex')
        assert result['status'] == 'found', result
        assert result['argv'] == [exe]
        assert result['source'] == 'installation'


def test_workbuddy_registered_nondefault_install():
    with sandbox() as root:
        app = root / 'another disk' / 'WorkBuddy'
        script = file(app, 'resources/app.asar.unpacked/cli/dist/codebuddy.js')
        node = file(root, '.workbuddy/binaries/node/versions/22.22.2-3/node.exe')
        with patch.object(D, 'windows_install_roots', return_value=[str(app)]):
            result = D.discover_local('codebuddy')
        assert result['status'] == 'found', result
        assert result['argv'] == [node, script]


def test_display_icon_quotes_and_comma():
    assert D.install_root_from_record({'DisplayIcon': '"D:\\工具, shared\\WorkBuddy.exe",0'}) == 'D:\\工具, shared'
    assert D.install_root_from_record({'InstallLocation': 'D:\\apps', 'DisplayIcon': 'x'}) == 'D:\\apps'
    assert D.install_root_from_record({'DisplayIcon': 'rundll32.exe -x'}) is None


def test_explicit_invalid_does_not_fallback():
    with sandbox() as root:
        file(root, 'local/OpenAI/Codex/bin/v1/codex.exe')
        result = D.discover_local('codex', {'executable': str(root / 'missing.exe')})
        assert result['status'] == 'invalid_override' and not result['argv']


def test_explicit_wins_and_multiple_candidates_stable():
    with sandbox() as root:
        one = file(root, 'local/OpenAI/Codex/bin/a/codex.exe')
        two = file(root, 'local/OpenAI/Codex/bin/b/codex.exe')
        assert D.discover_local('codex', {'executable': two})['argv'] == [two]
        result = D.discover_local('codex')
        assert result['argv'] == [one]
        assert len(result['candidates']) == 2
        assert result == D.discover_local('codex')


def test_workbuddy_missing_parts_are_not_ready():
    with sandbox() as root:
        app = root / 'workbuddy'
        file(app, 'WorkBuddy.exe')
        with patch.object(D, 'windows_install_roots', return_value=[str(app)]):
            assert D.discover_local('codebuddy')['status'] == 'incomplete'
            file(app, 'resources/app.asar.unpacked/cli/dist/codebuddy.js')
            assert D.discover_local('codebuddy')['status'] == 'incomplete'


def test_npm_codex_native_and_unknown_shim_refused():
    with sandbox() as root:
        shim = file(root, 'roaming/npm/codex.cmd')
        native = file(root, 'roaming/npm/node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe')
        assert D.discover_local('codex')['argv'] == [native]
        Path(native).unlink()
        assert D.discover_local('codex', {'executable': shim})['status'] == 'invalid_override'


def test_mac_existing_layout_with_explicit_script():
    with sandbox('darwin') as root:
        script = file(root, 'WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/dist/codebuddy.js')
        node = file(root, '.workbuddy/binaries/node/versions/22/bin/node')
        result = D.discover_local('codebuddy', {'script': script})
        assert result['argv'] == [node, script], result


def test_executable_extension_and_permission_error():
    with sandbox() as root:
        exe = file(root, '.local/bin/claude.exe')
        result = D.discover_local('claude_code')
        assert result['argv'] == [exe], (result, exe)
        with patch.object(D, '_check_file', side_effect=PermissionError('denied')):
            assert D.discover_local('claude_code')['status'] == 'unreadable'


def test_environment_override_and_registry_failure():
    with sandbox() as root:
        exe = file(root, 'custom/codex.exe')
        with patch.dict(os.environ, {'CODEX_PATH': exe}):
            assert D.discover_local('codex')['argv'] == [exe]
        with patch.object(D, 'windows_install_roots', side_effect=OSError('denied')):
            assert D.discover_local('codebuddy')['status'] == 'unreadable'


def test_executor_uses_same_windows_workbuddy_discovery():
    from hub import executors as E
    with sandbox() as root:
        app = root / 'workbuddy'
        script = file(app, 'resources/app.asar.unpacked/cli/dist/codebuddy.js')
        node = file(root, '.workbuddy/binaries/node/versions/22/node.exe')
        with patch.object(D, 'windows_install_roots', return_value=[str(app)]):
            assert E._resolve_codebuddy_paths(None, None) == (node, script)


def test_claude_explicit_path_is_carried_by_executor():
    from hub import executors as E
    with sandbox() as root:
        exe = file(root, 'chosen/claude.exe')
        executor = E.build_executor({'kind': 'claude_code', 'claude_path': exe}, workdir=str(root))
        assert executor.claude_path == exe
        assert E._claude_cmd(exe) == [exe]


if __name__ == '__main__':
    for name in sorted(n for n in globals() if n.startswith('test_')):
        globals()[name]()
        print('PASS', name)
