"""Data-directory backup card (CLAUDE.md §6.7 P2 item 10), the light v1:
GET /settings gains two read-only path fields (runtime_dir /
credentials_path) and settings.html renders them with copy buttons and a
credentials warning. NO export endpoint -- the OS's own copy tools are
the backup mechanism, and the recommended path (runtime_dir) excludes
credentials.yaml by DIRECTORY LAYOUT, not by filtering code.

The invariant test below pins exactly that layout fact: if a future
refactor ever moves credentials.yaml under RUNTIME_DIR, the "safe by
default" argument silently dies -- this test must fail loudly first.

No framework, plain asserts. Zero real LLM calls.

Run: python3 tests/test_data_directory_backup.py
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve

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


def test_settings_exposes_paths_and_layout_invariant():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_backup_")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        serve._configure(tmp, "default", port)
        code, data = _get(port, "/settings")
        assert code == 200
        assert data["runtime_dir"] == serve.RUNTIME_DIR and data["runtime_dir"]
        assert data["credentials_path"] == serve.CREDENTIALS_PATH and data["credentials_path"]
        # THE layout invariant: credentials must NOT live inside runtime_dir
        # (the whole "recommended backup is safe by default" argument)
        assert not data["credentials_path"].startswith(
            data["runtime_dir"] + os.sep), (data["runtime_dir"], data["credentials_path"])
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_settings_legacy_mode_empty_paths():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_backup_legacy_")
    try:
        # no _configure(): module defaults stay ""
        serve.RUNTIME_DIR = ""
        serve.CREDENTIALS_PATH = ""
        serve.DATA_DIR = ""
        srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            code, data = _get(port, "/settings")
            assert code == 200
            assert data["runtime_dir"] == "" and data["credentials_path"] == ""
        finally:
            srv.shutdown()
    finally:
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_settings_existing_fields_unchanged():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_backup_fields_")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        serve._configure(tmp, "default", port)
        code, data = _get(port, "/settings")
        assert code == 200
        for key in ("data_dir", "project", "budget", "agents", "credentials",
                    "runs", "schedules", "version"):
            assert key in data, key
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_restore_is_just_copying_the_directory_back():
    """Honesty check for the UI copy: a hand-made "backup" structure is
    picked up by the plain startup path (_configure -> _runs_load ->
    _adopt_latest_run) with zero extra code."""
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_backup_restore_")
    try:
        project_dir = os.path.join(tmp, "demo")
        runs_dir = os.path.join(project_dir, "runs")
        run_dir = os.path.join(runs_dir, "r1")
        os.makedirs(run_dir)
        with open(os.path.join(runs_dir, "index.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"run_id": "r1", "ts": 123.0, "goal": "备份恢复测试",
                                "status": "done", "cost_usd": 0.1}) + "\n")
        open(os.path.join(run_dir, "blackboard.jsonl"), "w", encoding="utf-8").close()
        serve._configure(tmp, "demo", 8787)
        assert serve._runs[0]["goal"] == "备份恢复测试"
        assert serve._run_meta["id"] == "r1"
        assert serve.BOARD_PATH == os.path.join(run_dir, "blackboard.jsonl")
    finally:
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll data-directory-backup tests passed.")
