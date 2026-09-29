"""Best-effort in-process scheduler (CLAUDE.md §6.7 P1 item 5, half B).

The firing loop is deliberately NOT started by _configure() -- tests call
_configure() directly and repeatedly, so a real timer thread there would
leak across every test in the process. _schedule_tick(now=...) is a pure
function of an injectable `now`, testable without sleeping.

No framework, plain asserts. Zero real LLM calls, no real subprocesses.

Run: python3 tests/test_scheduler.py
"""
from __future__ import annotations

import datetime
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


def _entry(**kw):
    base = {"id": "s1", "name": "周报", "goal": "生成本周周报",
            "freq": "daily", "hour": 9, "minute": 0, "enabled": True,
            "last_fired_date": None, "last_fired_status": None}
    base.update(kw)
    return base


# 2026-08-17 is a Monday (datetime.weekday() == 0)
MON = datetime.datetime(2026, 8, 17, 9, 2, 0)
TUE = datetime.datetime(2026, 8, 18, 9, 2, 0)


def test_day_matches():
    assert serve._schedule_day_matches(_entry(freq="daily"), TUE) is True
    assert serve._schedule_day_matches(_entry(freq="weekly", weekday=0), MON) is True
    assert serve._schedule_day_matches(_entry(freq="weekly", weekday=0), TUE) is False
    assert serve._schedule_day_matches(_entry(freq="monthly"), TUE) is False
    assert serve._schedule_day_matches(_entry(freq=None), TUE) is False


def test_seconds_past_due():
    e = _entry(hour=9, minute=0)
    assert serve._schedule_seconds_past_due(e, MON) == 120.0
    assert serve._schedule_seconds_past_due(e, MON.replace(hour=8, minute=59)) < 0
    for bad in (_entry(hour=None), _entry(hour=25), _entry(hour=-1),
                _entry(minute=60), _entry(minute="x"),
                {"freq": "daily", "hour": 9}):   # missing minute
        assert serve._schedule_seconds_past_due(bad, MON) is None, bad


def _setup_sched(tmp, entries):
    serve._configure(tmp, "default", 8787)
    serve._current = None
    serve._schedules_save(entries)


