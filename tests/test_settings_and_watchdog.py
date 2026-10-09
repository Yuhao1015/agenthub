"""settings endpoints: GET /settings, GET /settings/agents (existence probe,
zero subprocess), POST /settings/config, POST /settings/credentials, and the
run watchdog (brief 4.3: crashed runs must surface a run_crashed event and
an honest index status instead of the UI spinning on "running" forever).

No framework, plain asserts. Zero real LLM calls.

Run: python3 tests/test_settings_and_watchdog.py
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


def test_probe_agents_zero_subprocess():
    """Existence probe must never spawn a subprocess or make an LLM call."""
    # 打桩点必须是 _cli_which / _codex_cmd，不是 shutil.which：探测现在走
    # hub.executors._which，它在 PATH 之外还会翻 _EXTRA_BIN_DIRS（GUI 启动
    # 时看不见 PATH 的补救，见 tests/test_gui_path_resolution.py）。只桩
    # shutil.which 的话，跑测试的机器上真装了 codex/dsh 就会漏进来。
    orig_which = serve._cli_which
    orig_popen = serve.subprocess.Popen
    orig_resolve = serve._resolve_codebuddy_paths
    orig_dsh = serve._dsh_cmd
    orig_codex = serve._codex_cmd

    def boom(*a, **kw):
        raise AssertionError("probe must not spawn subprocesses")

    serve.subprocess.Popen = boom
    serve._cli_which = lambda name: "/usr/bin/claude" if name == "claude" else None
    serve._resolve_codebuddy_paths = lambda a, b: (None, None)
    serve._dsh_cmd = lambda p: ["/usr/local/bin/dsh"]
    serve._codex_cmd = lambda p: []
    try:
        agents = serve._probe_agents()
    finally:
        serve.subprocess.Popen = orig_popen
        serve._cli_which = orig_which
        serve._resolve_codebuddy_paths = orig_resolve
        serve._dsh_cmd = orig_dsh
        serve._codex_cmd = orig_codex

    by_id = {a["id"]: a for a in agents}
    assert by_id["claude_code"]["available"] is True
    assert by_id["dsh"]["available"] is True
    assert by_id["codebuddy"]["available"] is False
    assert by_id["codex"]["available"] is False
    assert by_id["git"]["available"] is False and by_id["git"]["detail"], by_id["git"]
    for a in agents:
        for field in ("id", "label", "kind", "available", "detail"):
            assert field in a, f"{a} missing {field}"
    assert by_id["codebuddy"]["detail"], "uninstalled agents need install guidance"
    assert by_id["codex"]["detail"]


def test_settings_endpoints_desktop_roundtrip():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_settings_")
    orig_env = {k: os.environ[k] for k in ("DEEPSEEK_API_KEY", "OPENROUTER_API_KEY")
                if k in os.environ}
    for k in ("DEEPSEEK_API_KEY", "OPENROUTER_API_KEY"):
        os.environ.pop(k, None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        serve._configure(tmp, "default", port)
        code, data = _get(port, "/settings")
        assert code == 200
        assert data["data_dir"] == tmp and data["budget"] == 2.0
        assert len(data["agents"]) == 5 and data["runs"] == []
        assert data["credentials"] == {"deepseek": False, "openrouter": False}

        code, data = _get(port, "/settings/agents")
        assert code == 200 and len(data["agents"]) == 5

        code, data = _post(port, "/settings/config", {"budget": 3.5})
        assert code == 200 and abs(data["budget"] - 3.5) < 1e-9
        import yaml
        merged = yaml.safe_load(open(serve.MERGED_MATRIX_PATH, encoding="utf-8"))
        assert abs(merged["budget"]["max_usd_per_run"] - 3.5) < 1e-9

        code, data = _post(port, "/settings/credentials", {"deepseek": "sk-abc"})
        assert code == 200 and data["deepseek"] is True
        mode = os.stat(serve.CREDENTIALS_PATH).st_mode & 0o777
        if os.name == 'nt':
            from hub.connection_store import verify_private
            verify_private(serve.CREDENTIALS_PATH)
        else:
            assert mode == 0o600, oct(mode)

        code, data = _get(port, "/settings")
        assert abs(data["budget"] - 3.5) < 1e-9
        assert data["credentials"]["deepseek"] is True
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)
        for k in ("DEEPSEEK_API_KEY", "OPENROUTER_API_KEY"):
            os.environ.pop(k, None)
        for k, v in orig_env.items():
            os.environ[k] = v


class _FakeProc:
    def __init__(self, returncode):
        self.returncode = returncode

    def wait(self):
        return self.returncode


def test_watch_run_crash_without_run_finished():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_watch_")
    try:
        serve._configure(tmp, "default", 8787)
        serve._runs_add("r1", "boom")
        events = os.path.join(serve.RUNS_DIR, "r1", "events.jsonl")
        os.makedirs(os.path.dirname(events), exist_ok=True)
        with open(events, "w", encoding="utf-8") as f:
            f.write("")   # no run_finished anywhere
        serve.EVENTS_PATH = events
        serve._watch_run("r1", _FakeProc(1), events, {"cancel_requested": False})
        serve._runs_load()
        assert serve._runs[0]["status"] == "crashed", serve._runs
        text = open(events, encoding="utf-8").read()
        assert '"type": "run_crashed"' in text, text
        assert "engine exited with code 1" in text, text
    finally:
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_watch_run_normal_finish_records_done_and_cost():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_watch2_")
    try:
        serve._configure(tmp, "default", 8787)
        serve._runs_add("r1", "ok")
        events = os.path.join(serve.RUNS_DIR, "r1", "events.jsonl")
        os.makedirs(os.path.dirname(events), exist_ok=True)
        with open(events, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "run_finished", "total_cost_usd": 0.5,
                                "statuses": {"t1": "done"}}) + "\n")
        serve.EVENTS_PATH = events
        serve._watch_run("r1", _FakeProc(0), events, {"cancel_requested": False})
        serve._runs_load()
        e = serve._runs[0]
        assert e["status"] == "done" and abs(e["cost_usd"] - 0.5) < 1e-9, e
        assert "run_crashed" not in open(events, encoding="utf-8").read()
    finally:
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_watch_run_failed_finish_records_failed():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_watch3_")
    try:
        serve._configure(tmp, "default", 8787)
        serve._runs_add("r1", "partial")
        events = os.path.join(serve.RUNS_DIR, "r1", "events.jsonl")
        os.makedirs(os.path.dirname(events), exist_ok=True)
        with open(events, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "run_finished", "total_cost_usd": 0.1,
                                "statuses": {"t1": "done", "t2": "failed"}}) + "\n")
        serve.EVENTS_PATH = events
        serve._watch_run("r1", _FakeProc(1), events, {"cancel_requested": False})   # nonzero exit but finished
        serve._runs_load()
        assert serve._runs[0]["status"] == "failed", serve._runs
        assert "run_crashed" not in open(events, encoding="utf-8").read()
    finally:
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_spawn_failure_records_crash_event():
    """Brief 1.1: a spawn failure (bad interpreter etc.) must be VISIBLE --
    a run_crashed event lands in the stream instead of a silent dead run."""
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_spawnfail_")

    class Boom:
        def __init__(self, *a, **kw):
            raise OSError("interpreter vanished")

    serve._configure(tmp, "default", 8787)   # before patching Popen (git probe)
    from test_gate_serve import authorize_local_agents
    authorize_local_agents()  # This test targets spawning after user consent.
    orig_popen = serve.subprocess.Popen
    serve.subprocess.Popen = Boom
    try:
        try:
            serve._spawn_main(["--goal=x"])
            raise AssertionError("spawn failure must propagate")
        except OSError:
            pass
        text = open(serve.EVENTS_PATH, encoding="utf-8").read()
        assert '"type": "run_crashed"' in text, text
        assert "spawn failed" in text, text
        serve._runs_load()
        assert serve._runs[-1]["status"] == "crashed"
    finally:
        serve.subprocess.Popen = orig_popen
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_preflight_endpoint_busy_returns_409():
    orig_current = serve._current

    class Alive:
        def poll(self):
            return None

    serve._current = Alive()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, data = _post(port, "/settings/preflight", {})
        assert code == 409, f"got {code} {data}"
    finally:
        srv.shutdown()
        serve._current = orig_current


if __name__ == "__main__":
    test_probe_agents_zero_subprocess()
    test_settings_endpoints_desktop_roundtrip()
    test_watch_run_crash_without_run_finished()
    test_watch_run_normal_finish_records_done_and_cost()
    test_watch_run_failed_finish_records_failed()
    test_spawn_failure_records_crash_event()
    test_preflight_endpoint_busy_returns_409()
    print("\nAll settings/watchdog tests passed.")
