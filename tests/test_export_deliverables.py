"""GET /export?run=<id> (CLAUDE.md §6.7 P0 item 3): one zip with this run's
deliverables (grouped by task id) + a templated SUMMARY.md, no LLM call.

Security core: the run id comes from the query string and IS joined into a
filesystem path, so _resolve_run must contain it (realpath prefix check) --
this is the attack surface GET /file never had. File-level reads reuse the
same whitelist + realpath containment as GET /file.

No framework, plain asserts. Zero real LLM calls.

Run: python3 tests/test_export_deliverables.py
"""
from __future__ import annotations

import http.client
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import zipfile
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
from hub.blackboard import Blackboard

_GLOBALS = ("PORT", "MATRIX", "BOARD_PATH", "EVENTS_PATH", "WORKDIR", "DATA_DIR",
            "PROJECT", "RUNTIME_DIR", "RUNS_DIR", "INDEX_PATH", "CONFIG_PATH",
            "MERGED_MATRIX_PATH", "PREFLIGHT_PATH", "CREDENTIALS_PATH",
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


def _get_raw(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    r = conn.getresponse()
    body = r.read()
    headers = {k.lower(): v for k, v in r.getheaders()}
    conn.close()
    return r.status, headers, body


def _write_board(path, entries, workdir):
    """entries: dict key -> value, written as engine-authored facts."""
    b = Blackboard(path, on_write=lambda e: None)
    for k, v in entries.items():
        b.put(k, v, author="engine")
    b.put("run/workdir", workdir, author="engine")
    return b


def _start(tmp, project="default"):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    serve._configure(tmp, project, port)
    return srv, port


def test_export_zip_contains_summary_and_grouped_deliverables():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_export_")
    try:
        srv, port = _start(tmp)
        workdir = os.path.join(tmp, "wd")
        os.makedirs(workdir, exist_ok=True)
        with open(os.path.join(workdir, "out.txt"), "w", encoding="utf-8") as f:
            f.write("hello export deliverable")
        serve._runs_add("r1", "test goal")
        serve._adopt_latest_run()
        assert serve._run_meta["id"] == "r1"
        run_dir = os.path.join(serve.RUNS_DIR, "r1")
        os.makedirs(run_dir, exist_ok=True)   # normally _spawn_main creates it
        board_path = os.path.join(run_dir, "blackboard.jsonl")
        _write_board(board_path, {
            "goal/statement": "test goal statement",
            "tasks/t1/spec": {"title": "任务 A", "type": "coding"},
            "tasks/t1/files": [{"path": "out.txt", "size": 24}],
            "tasks/t2/spec": {"title": "任务 B", "type": "qa"},
            "tasks/t2/files": [],
        }, workdir)
        events_path = os.path.join(run_dir, "events.jsonl")
        with open(events_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "task_completed", "task_id": "t1",
                                "agent": "cc-sonnet", "cost_usd": 0.5,
                                "cost_credits": 0.0}) + "\n")
            f.write(json.dumps({"type": "task_completed", "task_id": "t2",
                                "agent": "cc-opus", "cost_usd": 1.25,
                                "cost_credits": 0.0}) + "\n")
            f.write(json.dumps({"type": "run_finished", "total_cost_usd": 1.75,
                                "paid_api_cost_usd": 1.75, "total_credits": 0.0,
                                "statuses": {"t1": "done", "t2": "done"}}) + "\n")

        for path in ("/export?run=r1", "/export"):
            code, headers, body = _get_raw(port, path)
            assert code == 200, (path, code, body[:200])
            assert headers.get("content-type") == "application/zip", headers
            zf = zipfile.ZipFile(io.BytesIO(body))
            names = zf.namelist()
            assert "SUMMARY.md" in names, names
            assert "deliverables/t1/out.txt" in names, names
            assert zf.read("deliverables/t1/out.txt") == b"hello export deliverable"
            summary = zf.read("SUMMARY.md").decode("utf-8")
            assert "test goal statement" in summary
            assert "任务 A" in summary and "任务 B" in summary
            assert "$1.7500" in summary   # run_finished total
            assert "cc-sonnet" in summary and "cc-opus" in summary
            assert "完成 全部交付" in summary
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_export_rejects_run_id_path_traversal():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_export_sec_")
    try:
        srv, port = _start(tmp)
        for path in ("/export?run=../../../../etc",
                     "/export?run=%2e%2e%2f%2e%2e%2fetc",
                     "/export?run=..%2f..%2f..%2f..%2fetc"):
            code, headers, body = _get_raw(port, path)
            assert code == 404, (path, code)
            # no leakage: body must be exactly the JSON error, not any
            # content from a directory outside RUNS_DIR
            assert json.loads(body) == {"error": "run not found"}, body[:200]
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_export_skips_missing_file_without_failing_whole_request():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_export_miss_")
    try:
        srv, port = _start(tmp)
        workdir = os.path.join(tmp, "wd")
        os.makedirs(workdir, exist_ok=True)
        with open(os.path.join(workdir, "out.txt"), "w", encoding="utf-8") as f:
            f.write("present")
        serve._runs_add("r1", "test goal")
        serve._adopt_latest_run()
        run_dir = os.path.join(serve.RUNS_DIR, "r1")
        os.makedirs(run_dir, exist_ok=True)
        _write_board(os.path.join(run_dir, "blackboard.jsonl"), {
            "goal/statement": "test goal statement",
            "tasks/t1/spec": {"title": "任务 A", "type": "coding"},
            "tasks/t1/files": [{"path": "out.txt", "size": 7},
                               {"path": "gone.txt", "size": 1}],
        }, workdir)
        with open(os.path.join(run_dir, "events.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "run_finished", "total_cost_usd": 0.0,
                                "paid_api_cost_usd": 0.0, "total_credits": 0.0,
                                "statuses": {}}) + "\n")
        code, _, body = _get_raw(port, "/export?run=r1")
        assert code == 200, code
        zf = zipfile.ZipFile(io.BytesIO(body))
        assert "deliverables/t1/out.txt" in zf.namelist()
        assert "deliverables/t1/gone.txt" not in zf.namelist()
        summary = zf.read("SUMMARY.md").decode("utf-8")
        index = summary.split("## 交付文件索引")[1]
        assert "out.txt" in index
        assert "gone.txt" not in index   # honest absence, not silent
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_export_run_not_found_returns_404():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_export_404_")
    try:
        srv, port = _start(tmp)
        code, _, body = _get_raw(port, "/export?run=does-not-exist")
        assert code == 404, code
        assert json.loads(body) == {"error": "run not found"}
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_export_legacy_mode_ignores_run_param():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_export_legacy_")
    try:
        board_path = os.path.join(tmp, "blackboard.jsonl")
        events_path = os.path.join(tmp, "events.jsonl")
        serve.BOARD_PATH = board_path
        serve.EVENTS_PATH = events_path
        serve.RUNS_DIR = ""
        _write_board(board_path, {
            "goal/statement": "legacy goal",
            "tasks/t1/spec": {"title": "任务 L", "type": "coding"},
            "tasks/t1/files": [],
        }, tmp)
        with open(events_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "task_completed", "task_id": "t1",
                                "agent": "cc-sonnet", "cost_usd": 0.1,
                                "cost_credits": 0.0}) + "\n")
        srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            code, headers, body = _get_raw(port, "/export?run=anything")
            assert code == 200, code
            assert headers.get("content-type") == "application/zip"
            zf = zipfile.ZipFile(io.BytesIO(body))
            assert "legacy goal" in zf.read("SUMMARY.md").decode("utf-8")
        finally:
            srv.shutdown()
    finally:
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll export-deliverables tests passed.")
