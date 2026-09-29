"""GET /settings/search (CLAUDE.md §6.7 P1 item 4): cross-run full-text
search over goal / task titles / notes / deliverable file names, linear
scan, no index, tasks/*/output deliberately out of scope.

The trickiest correctness point: task_title results must parse task_id
from the blackboard KEY (tasks/{id}/spec writes don't pass task_id, so
the entry field is None) while file/note results read entry.get("task_id").

No framework, plain asserts. Zero real LLM calls.

Run: python3 tests/test_cross_run_search.py
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.parse
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


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
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


def _make_run_dir(run_id):
    d = os.path.join(serve.RUNS_DIR, run_id)
    os.makedirs(d, exist_ok=True)
    return d


def test_search_matches_all_four_fields():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_search_")
    try:
        srv, port = _start(tmp)
        serve._runs_add("r1", "生成本周 CSV 报表")
        serve._runs_add("r2", "完全无关的目标")
        b = Blackboard(os.path.join(_make_run_dir("r1"), "blackboard.jsonl"),
                       on_write=lambda e: None)
        # spec writes don't pass task_id (mirrors hub/engine.py exactly)
        b.put("tasks/t1/spec", {"title": "汇总 CSV 数据", "type": "coding"},
              author="system", kind="spec")
        b.put("tasks/t1/files", [{"path": "weekly_active.csv", "size": 5},
                                 {"path": "notes.txt", "size": 3}],
              author="engine", task_id="t1", kind="fact")
        b.add_note("导出时发现 csv 编码瓶颈", author="cc-sonnet", task_id="t2")

        code, data = _get(port, "/settings/search?q=csv")
        assert code == 200, (code, data)
        results = data["results"]
        kinds = {r["matched_in"] for r in results}
        assert kinds == {"goal", "task_title", "note", "file"}, kinds
        goal_row = [r for r in results if r["matched_in"] == "goal"][0]
        assert goal_row["run_id"] == "r1" and goal_row["task_id"] is None
        assert "CSV" in goal_row["snippet"]
        title_row = [r for r in results if r["matched_in"] == "task_title"][0]
        # THE key correctness point: parsed from the key, not a None field
        assert title_row["task_id"] == "t1", title_row
        file_row = [r for r in results if r["matched_in"] == "file"][0]
        assert file_row["task_id"] == "t1"
        assert file_row["snippet"] == "weekly_active.csv"
        note_row = [r for r in results if r["matched_in"] == "note"][0]
        assert note_row["task_id"] == "t2"
        assert "编码" in note_row["snippet"]
        # every row carries the run-level fields for render-without-lookup
        for r in results:
            for k in ("run_id", "goal", "ts", "status", "cost_usd", "snippet"):
                assert k in r, r
        assert data["total_runs_scanned"] == 2 and data["truncated"] is False
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_search_is_case_insensitive():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_search_ci_")
    try:
        srv, port = _start(tmp)
        serve._runs_add("r1", "生成本周 CSV 报表")
        _, up = _get(port, "/settings/search?q=CSV")
        _, low = _get(port, "/settings/search?q=csv")
        up_keys = {(r["matched_in"], r["task_id"], r["snippet"]) for r in up["results"]}
        low_keys = {(r["matched_in"], r["task_id"], r["snippet"]) for r in low["results"]}
        assert up_keys == low_keys and up_keys, (up, low)
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_search_empty_q_returns_400():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_search_400_")
    try:
        srv, port = _start(tmp)
        for path in ("/settings/search?q=", "/settings/search?q=%20%20"):
            code, data = _get(port, path)
            assert code == 400, (path, code)
            assert data["error"] == "need q", data
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_search_missing_blackboard_run_is_skipped():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_search_miss_")
    try:
        srv, port = _start(tmp)
        serve._runs_add("r1", "目标包含报告这个词")
        # no RUNS_DIR/r1/blackboard.jsonl on disk: only goal can match
        code, data = _get(port, "/settings/search?q=" + urllib.parse.quote("报告"))
        assert code == 200
        assert len(data["results"]) == 1
        assert data["results"][0]["matched_in"] == "goal"
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_search_legacy_mode_returns_empty():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_search_legacy_")
    try:
        serve.RUNS_DIR = ""
        serve._runs = []
        srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            code, data = _get(port, "/settings/search?q=anything")
            assert code == 200, code
            assert data["results"] == [] and data["total_runs_scanned"] == 0
            assert data["truncated"] is False
        finally:
            srv.shutdown()
    finally:
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_search_soft_cap():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_search_cap_")
    try:
        srv, port = _start(tmp)
        # r0 carries the 50-file burst; 190 more goal runs. Scan order is
        # newest-first, so goals (190) accumulate, then r0 adds 51 more
        # (1 goal + 50 files) = 241: crosses the cap with per-run overshoot.
        serve._runs_add("r0", "report 目标 0")
        for i in range(190):
            serve._runs_add(f"r{i + 1}", f"report 目标 {i}")
        b = Blackboard(os.path.join(_make_run_dir("r0"), "blackboard.jsonl"),
                       on_write=lambda e: None)
        b.put("tasks/t1/files", [{"path": f"report_{i}.csv", "size": 1} for i in range(50)],
              author="engine", task_id="t1", kind="fact")
        code, data = _get(port, "/settings/search?q=report")
        assert code == 200
        assert data["truncated"] is True
        # soft cap: never thousands of rows; may overshoot slightly for a
        # single run's burst, so assert a generous bound, not the exact cap
        assert len(data["results"]) <= serve.MAX_SEARCH_RESULTS + 60, len(data["results"])
        assert len(data["results"]) >= serve.MAX_SEARCH_RESULTS
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll cross-run-search tests passed.")
