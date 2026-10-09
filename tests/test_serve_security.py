"""CSRF hardening (brief 1.5) + /shutdown (brief 1.6) + /health (brief 4.4):
every POST must carry a JSON Content-Type and a local Origin; shutdown must
never deadlock the request thread and must refuse while a run is busy.

No framework, plain asserts. Zero real LLM calls.

Run: python3 tests/test_serve_security.py
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve


def _raw_post(port, path, obj, headers):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path, json.dumps(obj), headers)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    try:
        return r.status, json.loads(data or b"{}")
    except json.JSONDecodeError:
        return r.status, {}


def _start_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def test_post_rejects_non_json_content_type():
    orig_port = serve.PORT
    srv, port = _start_server()
    try:
        serve.PORT = port
        code, r = _raw_post(port, "/run", {"goal": "x"},
                            {"Content-Type": "text/plain"})
        assert code == 400 and "json" in (r.get("error") or ""), f"got {code} {r}"
    finally:
        srv.shutdown()
        serve.PORT = orig_port


def test_post_rejects_cross_origin():
    orig_port = serve.PORT
    srv, port = _start_server()
    try:
        serve.PORT = port
        code, r = _raw_post(port, "/rerun", {},
                            {"Content-Type": "application/json",
                             "Origin": "https://evil.example"})
        assert code == 403 and "cross-origin" in (r.get("error") or ""), f"got {code} {r}"
    finally:
        srv.shutdown()
        serve.PORT = orig_port


def test_post_allows_local_origin():
    orig_port = serve.PORT
    srv, port = _start_server()
    try:
        serve.PORT = port
        # no goal -> 400 from the handler itself, NOT a 403 from the CSRF gate
        code, r = _raw_post(port, "/run", {},
                            {"Content-Type": "application/json",
                             "Origin": f"http://127.0.0.1:{port}"})
        assert code == 400 and "goal" in (r.get("error") or ""), f"got {code} {r}"
    finally:
        srv.shutdown()
        serve.PORT = orig_port


def test_post_without_origin_still_works():
    """curl / urllib-style clients send no Origin header -- they must not be
    blocked (the JSON Content-Type requirement is what stops browsers)."""
    orig_port = serve.PORT
    srv, port = _start_server()
    try:
        serve.PORT = port
        code, r = _raw_post(port, "/run", {}, {"Content-Type": "application/json"})
        assert code == 400 and "goal" in (r.get("error") or ""), f"got {code} {r}"
    finally:
        srv.shutdown()
        serve.PORT = orig_port


class _FakeServer:
    def __init__(self):
        self.calls = 0

    def shutdown(self):
        self.calls += 1


class _Alive:
    def poll(self):
        return None


def test_shutdown_busy_returns_409_without_shutdown():
    orig_current, orig_server = serve._current, serve.SERVER
    fake = _FakeServer()
    serve._current = _Alive()
    serve.SERVER = fake
    srv, port = _start_server()
    try:
        code, r = _raw_post(port, "/shutdown", {}, {"Content-Type": "application/json"})
        assert code == 409, f"got {code} {r}"
        assert fake.calls == 0, "must not shut down while a run is in progress"
    finally:
        srv.shutdown()
        serve._current, serve.SERVER = orig_current, orig_server


def test_shutdown_idle_runs_shutdown_in_another_thread():
    orig_current, orig_server = serve._current, serve.SERVER
    fake = _FakeServer()
    serve._current = None
    serve.SERVER = fake
    srv, port = _start_server()
    try:
        code, r = _raw_post(port, "/shutdown", {}, {"Content-Type": "application/json"})
        assert code == 200, f"got {code} {r}"
        deadline = time.time() + 3
        while fake.calls == 0 and time.time() < deadline:
            time.sleep(0.02)
        assert fake.calls == 1, "shutdown must be triggered (from another thread)"
    finally:
        srv.shutdown()
        serve._current, serve.SERVER = orig_current, orig_server


def test_health_endpoint_shape():
    srv, port = _start_server()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/health")
        r = conn.getresponse()
        data = json.loads(r.read())
        conn.close()
        assert r.status == 200
        assert data["version"] == serve.VERSION
        assert "busy" in data and "data_dir" in data and "port" in data
    finally:
        srv.shutdown()


def _request(port, method, path, host=None, body=None, headers=None, want_location=False):
    """Like the helpers above, but can put an arbitrary Host header on the
    wire (http.client otherwise always sends 127.0.0.1:<port>)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest(method, path, skip_host=host is not None)
    if host is not None:
        conn.putheader("Host", host)
    for k, v in (headers or {}).items():
        conn.putheader(k, v)
    if body is not None:
        conn.putheader("Content-Length", str(len(body)))
    conn.endheaders(body)
    r = conn.getresponse()
    r.read()
    conn.close()
    return r.status if not want_location else (r.status, r.getheader("Location"))


