"""Fixed native folder chooser commands; returned paths are only candidates."""
import json
import ntpath
import os
import subprocess
from pathlib import Path


def pick_workdir(platform, *, run=None):
    run = run or subprocess.run
    if platform == 'win32':
        script = str(Path(__file__).resolve().parents[1] / 'assets' / 'pick-workdir.ps1')
        executable = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'),
                                  'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
        argv = [executable, '-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass', '-File', script]
    elif platform == 'darwin':
        argv = ['osascript', '-e', 'POSIX path of (choose folder with prompt "选择 Agent Hub 的工作区域")']
    else:
        return {'error': '当前系统暂不支持原生文件夹选择，请手动填写路径'}
    try:
        result = run(argv, capture_output=True, text=True, encoding='utf-8',
                     timeout=300, shell=False,
                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if platform == 'win32' else 0)
    except subprocess.TimeoutExpired:
        return {'cancelled': True, 'error': '选择框超时未操作（5 分钟），已取消'}
    except (OSError, UnicodeError):
        return {'error': '无法打开文件夹选择窗口，请重试或手动填写路径'}
    text = (result.stdout or '').strip().lstrip('\ufeff')
    if platform == 'darwin':
        return {'path': text} if result.returncode == 0 and text else {'cancelled': True}
    if result.returncode != 0 or len(text) > 65536:
        return {'error': '文件夹选择窗口未正常结束，请重试'}
    try:
        data = json.loads(text)
        if isinstance(data, dict) and data.get('cancelled') is True:
            return {'cancelled': True}
        path = data.get('path') if isinstance(data, dict) else None
        if (not isinstance(path, str) or not path or len(path) > 32767 or
                not ntpath.isabs(path) or any(ord(c) < 32 for c in path)):
            raise ValueError('Invalid folder response')
        return {'path': path}
    except (ValueError, TypeError):
        return {'error': '无法读取所选文件夹，请重试或手动填写路径'}
