"""ui_prefs：config.yaml 只做 UI 记忆，生效值走 argv。

无框架，纯 assert。零 LLM 调用。

Run: python3 tests/test_ui_prefs.py
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
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve

MODEL_PREF_DEFAULTS = {'manager_model':'', 'manager_effort':'',
                       'manager_model_mode':'inherit', 'manager_effort_mode':'inherit'}


_GLOBALS = ("PORT", "MATRIX", "BOARD_PATH", "EVENTS_PATH", "WORKDIR", "DATA_DIR",
            "PROJECT", "RUNTIME_DIR", "RUNS_DIR", "INDEX_PATH", "CONFIG_PATH",
            "MERGED_MATRIX_PATH", "PREFLIGHT_PATH", "CREDENTIALS_PATH",
            "SCHEDULES_PATH", "_current", "_run_meta")


def _snap():
    return {"_runs": list(serve._runs), "_run_meta": dict(serve._run_meta),
            **{key: getattr(serve, key) for key in _GLOBALS}}


def _restore(snapshot):
    for key, value in snapshot.items():
        if key == "_runs":
            serve._runs = list(value)
        elif key == "_run_meta":
            serve._run_meta = dict(value)
        else:
            setattr(serve, key, value)


def _post(port, path, obj):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path, json.dumps(obj), {"Content-Type": "application/json"})
    response = conn.getresponse()
    data = json.loads(response.read() or b"{}")
    conn.close()
    return response.status, data


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    response = conn.getresponse()
    data = json.loads(response.read() or b"{}")
    conn.close()
    return response.status, data


def _start(tmp):
    server = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    serve._configure(tmp, "default", port)
    return server, port


def test_valid_prefs_round_trip():
    snapshot = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_prefs_ok_")
    try:
        server, port = _start(tmp)
        code, data = _post(port, "/settings/config", {"ui_prefs": {
            "manager_agent": "cc-kimi", "assign_mode": "manual", "gate_mode": "ask"}})
        assert code == 200, (code, data)
        assert serve._ui_prefs() == {"manager_agent": "cc-kimi",
                                     "assign_mode": "manual", "gate_mode": "ask",
                                     **MODEL_PREF_DEFAULTS}
        code, data = _get(port, "/settings")
        assert code == 200 and data["ui_prefs"]["gate_mode"] == "ask", data
    finally:
        server.shutdown()
        _restore(snapshot)
        shutil.rmtree(tmp, ignore_errors=True)


def test_defaults_when_never_set():
    snapshot = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_prefs_def_")
    try:
        serve._configure(tmp, "default", 8787)
        assert serve._ui_prefs() == {"manager_agent": "", "assign_mode": "auto",
                                     "gate_mode": "auto", **MODEL_PREF_DEFAULTS}
    finally:
        _restore(snapshot)
        shutil.rmtree(tmp, ignore_errors=True)


def test_invalid_values_rejected_and_nothing_persisted():
    snapshot = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_prefs_bad_")
    try:
        server, port = _start(tmp)
        for bad in ({"gate_mode": "maybe"}, {"assign_mode": "sometimes"},
                    {"manager_agent": "根本不存在的"}, {"manager_agent": 7},
                    {"gate_mode": True}):
            code, data = _post(port, "/settings/config", {"ui_prefs": bad})
            assert code == 400, (bad, code, data)
        code, data = _post(port, "/settings/config", {"ui_prefs": "不是对象"})
        assert code == 400, (code, data)
        assert serve._ui_prefs()["gate_mode"] == "auto"
    finally:
        server.shutdown()
        _restore(snapshot)
        shutil.rmtree(tmp, ignore_errors=True)


def test_budget_still_works_alongside_prefs():
    snapshot = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_prefs_reg_")
    try:
        server, port = _start(tmp)
        code, data = _post(port, "/settings/config", {
            "budget": 3.5, "ui_prefs": {"gate_mode": "ask"}})
        assert code == 200 and abs(data["budget"] - 3.5) < 1e-9, data
        assert serve._ui_prefs()["gate_mode"] == "ask"
    finally:
        server.shutdown()
        _restore(snapshot)
        shutil.rmtree(tmp, ignore_errors=True)


def test_gate_argv_reflects_prefs():
    snapshot = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_prefs_argv_")
    try:
        serve._configure(tmp, "default", 8787)
        assert serve._gate_argv() == ["--gate-mode=auto", "--assign-mode=auto"]
        serve._save_config({"ui_prefs": {"gate_mode": "ask", "assign_mode": "manual"}})
        assert serve._gate_argv() == ["--gate-mode=ask", "--assign-mode=manual"]
    finally:
        _restore(snapshot)
        shutil.rmtree(tmp, ignore_errors=True)


def test_scheduler_forces_both_modes_auto():
    snapshot = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_prefs_sched_")
    captured = {}
    original = serve._spawn_main_if_idle
    serve._spawn_main_if_idle = lambda tail: captured.setdefault("tail", tail) and 1
    try:
        serve._configure(tmp, "default", 8787)
        serve._save_config({"ui_prefs": {"gate_mode": "ask", "assign_mode": "manual"}})
        serve._schedule_fire({"id": "s1", "name": "n", "goal": "g", "freq": "daily",
                              "hour": 3, "minute": 0, "enabled": True,
                              "last_fired_date": None, "last_fired_status": None},
                             datetime.datetime(2026, 8, 30, 3, 0))
        tail = captured["tail"]
        assert "--gate-mode=auto" in tail, tail
        assert "--assign-mode=auto" in tail, tail
    finally:
        serve._spawn_main_if_idle = original
        _restore(snapshot)
        shutil.rmtree(tmp, ignore_errors=True)


def test_manager_agent_applied_and_auto_restores_original_route():
    snapshot = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_prefs_mgr_")

    class _StubRouter:
        def __init__(self):
            self.routes = {"manager": ["cc-sonnet-mgr", "cc-kimi"]}

    try:
        serve._configure(tmp, "default", 8787)
        router = _StubRouter()
        assert serve._apply_manager_pref(router) == ""
        assert router.routes["manager"] == ["cc-sonnet-mgr", "cc-kimi"]
        serve._save_config({"ui_prefs": {"manager_agent": "cc-kimi"}})
        assert serve._apply_manager_pref(router) == "cc-kimi"
        assert router.routes["manager"] == ["cc-kimi"]
        serve._save_config({"ui_prefs": {"manager_agent": ""}})
        assert serve._apply_manager_pref(router) == ""
        assert router.routes["manager"] == ["cc-sonnet-mgr", "cc-kimi"], (
            "对话中改回自动时必须恢复原始降级链"
        )
    finally:
        _restore(snapshot)
        shutil.rmtree(tmp, ignore_errors=True)


def test_argv_uses_equals_form():
    for arg in serve._gate_argv():
        assert arg.startswith("--") and "=" in arg, arg


if __name__ == "__main__":
    for fn in sorted(key for key in list(globals()) if key.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll ui-prefs tests passed.")
