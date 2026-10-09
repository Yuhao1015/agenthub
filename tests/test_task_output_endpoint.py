"""Bug B7 (docs/decisions/gaps-2026-09-25.md): a task's full output must be
readable from the dashboard. task_completed only carries a 200-char preview,
and that preview was all the drawer's 「产出全文」 ever showed -- for
writing/analysis/review tasks the text IS the deliverable.

GET /task/output?task=<id>&run=<run id> reads tasks/<id>/output off that
run's blackboard. `run` matters because planner task ids (t1, t2, ...)
repeat in every run.

Run: python tests/test_task_output_endpoint.py
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
from hub.blackboard import Blackboard

_GLOBALS = ("PORT", "MATRIX", "BOARD_PATH", "EVENTS_PATH", "WORKDIR", "DATA_DIR",
            "PROJECT", "RUNTIME_DIR", "RUNS_DIR", "INDEX_PATH", "CONFIG_PATH",
            "MERGED_MATRIX_PATH", "PREFLIGHT_PATH", "CREDENTIALS_PATH",
            "SCHEDULES_PATH", "SESSIONS_DIR", "ACTIVE_SESSION_PATH", "_current")


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    response = conn.getresponse()
    body = response.read()
    conn.close()
    try:
        return response.status, json.loads(body)
    except ValueError:
        return response.status, {"_raw": body[:80]}


def test_full_output_per_run():
    saved = {"_runs": list(serve._runs), "_run_meta": dict(serve._run_meta),
             **{key: getattr(serve, key) for key in _GLOBALS}}
    tmp = tempfile.mkdtemp(prefix="ah_b7_")
    server = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    long_old = "旧运行的完整报告。" * 200       # far past the 200-char preview
    long_new = "新运行的完整报告。" * 150
    try:
        serve._configure(tmp, "default", port)
        for run_id, text in (("aaaa0001", long_old), ("aaaa0002", long_new)):
            serve._runs_add(run_id, "goal")
            run_dir = os.path.join(serve.RUNS_DIR, run_id)
            os.makedirs(run_dir, exist_ok=True)
            Blackboard(os.path.join(run_dir, "blackboard.jsonl"),
                       on_write=lambda e: None).put(
                "tasks/t1/output", text, author="cc-sonnet", task_id="t1",
                kind="output")
        serve._adopt_latest_run()           # current run = aaaa0002

        code, data = _get(port, "/task/output?task=t1&run=aaaa0001")
        assert code == 200 and data["output"] == long_old, (code, str(data)[:120])
        code, data = _get(port, "/task/output?task=t1&run=aaaa0002")
        assert code == 200 and data["output"] == long_new, (code, str(data)[:120])
        code, data = _get(port, "/task/output?task=t1")
        assert code == 200 and data["output"] == long_new, "no run = the current run"

        assert _get(port, "/task/output?task=t9&run=aaaa0001")[0] == 404
        assert _get(port, "/task/output?task=t1&run=..%2F..")[0] == 404
        assert _get(port, "/task/output?task=a%2Fb")[0] == 400
    finally:
        server.shutdown()
        for key, value in saved.items():
            if key == "_runs":
                serve._runs = list(value)
            elif key == "_run_meta":
                serve._run_meta = dict(value)
            else:
                setattr(serve, key, value)
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS: test_full_output_per_run")


if __name__ == "__main__":
    test_full_output_per_run()
