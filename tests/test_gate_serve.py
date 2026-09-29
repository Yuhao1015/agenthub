"""POST /gate 与 GET /agents（spec §4.4/§4.5）。

无框架，纯 assert。零 LLM 调用。

Run: python3 tests/test_gate_serve.py
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


def _req(port, method, path, obj=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    if obj is None:
        conn.request(method, path)
    else:
        conn.request(method, path, json.dumps(obj), {"Content-Type": "application/json"})
    response = conn.getresponse()
    data = json.loads(response.read() or b"{}")
    conn.close()
    return response.status, data


def _start(tmp, with_run=True):
    server = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    serve._configure(tmp, "default", port)
    if with_run:
        serve._runs_add("r1", "goal")
        serve._adopt_latest_run()
        run_dir = os.path.join(serve.RUNS_DIR, "r1")
        os.makedirs(run_dir, exist_ok=True)
        board = Blackboard(os.path.join(run_dir, "blackboard.jsonl"),
                           on_write=lambda event: None)
        board.put("run/gate/plan", {"kind": "plan"}, author="engine")
    return server, port


def test_approve_writes_gate_reply_to_blackboard():
    snapshot = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_gs_ok_")
    try:
        server, port = _start(tmp)
        code, data = _req(port, "POST", "/gate", {
            "gate_id": "plan", "decision": "approve", "overrides": {"t1": "cc-opus"}})
        assert code == 200 and data["ok"] is True, (code, data)
        board = Blackboard(serve.BOARD_PATH, on_write=lambda event: None)
        assert board.get("run/gate_reply/plan") == {
            "decision": "approve", "overrides": {"t1": "cc-opus"}}
    finally:
        server.shutdown()
        _restore(snapshot)
        shutil.rmtree(tmp, ignore_errors=True)


def test_rejects_bad_gate_id_decision_and_override():
    snapshot = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_gs_bad_")
    try:
        server, port = _start(tmp)
        for body in ({"gate_id": "", "decision": "approve"},
                     {"gate_id": "../etc/passwd", "decision": "approve"},
                     {"gate_id": "task:a/b", "decision": "approve"},
                     {"gate_id": "plan", "decision": "maybe"},
                     {"gate_id": "plan", "decision": "skip"},
                     {"gate_id": "plan", "decision": "approve", "overrides": []},
                     {"gate_id": "plan", "decision": "approve",
                      "overrides": {"t1": "不存在的 agent"}}):
            code, data = _req(port, "POST", "/gate", body)
            assert code == 400, (body, code, data)
    finally:
        server.shutdown()
        _restore(snapshot)
        shutil.rmtree(tmp, ignore_errors=True)


def test_task_gate_accepts_skip_when_that_gate_is_open():
    snapshot = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_gs_skip_")
    try:
        server, port = _start(tmp)
        board = Blackboard(serve.BOARD_PATH, on_write=lambda event: None)
        board.put("run/gate/task:t3", {"kind": "task"}, author="engine")
        code, data = _req(port, "POST", "/gate",
                          {"gate_id": "task:t3", "decision": "skip"})
        assert code == 200, (code, data)
    finally:
        server.shutdown()
        _restore(snapshot)
        shutil.rmtree(tmp, ignore_errors=True)


def test_unopened_or_already_answered_gate_returns_409():
    snapshot = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_gs_state_")
    try:
        server, port = _start(tmp)
        code, data = _req(port, "POST", "/gate",
                          {"gate_id": "task:future", "decision": "skip"})
        assert code == 409 and "没有打开" in data["error"], (code, data)
        body = {"gate_id": "plan", "decision": "approve"}
        assert _req(port, "POST", "/gate", body)[0] == 200
        code, data = _req(port, "POST", "/gate", body)
        assert code == 409 and "已经回答" in data["error"], (code, data)
    finally:
        server.shutdown()
        _restore(snapshot)
        shutil.rmtree(tmp, ignore_errors=True)


def test_gate_token_rejects_stale_tab_and_allows_new_instance_reply():
    snapshot = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_gs_token_")
    try:
        server, port = _start(tmp)
        board = Blackboard(serve.BOARD_PATH, on_write=lambda event: None)
        old = "1111111111111111"
        new = "2222222222222222"
        board.put("run/gate_reply/plan",
                  {"decision": "approve", "gate_token": old}, author="user")
        board.put("run/gate/plan", {"kind": "plan", "gate_token": new},
                  author="engine")

        code, data = _req(port, "POST", "/gate", {
            "gate_id": "plan", "gate_token": old, "decision": "approve"})
        assert code == 409 and "过期" in data["error"], (code, data)

        code, data = _req(port, "POST", "/gate", {
            "gate_id": "plan", "gate_token": new, "decision": "cancel"})
        assert code == 200, (code, data)
        latest = Blackboard(serve.BOARD_PATH, on_write=lambda event: None)
        assert latest.get("run/gate_reply/plan") == {
            "decision": "cancel", "overrides": {}, "gate_token": new}
    finally:
        server.shutdown()
        _restore(snapshot)
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_run_returns_404_not_a_dead_connection():
    snapshot = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_gs_norun_")
    try:
        server, port = _start(tmp, with_run=False)
        code, data = _req(port, "POST", "/gate",
                          {"gate_id": "plan", "decision": "approve"})
        assert code == 404 and "error" in data, (code, data)
    finally:
        server.shutdown()
        _restore(snapshot)
        shutil.rmtree(tmp, ignore_errors=True)


def test_agents_endpoint_shape_and_honest_precision_label():
    snapshot = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_gs_agents_")
    original_which = serve.shutil.which
    try:
        serve.shutil.which = lambda name: (
            "/usr/local/bin/codex" if name == "codex" else original_which(name))
        server, port = _start(tmp)
        code, data = _req(port, "GET", "/agents")
        assert code == 200, (code, data)
        names = [agent["name"] for agent in data["agents"]]
        assert "cc-sonnet" in names and "mock" in names, names
        one = next(agent for agent in data["agents"] if agent["name"] == "cc-sonnet")
        for field in ("name", "kind", "animal", "tier", "specialty", "blurb",
                      "installed", "detail"):
            assert field in one, f"{field} 缺失: {one}"
        assert one["kind"] == "claude_code", one
        codex = next(agent for agent in data["agents"]
                     if agent["name"] == "codex-local")
        assert codex["kind"] == "codex", codex
        assert codex["installed"] is True, codex
        assert data["precision"] == "kind" and data["note"], data
    finally:
        serve.shutil.which = original_which
        server.shutdown()
        _restore(snapshot)
        shutil.rmtree(tmp, ignore_errors=True)


def test_open_gate_id_reported_in_busy_message():
    snapshot = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_gs_busy_")

    class _Alive:
        def poll(self):
            return None

    try:
        server, port = _start(tmp)
        serve._current = _Alive()
        assert serve._open_gate_id() == "plan", serve._open_gate_id()
        code, data = _req(port, "POST", "/run", {"goal": "x"})
        assert code == 409, (code, data)
        assert "plan" in data["error"] and "权限" in data["error"], data
    finally:
        server.shutdown()
        _restore(snapshot)
        shutil.rmtree(tmp, ignore_errors=True)


def test_every_busy_409_explains_the_stuck_gate():
    """卡住的门堵的是**所有**入口，不只是 POST /run。

    切项目和预检也会被 _run_busy() 挡住，而它们原本回的是干巴巴一句
    "a run is already in progress" —— 用户完全查不到真正的原因是有一道
    门等着他放行。§3.5 已知缺口的缓解必须覆盖每个入口，只覆盖一个等于
    留下两条查不到原因的死路。
    """
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_gs_busy_all_")

    class Alive:
        def poll(self):
            return None

    try:
        srv, port = _start(tmp)
        serve._current = Alive()
        for method, path, body in (
                ("POST", "/run", {"goal": "x"}),
                ("POST", "/settings/switch-project", {"project": "other"}),
                ("POST", "/settings/preflight", {}),
        ):
            code, d = _req(port, method, path, body)
            assert code == 409, (path, code, d)
            assert "plan" in d["error"], \
                f"{path} 的 409 没说出卡在哪道门：{d['error']!r}"
    finally:
        srv.shutdown(); _restore(s); shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for fn in sorted(key for key in list(globals()) if key.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll gate-serve tests passed.")
