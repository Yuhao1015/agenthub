"""Contract regression tests for the cost-trends card (CLAUDE.md §6.7 P1-6).

The card is pure frontend (settings.html aggregates the `runs` list from
GET /settings in the browser). There is no new backend logic, so these
tests pin the backend contract the frontend hardcodes: every runs entry
carries run_id/ts/goal/status/cost_usd, ts is Unix SECONDS (not millis),
and _runs_update merges fields instead of replacing the entry. A silent
drift in any of these makes the trend chart compute wrong dates/sums
without any error.

No framework, plain asserts. Zero real LLM calls.

Run: python3 tests/test_cost_trends.py
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve

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


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    r = conn.getresponse()
    data = json.loads(r.read() or b"{}")
    conn.close()
    return r.status, data


TREND_FIELDS = ("run_id", "ts", "goal", "status", "cost_usd")


def test_settings_runs_entries_have_trend_required_fields():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_trends_")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        serve._configure(tmp, "default", port)
        serve._runs_add("r1", "goal a")
        serve._runs_add("r2", "goal b")
        serve._runs_update("r1", status="done", cost_usd=0.1234)

        code, data = _get(port, "/settings")
        assert code == 200
        runs = data["runs"]
        assert len(runs) == 2, runs
        for e in runs:
            for k in TREND_FIELDS:
                assert k in e, f"{e} missing {k}"
        # The frontend does Number(r.ts) and new Date(ts*1000): a string
        # here would silently produce NaN/wrong dates with no error.
        assert isinstance(runs[0]["ts"], (int, float)), runs[0]
        assert isinstance(runs[0]["cost_usd"], (int, float)), runs[0]
        assert runs[0]["cost_usd"] == 0.1234, runs[0]
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_runs_add_ts_is_unix_seconds_not_millis():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_trends_ts_")
    try:
        serve._configure(tmp, "default", 8787)
        serve._runs_add("r1", "x")
        ts = serve._runs[-1]["ts"]
        # A millis regression (time.time()*1000) makes this diff ~1.7e12.
        assert abs(ts - time.time()) < 5, ts
    finally:
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_runs_update_preserves_other_fields():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_trends_merge_")
    try:
        serve._configure(tmp, "default", 8787)
        serve._runs_add("r1", "goal a")
        before = dict(serve._runs[-1])
        serve._runs_update("r1", cost_usd=0.5)
        after = serve._runs[-1]
        # _runs_update must merge, not replace: goal/ts/run_id survive.
        assert after["cost_usd"] == 0.5
        assert after["goal"] == before["goal"] == "goal a"
        assert after["ts"] == before["ts"]
        assert after["run_id"] == before["run_id"] == "r1"
        assert after["status"] == before["status"]
    finally:
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll cost-trends contract tests passed.")