def test_tick_fires_due_and_idle():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_sched_fire_")
    try:
        _setup_sched(tmp, [_entry()])
        calls = []
        orig_spawn, orig_busy = serve._spawn_main, serve._run_busy
        serve._spawn_main = lambda argv: (calls.append(argv), 123)[1]
        serve._run_busy = lambda: False
        try:
            serve._schedule_tick(now=MON)
        finally:
            serve._spawn_main, serve._run_busy = orig_spawn, orig_busy
        assert calls == [["--goal=生成本周周报", "--gate-mode=auto",
                          "--assign-mode=auto"]], calls
        saved = serve._schedules_load()
        assert saved[0]["last_fired_status"] == "fired", saved
        assert saved[0]["last_fired_date"] == "2026-08-17", saved
    finally:
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_tick_busy_retries_within_grace():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_sched_busy_")
    try:
        _setup_sched(tmp, [_entry()])
        calls = []
        orig_spawn, orig_busy = serve._spawn_main, serve._run_busy
        serve._spawn_main = lambda argv: (calls.append(argv), 123)[1]
        serve._run_busy = lambda: True
        try:
            serve._schedule_tick(now=MON)
        finally:
            serve._run_busy = lambda: False
        assert calls == [], "busy tick must not fire"
        saved = serve._schedules_load()
        assert saved[0]["last_fired_date"] is None, "busy tick must not burn the verdict"
        # same `now` still inside the grace window: next tick fires
        try:
            serve._schedule_tick(now=MON)
        finally:
            serve._spawn_main, serve._run_busy = orig_spawn, orig_busy
        assert calls == [["--goal=生成本周周报", "--gate-mode=auto",
                          "--assign-mode=auto"]], calls
        assert serve._schedules_load()[0]["last_fired_status"] == "fired"
    finally:
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_tick_missed_window_is_recorded_not_fired():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_sched_miss_")
    try:
        _setup_sched(tmp, [_entry()])
        calls = []
        orig_spawn = serve._spawn_main
        serve._spawn_main = lambda argv: (calls.append(argv), 123)[1]
        try:
            serve._schedule_tick(now=MON.replace(minute=11))   # 11 min late
        finally:
            serve._spawn_main = orig_spawn
        assert calls == [], "missed windows are never fired"
        saved = serve._schedules_load()
        assert saved[0]["last_fired_status"] == "missed", saved
        assert saved[0]["last_fired_date"] == "2026-08-17"
    finally:
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_tick_skips_already_judged_today():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_sched_done_")
    try:
        _setup_sched(tmp, [_entry(last_fired_date="2026-08-17",
                                  last_fired_status="missed")])
        calls = []
        orig_spawn = serve._spawn_main
        serve._spawn_main = lambda argv: (calls.append(argv), 123)[1]
        try:
            serve._schedule_tick(now=MON)
        finally:
            serve._spawn_main = orig_spawn
        assert calls == []
        saved = serve._schedules_load()
        assert saved[0]["last_fired_status"] == "missed"   # unchanged
    finally:
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_tick_skips_wrong_weekday_and_disabled_and_dirty():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_sched_skip_")
    try:
        _setup_sched(tmp, [
            _entry(id="w", freq="weekly", weekday=0),      # Monday, now is Tuesday
            _entry(id="d", enabled=False),
            _entry(id="bad", hour=25),                      # dirty data
        ])
        calls = []
        orig_spawn = serve._spawn_main
        serve._spawn_main = lambda argv: (calls.append(argv), 123)[1]
        try:
            serve._schedule_tick(now=TUE)
        finally:
            serve._spawn_main = orig_spawn
        assert calls == []
        saved = serve._schedules_load()
        for e in saved:
            assert e["last_fired_date"] is None and e["last_fired_status"] is None, e
    finally:
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_tick_no_schedules_path_is_noop():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_sched_legacy_")
    try:
        serve.SCHEDULES_PATH = ""
        serve._schedule_tick(now=MON)   # must not raise, must not touch anything
    finally:
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_schedules_http_validation_and_crud():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_sched_http_")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        serve._configure(tmp, "default", port)
        ok = {"name": "周报", "goal": "生成周报", "freq": "weekly",
              "weekday": 0, "hour": 9, "minute": 0}
        for bad in (dict(ok, freq="monthly"),
                    dict(ok, hour=25),
                    dict(ok, minute=60),
                    dict(ok, weekday=9),
                    dict(ok, weekday="x"),
                    dict(ok, name="  "),
                    dict(ok, goal="")):
            code, data = _post(port, "/schedules", bad)
            assert code == 400, (bad, code, data)
        code, data = _post(port, "/schedules", ok)
        assert code == 200 and data["ok"] is True, (code, data)
        sid = data["schedules"][0]["id"]
        assert data["schedules"][0]["weekday"] == 0
        assert data["schedules"][0]["last_fired_date"] is None
        code, data = _get(port, "/schedules")
        assert code == 200 and len(data["schedules"]) == 1
        # daily needs no weekday
        code, data = _post(port, "/schedules",
                           {"name": "日报", "goal": "生成日报", "freq": "daily",
                            "hour": 18, "minute": 30})
        assert code == 200 and len(data["schedules"]) == 2
        assert "weekday" not in data["schedules"][1]
        code, data = _post(port, "/schedules/delete", {"id": "nope"})
        assert code == 404
        code, data = _post(port, "/schedules/delete", {"id": sid})
        assert code == 200 and len(data["schedules"]) == 1
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_configure_does_not_start_scheduler_thread():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_sched_nothread_")
    try:
        before = threading.enumerate()
        serve._configure(tmp, "default", 8787)
        after = threading.enumerate()
        # _configure must not spawn the daemon loop: tests call it directly
        # and repeatedly; a leaked timer thread would poison every test
        assert len(after) <= len(before), (before, after)
    finally:
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_spawn_main_if_idle_serializes_concurrent_callers():
    """Regression: the scheduler thread can now spawn a run on its own,
    independent of any HTTP request -- without _spawn_lock, two callers
    (say, an HTTP-triggered goal and a scheduled goal firing at the same
    moment) could both pass their own "is it busy?" check before either
    one's spawn takes effect, and both spawn an engine sharing one
    blackboard. _spawn_main_if_idle must make "check busy, then spawn"
    atomic: exactly one of N concurrent callers may win."""
    s = _snap()
    try:
        results = []
        busy_flag = {"on": False}
        orig_busy, orig_spawn = serve._run_busy, serve._spawn_main

        def fake_busy():
            return busy_flag["on"]

        def fake_spawn(argv):
            # widen the race window between the busy check and the
            # "become busy" side effect -- without the lock, concurrent
            # callers would all observe fake_busy() == False before any
            # of them gets here
            time.sleep(0.05)
            busy_flag["on"] = True
            return 999

        serve._run_busy, serve._spawn_main = fake_busy, fake_spawn
        try:
            def worker():
                results.append(serve._spawn_main_if_idle(["--goal=x"]))
            threads = [threading.Thread(target=worker) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            serve._run_busy, serve._spawn_main = orig_busy, orig_spawn
        fired = [r for r in results if r is not None]
        assert len(fired) == 1, f"expected exactly one winner, got {results}"
        assert results.count(None) == 4, results
    finally:
        _restore(s)


if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll scheduler tests passed.")
