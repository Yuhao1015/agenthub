"""Tool permissions writable (CLAUDE.md §6.7 P2 item 8): POST /settings/tools
is TIGHTEN-ONLY -- the UI may only remove tools from a claude_code agent's
current effective set. Writes go through the existing matrix_patch channel
(config.yaml -> _write_merged_matrix -> merged_matrix.yaml for the engine
subprocess), never a new config path.

Key rules under test: widening rejected; empty list clamps to ["Read"]
(empty string empirically does NOT restrict tools); dropping Read while
keeping other tools rejected; non-claude_code agents rejected; other
patch fields for the same agent survive untouched.

No framework, plain asserts. Zero real LLM calls.

Run: python3 tests/test_tools_permissions.py
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


def _start(tmp):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    serve._configure(tmp, "default", port)
    return srv, port


def _merged():
    return yaml.safe_load(open(serve.MERGED_MATRIX_PATH, encoding="utf-8"))


def test_effective_allowed_tools_reads_merged():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_tools_read_")
    try:
        serve._configure(tmp, "default", 8787)
        got = serve._effective_allowed_tools("cc-sonnet")
        assert got == (["Read", "Edit", "Write", "Bash"], False), got
        assert serve._effective_allowed_tools("cc-opus") == (["Read"], False)
        assert serve._effective_allowed_tools("cc-sonnet-mgr") == (["Read"], False)
        # a codebuddy agent (no allowed_tools field) -> None
        assert serve._effective_allowed_tools("does-not-exist") is None
        # after a tightening save, the merged set narrows and patched=True
        serve._save_config({"matrix_patch": {"agents": {
            "cc-sonnet": {"allowed_tools": "Read,Edit"}}}})
        serve._write_merged_matrix()
        assert serve._effective_allowed_tools("cc-sonnet") == (["Read", "Edit"], True)
    finally:
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_save_tightens_only():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_tools_tight_")
    try:
        srv, port = _start(tmp)
        code, data = _post(port, "/settings/tools",
                           {"agent": "cc-sonnet", "allowed": ["Read", "Edit"]})
        assert code == 200 and data["ok"] is True, (code, data)
        cfg = serve._load_config()
        # stored as the comma-space joined string; executors split+strip it
        assert cfg["matrix_patch"]["agents"]["cc-sonnet"]["allowed_tools"] == "Read, Edit"
        assert _merged()["agents"]["cc-sonnet"]["allowed_tools"] == "Read, Edit"
        code, data = _get(port, "/settings/tools")
        sonnet = [a for a in data["agents"] if a["name"] == "cc-sonnet"][0]
        assert sonnet["allowed"] == ["Read", "Edit"] and sonnet["patched"] is True
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_save_rejects_widening_and_bad_shapes():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_tools_wide_")
    try:
        srv, port = _start(tmp)
        # widening: a tool not in the base set
        code, data = _post(port, "/settings/tools",
                           {"agent": "cc-sonnet", "allowed": ["Read", "GodMode"]})
        assert code == 400, (code, data)
        assert "GodMode" in data["error"]
        # not a list
        code, data = _post(port, "/settings/tools",
                           {"agent": "cc-sonnet", "allowed": "Read"})
        assert code == 400
        # unknown / non-claude_code agent
        for agent in ("does-not-exist", "cc-glm-strong"):
            code, data = _post(port, "/settings/tools",
                               {"agent": agent, "allowed": ["Read"]})
            assert code == 400, (agent, code)
        # nothing was persisted by any rejected request
        assert not os.path.exists(serve.CONFIG_PATH) or \
            not (serve._load_config().get("matrix_patch") or {}).get("agents")
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_save_clamps_empty_to_read_and_rejects_dropping_read():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_tools_clamp_")
    try:
        srv, port = _start(tmp)
        code, data = _post(port, "/settings/tools",
                           {"agent": "cc-sonnet", "allowed": []})
        assert code == 200 and data["clamped"] is True, (code, data)
        assert serve._load_config()["matrix_patch"]["agents"]["cc-sonnet"]["allowed_tools"] == "Read"
        # dropping Read while keeping other tools is rejected
        code, data = _post(port, "/settings/tools",
                           {"agent": "cc-sonnet", "allowed": ["Edit"]})
        assert code == 400, (code, data)
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_removed_tool_can_be_restored():
    """Regression: tightening once must not make a tool unreachable
    forever. The ratchet's ceiling is the matrix's own base set, not
    whatever's currently effective -- otherwise the SECOND save (trying
    to re-check a tool the FIRST save removed) gets rejected as
    "widening", and the checkbox for it silently vanishes from the UI
    (base-driven rendering) with no way back short of hand-editing
    config.yaml."""
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_tools_restore_")
    try:
        srv, port = _start(tmp)
        base = serve._base_allowed_tools("cc-sonnet")
        assert "Bash" in base

        # tighten: drop Bash
        tightened = [t for t in base if t != "Bash"]
        code, data = _post(port, "/settings/tools",
                           {"agent": "cc-sonnet", "allowed": tightened})
        assert code == 200, (code, data)
        sonnet = [a for a in data["agents"] if a["name"] == "cc-sonnet"][0]
        assert "Bash" not in sonnet["allowed"]
        assert sonnet["base"] == base   # base always offers the full set

        # restore: re-check Bash -- must succeed, not be treated as widening
        code, data = _post(port, "/settings/tools",
                           {"agent": "cc-sonnet", "allowed": base})
        assert code == 200, (code, data)
        sonnet = [a for a in data["agents"] if a["name"] == "cc-sonnet"][0]
        assert sonnet["allowed"] == base, sonnet
        # fully restored to baseline -> the override is dropped, not kept
        # around as a redundant no-op patch entry
        assert "cc-sonnet" not in (serve._load_config().get("matrix_patch") or {}).get("agents", {})
        assert sonnet["patched"] is False
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_legacy_mode_without_data_dir_returns_400_not_crash():
    """Regression: POST /settings/tools must guard on CONFIG_PATH like
    every other settings-writer does (_handle_settings_config already
    does) -- without it, _save_config's _atomic_write raises on an empty
    path and the connection dies with no HTTP response at all."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert serve.CONFIG_PATH == "", \
            "this test assumes no _configure() call happened yet in this process"
        code, data = _post(port, "/settings/tools",
                           {"agent": "cc-sonnet", "allowed": ["Read"]})
        assert code == 400, (code, data)
        assert "error" in data
    finally:
        srv.shutdown()


def test_save_preserves_other_patch_fields():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_tools_preserve_")
    try:
        srv, port = _start(tmp)
        serve._save_config({"matrix_patch": {"agents": {
            "cc-sonnet": {"extra_field": 1}}}})
        code, data = _post(port, "/settings/tools",
                           {"agent": "cc-sonnet", "allowed": ["Read"]})
        assert code == 200
        entry = serve._load_config()["matrix_patch"]["agents"]["cc-sonnet"]
        assert entry == {"extra_field": 1, "allowed_tools": "Read"}, entry
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll tools-permissions tests passed.")