def test_only_the_two_pages_are_served_statically():
    """B9 (2026-09-25): the static fallback served the whole working
    directory (the launcher cd's to the repo root): source, .git/,
    .claude/settings.local.json, any untracked file lying in the root."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cwd = os.getcwd()
    os.chdir(root)                     # the static handler serves the cwd
    srv, port = _start_server()
    try:
        for page in ("/dashboard.html", "/settings.html", "/dashboard.html?v=2"):
            assert _request(port, "GET", page) == 200, page
        for leak in ("/serve.py", "/CLAUDE.md", "/.git/HEAD", "/hub/",
                     "/dashboard.html/../serve.py"):
            status = _request(port, "GET", leak)
            assert status == 404, (leak, status)
        assert _request(port, "HEAD", "/serve.py") == 404
        # the bare origin used to be a directory listing; the preview pane
        # and anyone typing localhost:8787 land there, so send them on
        status, location = _request(port, "GET", "/", want_location=True)
        assert status == 302 and location == "/dashboard.html", (status, location)
    finally:
        srv.shutdown()
        os.chdir(cwd)
    print("PASS: test_only_the_two_pages_are_served_statically")


def test_foreign_host_is_rejected():
    """B9: DNS rebinding. A page on evil.example whose name re-resolves to
    127.0.0.1 is same-origin with this server as far as the browser can
    tell, so it could read /settings, /events.jsonl, /file, /export...
    (POST had an Origin check; GET checked nothing.) Its requests still
    say Host: evil.example:<port> -- only our own names get through."""
    srv, port = _start_server()
    try:
        for host in (f"127.0.0.1:{port}", f"localhost:{port}", f"LOCALHOST:{port}"):
            assert _request(port, "GET", "/health", host=host) == 200, host
        for host in (f"evil.example:{port}", f"127.0.0.1:{port + 1}", "127.0.0.1", ""):
            for path in ("/health", "/settings", "/events.jsonl", "/dashboard.html"):
                status = _request(port, "GET", path, host=host)
                assert status == 403, (host, path, status)
        status = _request(port, "POST", "/run", host=f"evil.example:{port}", body=b"{}",
                          headers={"Content-Type": "application/json"})
        assert status == 403, status
    finally:
        srv.shutdown()
    print("PASS: test_foreign_host_is_rejected")


def test_run_mock_flag_forwarded():
    """4.6's zero-cost demo button rides POST /run {"mock": true} -- the
    flag must survive to the spawned argv."""
    orig_current, orig_popen = serve._current, serve.subprocess.Popen
    orig_runs_dir = serve.RUNS_DIR
    captured = {}

    class P:
        def __init__(self, argv, **kw):
            captured["argv"] = argv
            self.pid = 1
            self.returncode = 0

        def poll(self):
            return None

        def wait(self):
            return 0

    serve.subprocess.Popen = P
    serve._current = None
    serve.RUNS_DIR = ""
    srv, port = _start_server()
    try:
        code, r = _raw_post(port, "/run", {"goal": "demo", "mock": True},
                            {"Content-Type": "application/json"})
        assert code == 200, f"got {code} {r}"
        assert "--mock" in captured["argv"] and "--goal=demo" in captured["argv"]
    finally:
        srv.shutdown()
        serve.subprocess.Popen = orig_popen
        serve._current = orig_current
        serve.RUNS_DIR = orig_runs_dir


if __name__ == "__main__":
    test_post_rejects_non_json_content_type()
    test_post_rejects_cross_origin()
    test_post_allows_local_origin()
    test_post_without_origin_still_works()
    test_shutdown_busy_returns_409_without_shutdown()
    test_shutdown_idle_runs_shutdown_in_another_thread()
    test_health_endpoint_shape()
    test_only_the_two_pages_are_served_statically()
    test_foreign_host_is_rejected()
    test_run_mock_flag_forwarded()
    print("\nAll serve security/shutdown tests passed.")
