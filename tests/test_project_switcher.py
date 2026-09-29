"""Project switcher (CLAUDE.md §6.7 P1 item 7): POST /settings/switch-project
writes <data-dir>/last_project.json then shuts the server down (no in-process
hot switch -- design doc §2 argues _configure() is not re-entrant), and
GET /settings/projects lists valid project dirs read-only.

The new attack surface is PROJECT accepting untrusted HTTP input for the
first time: the name is validated against _PROJECT_NAME_RE BEFORE it ever
reaches os.path.join(), and _load_last_project re-validates on read.

No framework, plain asserts. Zero real LLM calls, no real subprocesses
(SERVER is None in tests, so a successful switch never actually shuts
anything down).

Run: python3 tests/test_project_switcher.py
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


def _post(port, path, obj):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path, json.dumps(obj), {"Content-Type": "application/json"})
    r = conn.getresponse()
    data = json.loads(r.read() or b"{}")
    conn.close()
    return r.status, data


def test_load_last_project_priority_chain():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_proj_prio_")
    try:
        # no marker file: fall back to "default"
        serve._configure(tmp, None, 8787)
        assert serve.PROJECT == "default", serve.PROJECT
        # valid marker file: adopted
        with open(os.path.join(tmp, "last_project.json"), "w", encoding="utf-8") as f:
            f.write('{"project": "acme"}')
        serve._configure(tmp, None, 8787)
        assert serve.PROJECT == "acme", serve.PROJECT
        # explicit --project beats the marker
        serve._configure(tmp, "explicit", 8787)
        assert serve.PROJECT == "explicit", serve.PROJECT
    finally:
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_load_last_project_rejects_bad_name():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_proj_bad_")
    try:
        for bad in ("../../etc", "a/b", "带空格", ".hidden", "x" * 65, ""):
            with open(os.path.join(tmp, "last_project.json"), "w", encoding="utf-8") as f:
                json.dump({"project": bad}, f)
            assert serve._load_last_project(tmp) is None, bad
        # corrupted json -> None
        with open(os.path.join(tmp, "last_project.json"), "w", encoding="utf-8") as f:
            f.write("{nope")
        assert serve._load_last_project(tmp) is None
        # missing file -> None
        os.remove(os.path.join(tmp, "last_project.json"))
        assert serve._load_last_project(tmp) is None
    finally:
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


class _FakeProc:
    def poll(self):
        return None   # still running -> _run_busy() True


def test_switch_project_endpoint_roundtrip():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_proj_switch_")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        serve._configure(tmp, "default", port)
        code, data = _post(port, "/settings/switch-project", {"project": "acme"})
        assert code == 200 and data == {"ok": True, "project": "acme"}, (code, data)
        with open(os.path.join(tmp, "last_project.json"), encoding="utf-8") as f:
            assert json.load(f) == {"project": "acme"}
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_switch_project_rejects_bad_name_without_writing():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_proj_reject_")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        serve._configure(tmp, "default", port)
        for bad in ("../evil", "bad name", "a.b", "x" * 65):
            code, data = _post(port, "/settings/switch-project", {"project": bad})
            assert code == 400, (bad, code, data)
            assert not os.path.exists(os.path.join(tmp, "last_project.json")), \
                f"rejected switch must not write the marker ({bad})"
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_switch_project_busy_returns_409():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_proj_busy_")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        serve._configure(tmp, "default", port)
        serve._current = _FakeProc()
        code, data = _post(port, "/settings/switch-project", {"project": "acme"})
        assert code == 409, (code, data)
        assert not os.path.exists(os.path.join(tmp, "last_project.json"))
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_scan_projects_lists_valid_dirs_with_run_counts():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_proj_scan_")
    try:
        serve._configure(tmp, "default", 8787)
        for name, entries in (("proj-a", [{"ts": 100.0}, {"ts": 200.0}]),
                              ("proj-b", [{"ts": 50.0}])):
            os.makedirs(os.path.join(tmp, name, "runs"), exist_ok=True)
            with open(os.path.join(tmp, name, "runs", "index.jsonl"),
                      "w", encoding="utf-8") as f:
                for e in entries:
                    f.write(json.dumps(e) + "\n")
        os.makedirs(os.path.join(tmp, "bad name"))   # illegal: skipped
        projects = serve._scan_projects()
        names = [p["name"] for p in projects]
        # "default" exists because _configure creates its dirs; the sort
        # is what we pin: proj-a (last_ts 200) before proj-b (50) before
        # default (no index -> 0)
        assert names == ["proj-a", "proj-b", "default"], names
        by_name = {p["name"]: p for p in projects}
        assert by_name["proj-a"]["run_count"] == 2
        assert by_name["proj-b"]["run_count"] == 1
        assert by_name["proj-a"]["last_ts"] == 200.0
    finally:
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_settings_projects_endpoint():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_proj_ep_")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        serve._configure(tmp, "default", port)
        code, data = _get(port, "/settings/projects")
        assert code == 200, (code, data)
        assert data["current"] == serve.PROJECT == "default"
        names = [p["name"] for p in data["projects"]]
        assert "default" in names, names
        for p in data["projects"]:
            assert isinstance(p["run_count"], int) and p["run_count"] >= 0
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll project switcher tests passed.")
