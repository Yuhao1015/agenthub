"""Found by the 2026-09-25 browser check: the static handler sent Last-Modified
but no Cache-Control, so browsers heuristically reused a cached
dashboard.html -- after an update the user can keep running the OLD
frontend against the NEW backend for hours (the check itself ran stale code).

Static pages must be revalidated every time (Cache-Control: no-cache; an
unchanged file still costs only a 304).

Run: python tests/test_static_no_cache.py
"""
from __future__ import annotations

import http.client
import os
import sys
import threading
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import serve


def test_static_pages_are_revalidated():
    cwd = os.getcwd()
    os.chdir(ROOT)                  # the static handler serves the cwd
    server = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for page in ("/dashboard.html", "/settings.html", "/assets/studio.css",
                     "/assets/studio.js", "/assets/vendor/lucide.min.js"):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", page)
            response = conn.getresponse()
            response.read()
            conn.close()
            assert response.status == 200, (page, response.status)
            cache = response.getheader("Cache-Control") or ""
            assert "no-cache" in cache, f"{page}: Cache-Control={cache!r}"
        for blocked in ("/assets/", "/assets/vendor/", "/assets/vendor/LUCIDE-LICENSE.txt",
                        "/assets/../serve.py", "/assets/%2e%2e/serve.py", "/serve.py", "/.git/config"):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", blocked)
            response = conn.getresponse()
            response.read()
            conn.close()
            assert response.status == 404, (blocked, response.status)
    finally:
        server.shutdown()
        os.chdir(cwd)
    print("PASS: test_static_pages_are_revalidated")


if __name__ == "__main__":
    test_static_pages_are_revalidated()
