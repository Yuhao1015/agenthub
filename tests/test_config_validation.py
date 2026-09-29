"""Config-input validation + handler robustness (2026-08-23 复核轮).

Every test here defends one property: a bad request must produce an HTTP
ERROR RESPONSE, never an unhandled exception. ThreadingHTTPServer turns an
unhandled exception in a handler into a dropped connection with no reply
at all -- the frontend can't tell that apart from "serve.py is gone", so
it shows "连不上 serve.py" and the user is told to restart a server that
is in fact running fine.

The worst case found was persistent: POST /settings/config accepted any
type for matrix_patch, _deep_merge REPLACES wholesale on a non-dict, and
the result is written to merged_matrix.yaml -- which is the file handed to
the engine subprocess via --matrix. So one malformed request destroyed the
capability matrix for every future run AND bricked the settings page, and
it survived restarts because it lived in config.yaml.

No framework, plain asserts. Zero real LLM calls.

Run: python3 tests/test_config_validation.py
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
import yaml

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


def _req(port, method, path, body=None):
    """Returns (status, data) -- or ("DROPPED", err) when the server closed
    the connection without replying, which is exactly the failure mode
    these tests exist to prevent."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    payload = json.dumps(body) if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, payload, headers)
    try:
        r = conn.getresponse()
        data = json.loads(r.read() or b"{}")
        return r.status, data
    except Exception as e:
        return "DROPPED", f"{type(e).__name__}: {e}"
    finally:
        conn.close()


def _start(tmp):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    serve._configure(tmp, "default", port)
    return srv, port


def test_matrix_patch_must_be_an_object():
    """The headline regression: a scalar matrix_patch used to be accepted,
    overwrite the whole merged matrix, and permanently break both the
    settings page and every future run."""
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_cfgval_mp_")
    try:
        srv, port = _start(tmp)
        assert _req(port, "GET", "/settings")[0] == 200

        for bad in ("notadict", 123, True, ["a"]):
            code, data = _req(port, "POST", "/settings/config", {"matrix_patch": bad})
            assert code == 400, f"matrix_patch={bad!r} should be rejected, got {code} {data}"

        # nothing was persisted, and the matrix is still a real matrix
        assert not serve._matrix_patch(), serve._load_config()
        merged = yaml.safe_load(open(serve.MERGED_MATRIX_PATH, encoding="utf-8"))
        assert isinstance(merged, dict) and merged.get("agents"), merged

        # the endpoints that used to die still answer
        for path in ("/settings", "/settings/tools"):
            code, _ = _req(port, "GET", path)
            assert code == 200, f"GET {path} -> {code}"

        # None is the documented "clear it" value and stays accepted
        code, _ = _req(port, "POST", "/settings/config", {"matrix_patch": None})
        assert code == 200
        assert serve._matrix_patch() == {}
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_already_corrupted_config_does_not_brick_the_page():
    """Self-heal path: installs corrupted BEFORE this fix (or by a hand
    edit) must degrade to defaults, not keep dropping connections."""
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_cfgval_heal_")
    try:
        srv, port = _start(tmp)
        # write the damage directly, bypassing the now-validating endpoint
        serve._save_config({"matrix_patch": "notadict"})
        serve._write_merged_matrix()

        assert serve._matrix_patch() == {}, "a non-dict patch reads as empty"
        merged = yaml.safe_load(open(serve.MERGED_MATRIX_PATH, encoding="utf-8"))
        assert isinstance(merged, dict), f"corrupt patch must not reach the engine: {merged!r}"

        for path in ("/settings", "/settings/tools", "/settings/agents"):
            code, _ = _req(port, "GET", path)
            assert code == 200, f"GET {path} -> {code}"
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_non_dict_matrix_yaml_degrades_instead_of_raising():
    """A structurally wrong matrix file (bare scalar) must read as "no
    matrix", not raise AttributeError inside a handler."""
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_cfgval_yaml_")
    try:
        srv, port = _start(tmp)
        with open(serve.MERGED_MATRIX_PATH, "w", encoding="utf-8") as f:
            f.write("justastring\n")
        serve.MATRIX = serve.MERGED_MATRIX_PATH

        assert serve._load_yaml_dict(serve.MATRIX) == {}
        assert serve._effective_allowed_tools("cc-sonnet") is None
        code, data = _req(port, "GET", "/settings")
        assert code == 200, (code, data)
        assert data["budget"] == 2.0, "falls back to the default budget"
        code, data = _req(port, "GET", "/settings/tools")
        assert code == 200, (code, data)
        # base still comes from the repo matrix, so the UI stays usable
        assert any(a["name"] == "cc-sonnet" for a in data["agents"]), data
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_budget_rejects_bool_and_nonfinite():
    """isinstance(True, int) is True in Python -- {"budget": true} used to
    sail through and silently set the guard rail to $1.00."""
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_cfgval_budget_")
    try:
        srv, port = _start(tmp)
        for bad in (True, False, "3.5", None, [1], float("nan"),
                    float("inf"), -1):
            code, data = _req(port, "POST", "/settings/config", {"budget": bad})
            assert code == 400, f"budget={bad!r} should be rejected, got {code} {data}"
        # config.yaml untouched by every rejected attempt
        assert not (serve._load_config().get("budget") or {}), serve._load_config()

        # real numbers still work, including 0 and ints
        for good in (0, 3, 7.5):
            code, data = _req(port, "POST", "/settings/config", {"budget": good})
            assert code == 200 and abs(data["budget"] - float(good)) < 1e-9, (good, data)
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_handler_has_socket_timeout():
    """A connection that never sends a request line used to park a handler
    thread forever (measured: 300 idle connections -> 302 live threads).
    Browsers open such sockets routinely (preconnect), so over a long
    session threads grew without bound."""
    assert isinstance(serve.Handler.timeout, (int, float)), \
        "Handler.timeout must be set; None means idle sockets hold threads forever"
    assert 0 < serve.Handler.timeout <= 300, serve.Handler.timeout


