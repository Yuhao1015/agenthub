"""POST /chat session flow, fully faked (no LLM, no real spawn).

Run: python tests/test_serve_chat.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

# macOS system proxy (e.g. MacPacket) hijacks urllib's localhost requests
# via getproxies(); force no proxy so these tests stay hermetic.
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
from hub import manager


@contextmanager
def isolated_chat_state():
    """Keep these legacy /chat tests independent of desktop persistence."""
    path_names = ("RUNTIME_DIR", "RUNS_DIR", "SESSIONS_DIR",
                  "ACTIVE_SESSION_PATH")
    saved_paths = {name: getattr(serve, name) for name in path_names}
    saved_sessions = serve._sessions
    saved_active_sid = serve._active_sid
    saved_inflight = serve._chat_inflight
    saved_chat_lock = serve._chat_lock
    saved_progress = serve._progress
    serve._sessions = {}
    serve._active_sid = ""
    serve._chat_inflight = set()
    serve._chat_lock = threading.RLock()
    serve._progress = {}
    for name in path_names:
        setattr(serve, name, "")
    try:
        yield
    finally:
        serve._sessions = saved_sessions
        serve._active_sid = saved_active_sid
        serve._chat_inflight = saved_inflight
        serve._chat_lock = saved_chat_lock
        serve._progress = saved_progress
        for name, value in saved_paths.items():
            setattr(serve, name, value)


def _post(port, path, obj):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(obj).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@isolated_chat_state()
def test_chat_flow():
    """asking -> confirm -> correction -> confirm -> '对' -> started.
    Also verifies manager_cost_usd accumulates across turns (finding #5)
    and that the spawned argv uses the '=' form (finding #1)."""
    scripted = [
        {"status": "asking", "question": "抓哪个网站？", "_cost_usd": 0.01},
        {"status": "ready", "goal": "G1", "constraints": "C1", "_cost_usd": 0.02},
        {"status": "ready", "goal": "G2", "constraints": "C2", "_cost_usd": 0.03},
    ]
    calls = {"n": 0}

    # 签名要跟 manager_step 一致：_handle_chat 现在会传 on_event（经理
    # 思考进度），漏了这个参数会 TypeError -> handler 抛异常 -> 连接被
    # 丢弃、无 HTTP 响应，症状跟「服务端没了」一模一样（CLAUDE.md §6.8）
    async def fake_step(history, router, workdir, force_finish=False, on_event=None):
        out = scripted[calls["n"]]
        calls["n"] += 1
        return out

    spawned = []
    original_step = manager.manager_step
    original_spawn = serve._spawn_main
    original_router = serve.Router
    manager.manager_step = fake_step
    serve._spawn_main = lambda tail: spawned.append(tail) or 4242
    serve.Router = lambda *a, **k: object()   # no real executors needed
    serve._sessions.clear()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        code, r1 = _post(port, "/chat", {"session_id": None, "message": "帮我写个爬虫"})
        assert code == 200 and r1["status"] == "asking" and r1["question"] == "抓哪个网站？"
        assert abs(r1["manager_cost_usd"] - 0.01) < 1e-9, f"got {r1}"
        sid = r1["session_id"]

        code, r2 = _post(port, "/chat", {"session_id": sid, "message": "抓 HN 首页"})
        assert code == 200 and r2["status"] == "confirm"
        assert (r2["goal"], r2["constraints"]) == ("G1", "C1")
        assert abs(r2["manager_cost_usd"] - 0.03) < 1e-9, \
            f"cost must accumulate across turns, got {r2}"

        # correction: not a confirmation -> back to the manager
        code, r3 = _post(port, "/chat", {"session_id": sid, "message": "不对，要抓评论区"})
        assert code == 200 and r3["status"] == "confirm" and r3["goal"] == "G2"
        assert abs(r3["manager_cost_usd"] - 0.06) < 1e-9, f"got {r3}"
        assert not spawned, "a correction must NOT spawn a run"

        code, r4 = _post(port, "/chat", {"session_id": sid, "message": "对"})
        assert code == 200 and r4["status"] == "started" and r4["pid"] == 4242
        assert abs(r4["manager_cost_usd"] - 0.06) < 1e-9, \
            "the final confirm turn makes no new manager_step call, cost must carry over"
        assert spawned == [["--goal=G2", "--constraints=C2", "--gate-mode=auto",
                            "--assign-mode=auto"]], f"got {spawned}"
        assert sid not in serve._sessions, "session must be dropped after start"

        code, r5 = _post(port, "/chat", {"session_id": None, "message": ""})
        assert code == 400
    finally:
        srv.shutdown()
        manager.manager_step = original_step
        serve._spawn_main = original_spawn
        serve.Router = original_router
    print("PASS: test_chat_flow")


@isolated_chat_state()
def test_handle_run_dash_leading_goal_uses_equals_form():
    """A goal starting with '-' must survive to the spawned argv as a
    single '--goal=...' token, never a separate token that argparse would
    misparse as another flag (main.py --goal -foo exits 2, silently, on
    the /run path). Verifies finding #1 on the /run side."""
    captured = {}

    class _FakeProc:
        def __init__(self, argv, **kw):
            captured["argv"] = argv
            self.pid = 5150

    original_current = serve._current
    original_popen = serve.subprocess.Popen
    serve.subprocess.Popen = _FakeProc
    serve._current = None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        dash_goal = "-不许用外部库，抓 HN 首页"
        code, r = _post(port, "/run", {"goal": dash_goal})
        assert code == 200 and r["pid"] == 5150, f"got {code} {r}"
        argv = captured["argv"]
        assert f"--goal={dash_goal}" in argv, f"expected a single '=' token, got {argv}"
        assert dash_goal not in argv, "dash-leading value must not be its own argv token"
    finally:
        srv.shutdown()
        serve.subprocess.Popen = original_popen
        serve._current = original_current
    print("PASS: test_handle_run_dash_leading_goal_uses_equals_form")


@isolated_chat_state()
def test_handle_chat_started_dash_leading_values_use_equals_form():
    """Same guarantee on the /chat -> confirm -> started path: the
    manager's proposal often phrases constraints as a bullet list
    ('-不许用外部库'), and that must not break argparse in the spawned
    main.py. Verifies finding #1 on the /chat side."""
    dash_goal = "-做一个落地页"
    dash_constraints = "-不许用外部库\n-预算500"
    scripted = [{"status": "ready", "goal": dash_goal, "constraints": dash_constraints,
                "_cost_usd": 0.0}]
    calls = {"n": 0}

    # 签名要跟 manager_step 一致：_handle_chat 现在会传 on_event（经理
    # 思考进度），漏了这个参数会 TypeError -> handler 抛异常 -> 连接被
    # 丢弃、无 HTTP 响应，症状跟「服务端没了」一模一样（CLAUDE.md §6.8）
    async def fake_step(history, router, workdir, force_finish=False, on_event=None):
        out = scripted[calls["n"]]
        calls["n"] += 1
        return out

    captured = {}

    class _FakeProc:
        def __init__(self, argv, **kw):
            captured["argv"] = argv
            self.pid = 6161

    original_step = manager.manager_step
    original_popen = serve.subprocess.Popen
    original_router = serve.Router
    original_current = serve._current
    manager.manager_step = fake_step
    serve.subprocess.Popen = _FakeProc
    serve.Router = lambda *a, **k: object()
    serve._current = None
    serve._sessions.clear()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        code, r1 = _post(port, "/chat", {"session_id": None, "message": "帮我做个落地页"})
        assert code == 200 and r1["status"] == "confirm", f"got {code} {r1}"
        sid = r1["session_id"]
        code, r2 = _post(port, "/chat", {"session_id": sid, "message": "对"})
        assert code == 200 and r2["status"] == "started", f"got {code} {r2}"
        argv = captured["argv"]
        assert f"--goal={dash_goal}" in argv, f"expected a single '=' token, got {argv}"
        assert f"--constraints={dash_constraints}" in argv, f"expected a single '=' token, got {argv}"
    finally:
        srv.shutdown()
        manager.manager_step = original_step
        serve.subprocess.Popen = original_popen
        serve.Router = original_router
        serve._current = original_current
    print("PASS: test_handle_chat_started_dash_leading_values_use_equals_form")


@isolated_chat_state()
def test_chat_409_when_run_in_progress():
    class _Alive:
        def poll(self):
            return None

    original = serve._current
    serve._current = _Alive()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        code, r = _post(port, "/chat", {"session_id": None, "message": "hi"})
        assert code == 409, f"expected 409 while a run is alive, got {code}"
    finally:
        srv.shutdown()
        serve._current = original
    print("PASS: test_chat_409_when_run_in_progress")


if __name__ == "__main__":
    test_chat_flow()
    test_chat_409_when_run_in_progress()
    test_handle_run_dash_leading_goal_uses_equals_form()
    test_handle_chat_started_dash_leading_values_use_equals_form()
    print("\nAll serve /chat tests passed.")
