"""Workdir picker (2026-08-17 user request): settings.html「工作区域」card
backs onto config.yaml `workdir` + three POST endpoints.

Key semantics under test:
- _configure applies a VALID override only (existing dir); vanished dirs
  silently fall back to the default so the app still starts.
- POST /settings/workdir: absolute+existing+outside-DATA_DIR only; empty
  string resets to default and removes the config key; the in-process
  WORKDIR global updates immediately (next run picks it up, no restart).
- POST /settings/pick-workdir: fixed native Windows/macOS command (no user
  input in the command), cancellation preserves settings.
- POST /settings/reveal-workdir opens the CONFIGURED workdir only.

No framework, plain asserts. Zero real LLM calls. The osascript/reveal
subprocesses are monkeypatched; nothing real is launched.

Run: python3 tests/test_workdir_picker.py
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
try:
    from hub.workdir_picker import pick_workdir
except ImportError:
    pick_workdir = None

_GLOBALS = ("PORT", "MATRIX", "BOARD_PATH", "EVENTS_PATH", "WORKDIR", "DATA_DIR",
            "PROJECT", "RUNTIME_DIR", "RUNS_DIR", "INDEX_PATH", "CONFIG_PATH",
            "MERGED_MATRIX_PATH", "PREFLIGHT_PATH", "CREDENTIALS_PATH",
            "SCHEDULES_PATH",
            "_current", "_run_meta")


def _snap():
    return {"_runs": list(serve._runs), "_run_meta": dict(serve._run_meta),
            **{k: getattr(serve, k) for k in _GLOBALS}}


def _restore(s):
    for k, v in s.items():
        if k == "_runs":
            serve._runs = list(v)
        elif k == "_run_meta":
            serve._run_meta = dict(v)
        else:
            setattr(serve, k, v)


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    r = conn.getresponse()
    data = json.loads(r.read() or b"{}")
    conn.close()
    return r.status, data


def _post(port, path, obj):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path, json.dumps(obj), {"Content-Type": "application/json"})
    r = conn.getresponse()
    data = json.loads(r.read() or b"{}")
    conn.close()
    return r.status, data


def _start(tmp):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    serve._configure(tmp, "default", port)
    return srv, port


def test_configure_applies_valid_override_and_falls_back():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_wk_cfg_")
    try:
        custom = os.path.join(tmp, "my-project")
        os.makedirs(custom)
        serve._configure(tmp, "default", 8787)          # establishes CONFIG_PATH
        serve._save_config({"workdir": custom})
        serve._configure(tmp, "default", 8787)          # re-read: override wins
        assert serve.WORKDIR == os.path.realpath(custom), serve.WORKDIR
        # vanished override: fall back to the default, app still starts
        os.rmdir(custom)
        serve._configure(tmp, "default", 8787)
        assert serve.WORKDIR == os.path.join(serve.RUNTIME_DIR, "workdir")
    finally:
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_save_workdir_endpoint_validates_and_applies():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_wk_save_")
    outside = tempfile.mkdtemp(prefix="ah_wk_chosen_")   # must live OUTSIDE data dir
    try:
        srv, port = _start(tmp)
        custom = os.path.join(outside, "chosen")
        os.makedirs(custom)
        code, data = _post(port, "/settings/workdir", {"path": custom})
        assert code == 200 and data["workdir"] == os.path.realpath(custom), (code, data)
        assert serve.WORKDIR == os.path.realpath(custom)
        assert serve._load_config()["workdir"] == os.path.realpath(custom)
        # relative / missing / inside-data-dir are all rejected, config untouched
        for bad in ("relative/path",
                    os.path.join(tmp, "does-not-exist"),
                    os.path.join(serve.RUNTIME_DIR, "runs")):
            code, data = _post(port, "/settings/workdir", {"path": bad})
            assert code == 400, (bad, code, data)
        assert serve._load_config()["workdir"] == os.path.realpath(custom)
        # empty string resets to default and removes the key
        code, data = _post(port, "/settings/workdir", {"path": ""})
        assert code == 200
        assert serve.WORKDIR == os.path.join(serve.RUNTIME_DIR, "workdir")
        assert "workdir" not in serve._load_config()
        assert os.path.isdir(serve.WORKDIR)
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(outside, ignore_errors=True)


def test_pick_workdir_platforms_fixed_command():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_wk_pick_")
    try:
        srv, port = _start(tmp)
        orig_platform = serve.sys.platform
        orig_run = serve.subprocess.run
        try:
            serve.sys.platform = "linux"
            code, data = _post(port, "/settings/pick-workdir", {})
            assert code == 400, (code, data)

            serve.sys.platform = "darwin"
            captured = {}

            def fake_run(argv, **kw):
                captured["argv"] = argv
                return subprocess.CompletedProcess(argv, 0, stdout="/tmp/picked\n", stderr="")
            serve.subprocess.run = fake_run
            code, data = _post(port, "/settings/pick-workdir", {})
            assert code == 200 and data == {"path": "/tmp/picked"}, (code, data)
            # fixed command, zero user input reaches argv
            assert captured["argv"] == ["osascript", "-e",
                'POSIX path of (choose folder with prompt "选择 Agent Hub 的工作区域")']
            # user cancelled: nonzero exit, empty stdout -> cancelled
            serve.subprocess.run = lambda argv, **kw: subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="")
            code, data = _post(port, "/settings/pick-workdir", {})
            assert code == 200 and data == {"cancelled": True}, (code, data)

            serve.sys.platform = 'win32'
            calls = []
            def windows_run(argv, **kwargs):
                calls.append((argv, kwargs))
                return subprocess.CompletedProcess(argv, 0,
                    stdout=json.dumps({'path': r'C:\团队\Project A'}), stderr='')
            serve.subprocess.run = windows_run
            code, data = _post(port, '/settings/pick-workdir', {})
            assert code == 200 and data == {'path': r'C:\团队\Project A'}, (code, data)
            assert '-STA' in calls[0][0] and '-File' in calls[0][0]
            assert calls[0][1]['timeout'] == 300 and calls[0][1]['shell'] is False
            before = serve.WORKDIR
            serve.subprocess.run = lambda argv, **kw: subprocess.CompletedProcess(
                argv, 0, stdout='{"cancelled":true}', stderr='')
            assert _post(port, '/settings/pick-workdir', {}) == (200, {'cancelled': True})
            assert serve.WORKDIR == before
            serve._workdir_picker_lock.acquire()
            try: assert _post(port, '/settings/pick-workdir', {})[0] == 409
            finally: serve._workdir_picker_lock.release()
            def timeout(argv, **kwargs): raise subprocess.TimeoutExpired(argv, 300)
            serve.subprocess.run = timeout
            assert _post(port, '/settings/pick-workdir', {})[1]['cancelled'] is True
            assert not serve._workdir_picker_lock.locked()
        finally:
            serve.sys.platform = orig_platform
            serve.subprocess.run = orig_run
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_windows_picker_failure_is_recoverable():
    assert pick_workdir is not None, 'Windows picker helper missing'
    def broken(argv, **kwargs): raise OSError('SECRET')
    assert 'SECRET' not in json.dumps(pick_workdir('win32', run=broken))
    for output in ('{broken', '{}', '{"path":123}', '{"path":"bad\\npath"}'):
        result = pick_workdir('win32', run=lambda argv, **kw:
            subprocess.CompletedProcess(argv, 0, stdout=output, stderr=''))
        assert 'error' in result and 'path' not in result
def test_reveal_workdir_opens_configured_path_only():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_wk_reveal_")
    try:
        srv, port = _start(tmp)
        captured = {}
        orig_popen = serve.subprocess.Popen
        serve.subprocess.Popen = lambda argv, **kw: (captured.update(argv=argv), None)[1]
        try:
            code, data = _post(port, "/settings/reveal-workdir", {})
            assert code == 200 and data == {"ok": True}, (code, data)
        finally:
            serve.subprocess.Popen = orig_popen
        opener = 'explorer' if sys.platform == 'win32' else 'open' if sys.platform == 'darwin' else 'xdg-open'
        assert captured["argv"] == [opener, serve.WORKDIR], captured
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_settings_exposes_workdir():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_wk_settings_")
    try:
        srv, port = _start(tmp)
        code, data = _get(port, "/settings")
        assert code == 200 and data["workdir"] == serve.WORKDIR, data.get("workdir")
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll workdir-picker tests passed.")