def test_idle_connection_thread_is_released():
    """End-to-end proof of the timeout: open a silent connection, and the
    handler thread must go away on its own without the client doing
    anything. Uses a deliberately tiny timeout so the test is fast."""
    import socket
    import time
    s = _snap()
    orig_timeout = serve.Handler.timeout
    serve.Handler.timeout = 1
    tmp = tempfile.mkdtemp(prefix="ah_cfgval_idle_")
    try:
        srv, port = _start(tmp)
        before = threading.active_count()
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            # give the server a moment to accept and spawn the handler
            deadline = time.time() + 3
            while threading.active_count() <= before and time.time() < deadline:
                time.sleep(0.05)
            assert threading.active_count() > before, "handler thread never started"

            # ...and now it must retire on its own, client still connected
            deadline = time.time() + 8
            while threading.active_count() > before and time.time() < deadline:
                time.sleep(0.1)
            assert threading.active_count() <= before, \
                "idle handler thread outlived Handler.timeout"
        finally:
            sock.close()
    finally:
        srv.shutdown()
        serve.Handler.timeout = orig_timeout
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_pick_workdir_subprocess_has_a_timeout():
    """The macOS folder chooser blocks until the user acts; a dialog that
    opened behind another window would otherwise hang the request thread
    forever. Asserts the timeout is actually passed to subprocess.run."""
    if sys.platform != "darwin":
        print("== test_pick_workdir_subprocess_has_a_timeout == (skipped: not macOS)")
        return
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_cfgval_pick_")
    captured = {}
    orig_run = serve.subprocess.run

    def fake_run(argv, **kw):
        captured["argv"], captured["kw"] = argv, kw
        raise serve.subprocess.TimeoutExpired(argv, kw.get("timeout"))

    try:
        srv, port = _start(tmp)
        serve.subprocess.run = fake_run
        code, data = _req(port, "POST", "/settings/pick-workdir", {})
        assert code == 200 and data.get("cancelled") is True, (code, data)
        assert captured["argv"][0] == "osascript", captured["argv"]
        t = captured["kw"].get("timeout")
        assert isinstance(t, (int, float)) and t > 0, f"no timeout passed: {captured['kw']}"
    finally:
        serve.subprocess.run = orig_run
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_fresh_project_does_not_serve_files_from_cwd():
    """A project with no runs yet must not fall back to the module-level
    RELATIVE defaults ("events.jsonl"/"blackboard.jsonl"), which resolve
    against whatever cwd the launcher started in. The repo root ships both
    files from old dev runs, so the bug served a stale foreign run as if it
    were this project's -- and /rerun's os.path.exists(BOARD_PATH) check
    passed, resuming against that foreign blackboard."""
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_cfgval_cwd_")
    cwd = tempfile.mkdtemp(prefix="ah_cfgval_cwd_fake_")
    orig_cwd = os.getcwd()
    try:
        # a cwd that DOES contain the tempting relative-path files
        for name in ("events.jsonl", "blackboard.jsonl"):
            with open(os.path.join(cwd, name), "w", encoding="utf-8") as f:
                f.write(json.dumps({"key": "goal/statement", "value": "FOREIGN"}) + "\n")
        os.chdir(cwd)

        srv, port = _start(tmp)
        for path in ("/events.jsonl", "/blackboard.jsonl"):
            code, data = _req(port, "GET", path)
            assert code == 404, f"{path} leaked cwd data: {code} {str(data)[:120]}"
            assert isinstance(data, dict) and "error" in data, data   # JSON, not an HTML page

        assert os.path.realpath(serve.BOARD_PATH).startswith(os.path.realpath(serve.RUNS_DIR)), \
            serve.BOARD_PATH
        assert os.path.realpath(serve.EVENTS_PATH).startswith(os.path.realpath(serve.RUNS_DIR)), \
            serve.EVENTS_PATH

        # and the dangerous consequence is gone too
        code, data = _req(port, "POST", "/rerun", {})
        assert code == 400 and "blackboard" in data.get("error", ""), (code, data)
    finally:
        srv.shutdown()
        os.chdir(orig_cwd)
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(cwd, ignore_errors=True)


def test_api_404s_are_json_not_html():
    """Every API reply is JSON; a frontend doing res.json() on the error
    path shouldn't hit a parse error instead of a readable reason."""
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_cfgval_404_")
    try:
        srv, port = _start(tmp)
        code, data = _req(port, "POST", "/definitely-not-an-endpoint", {})
        assert code == 404 and isinstance(data, dict), (code, data)
        assert "unknown endpoint" in data.get("error", ""), data
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll config-validation tests passed.")
