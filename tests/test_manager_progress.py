"""经理思考进度（2026-08-31）。

**先说这个功能做不到什么**：`claude -p` 的 thinking 块内容是空字符串，
只有一个不透明签名 —— 2026-07-23 实测，2026-08-31 复测仍然如此：

    content_block_delta -> {"type":"thinking_delta","thinking":""}

所以**没有推理文本可以简述**。任何「经理正在考虑预算……」之类的文案都
只能是编的。能给的是推理的**形状**：想了几轮、每轮多久、中间读了什么。
各家 chatbot 折叠态给的「Thought for 12 seconds」就是这个东西。

传输走旁路轮询，对 /chat 零破坏：manager_step 把 on_event 透传下去，
serve.py 每个 session 挂一个有界缓冲，前端在等 /chat 响应期间轮询
GET /chat/progress。轮询挂了就是看不到进度，对话本身不受影响。

无框架，纯 assert。零 LLM 调用。

Run: python3 tests/test_manager_progress.py
"""
from __future__ import annotations

import asyncio
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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import serve
from test_manager import _CannedExecutor, _OneAgentRouter

_GLOBALS = ("PORT", "MATRIX", "BOARD_PATH", "EVENTS_PATH", "WORKDIR", "DATA_DIR",
            "PROJECT", "RUNTIME_DIR", "RUNS_DIR", "INDEX_PATH", "CONFIG_PATH",
            "MERGED_MATRIX_PATH", "PREFLIGHT_PATH", "CREDENTIALS_PATH",
            "SCHEDULES_PATH", "_current", "_run_meta")


def _snap():
    return {"_runs": list(serve._runs), "_run_meta": dict(serve._run_meta),
            "_sessions": dict(serve._sessions),
            **{k: getattr(serve, k) for k in _GLOBALS}}


def _restore(s):
    for k, v in s.items():
        if k == "_runs":
            serve._runs = list(v)
        elif k in ("_run_meta", "_sessions"):
            setattr(serve, k, dict(v))
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


# ---------- manager_step 透传 ----------

def test_manager_step_forwards_on_event_to_the_executor():
    from hub.manager import manager_step
    seen = []
    ex = _CannedExecutor(['{"status":"asking","question":"抓哪个网站？"}'])
    asyncio.run(manager_step([{"role": "user", "text": "g"}], _OneAgentRouter(ex),
                             ".", on_event=seen.append))
    assert ex.on_events and ex.on_events[0] is not None, \
        "manager_step 没把 on_event 传给执行器 —— 流式回调在经理这条路上是死的"


def test_manager_step_without_on_event_still_works():
    """回归：既有调用点（CLI 的 run_clarification_loop）不传 on_event。"""
    from hub.manager import manager_step
    ex = _CannedExecutor(['{"status":"asking","question":"抓哪个网站？"}'])
    step = asyncio.run(manager_step([{"role": "user", "text": "g"}],
                                    _OneAgentRouter(ex), "."))
    assert step["status"] == "asking" and step["question"] == "抓哪个网站？"
    assert ex.on_events == [None]


# ---------- 进度缓冲 ----------

def test_progress_buffer_records_steps_with_elapsed():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_prog_rec_")
    try:
        serve._configure(tmp, "default", 8787)
        sink = serve._progress_sink("sid1")
        sink("正在思考…")
        sink("Read(README.md)")
        steps = serve._progress_read("sid1")
        assert [x["detail"] for x in steps] == ["正在思考…", "Read(README.md)"]
        for x in steps:
            assert isinstance(x["ts"], float), x
    finally:
        _restore(s); shutil.rmtree(tmp, ignore_errors=True)


def test_progress_is_per_session_and_cleared_each_turn():
    """一轮只看这一轮的进度；别的 session 的绝不能串过来。"""
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_prog_iso_")
    try:
        serve._configure(tmp, "default", 8787)
        serve._progress_sink("A")("A 的第一步")
        serve._progress_sink("B")("B 的第一步")
        assert len(serve._progress_read("A")) == 1
        assert serve._progress_read("B")[0]["detail"] == "B 的第一步"
        serve._progress_reset("A")
        assert serve._progress_read("A") == []
        assert len(serve._progress_read("B")) == 1, "重置 A 不能动到 B"
    finally:
        _restore(s); shutil.rmtree(tmp, ignore_errors=True)


def test_progress_buffer_is_bounded():
    """经理会话是长期驻留的内存对象，进度不能无限增长。"""
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_prog_cap_")
    try:
        serve._configure(tmp, "default", 8787)
        sink = serve._progress_sink("sid")
        for i in range(serve.PROGRESS_MAX_STEPS * 3):
            sink(f"step {i}")
        got = serve._progress_read("sid")
        assert len(got) == serve.PROGRESS_MAX_STEPS, len(got)
        assert got[-1]["detail"] == f"step {serve.PROGRESS_MAX_STEPS * 3 - 1}", \
            "满了要丢最老的，保留最新的"
    finally:
        _restore(s); shutil.rmtree(tmp, ignore_errors=True)


# ---------- 端点 ----------

def test_progress_endpoint_returns_steps():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_prog_ep_")
    try:
        srv, port = _start(tmp)
        serve._progress_sink("sid9")("正在思考…")
        code, d = _get(port, "/chat/progress?session_id=sid9")
        assert code == 200, (code, d)
        assert [x["detail"] for x in d["steps"]] == ["正在思考…"]
    finally:
        srv.shutdown(); _restore(s); shutil.rmtree(tmp, ignore_errors=True)


def test_progress_endpoint_unknown_session_is_empty_not_an_error():
    """前端在拿到 session_id 之前就会开始轮询（第一轮还没返回 id）。
    那不是错误，是正常时序 —— 必须干净返回空，不能 404 让前端亮红。"""
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_prog_unknown_")
    try:
        srv, port = _start(tmp)
        code, d = _get(port, "/chat/progress?session_id="
                       + urllib.parse.quote("从来没有过的"))
        assert code == 200 and d["steps"] == [], (code, d)
        code, d = _get(port, "/chat/progress")
        assert code == 200 and d["steps"] == [], (code, d)
    finally:
        srv.shutdown(); _restore(s); shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll manager-progress tests passed.")
