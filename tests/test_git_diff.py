"""Git diff view (CLAUDE.md §6.7 P2 item 9): GET /file/diff?task=&path=
runs a live `git diff` (worktree vs HEAD) for one recorded deliverable.

Security core (the handoff's written requirements): the file must pass
GET /file's whitelist + realpath containment BEFORE any subprocess exists,
and the path must reach git as a literal argv element (never shell string
concatenation) -- the second line of defense even after the whitelist.

No framework, plain asserts. The end-to-end test uses the real `git`
binary when available and skips honestly otherwise.

Run: python3 tests/test_git_diff.py
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
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


def _start(tmp, workdir):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    serve._configure(tmp, "default", port)
    serve._runs_add("r1", "goal")
    serve._adopt_latest_run()
    run_dir = os.path.join(serve.RUNS_DIR, "r1")
    os.makedirs(run_dir, exist_ok=True)
    b = Blackboard(os.path.join(run_dir, "blackboard.jsonl"), on_write=lambda e: None)
    b.put("run/workdir", workdir, author="engine")
    b.put("tasks/t1/spec", {"title": "任务", "type": "coding"}, author="system", kind="spec")
    b.put("tasks/t1/files", [{"path": "a.txt", "size": 4}], author="engine", task_id="t1", kind="fact")
    return srv, port


def test_diff_requires_whitelist_before_git():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_diff_wl_")
    try:
        srv, port = _start(tmp, tmp)
        calls = []
        orig = serve.subprocess.run

        def boom(*a, **kw):
            calls.append(a)
            raise AssertionError("git must not be invoked before the whitelist")
        serve.subprocess.run = boom
        try:
            # ../evil is NOT recorded in tasks/t1/files -> 404, zero subprocesses
            code, data = _get(port, "/file/diff?task=t1&path=..%2Fevil")
            assert code == 404, (code, data)
            assert calls == [], "whitelist must reject before git runs"
        finally:
            serve.subprocess.run = orig
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_diff_git_invocation_is_argv_list():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_diff_argv_")
    workdir = os.path.join(tmp, "wd")
    os.makedirs(workdir)
    os.makedirs(os.path.join(workdir, ".git"))   # pretend it's a repo
    with open(os.path.join(workdir, "a.txt"), "w", encoding="utf-8") as f:
        f.write("x")
    try:
        srv, port = _start(tmp, workdir)
        captured = {}
        orig = serve.subprocess.run
        orig_which = serve.shutil.which

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return subprocess.CompletedProcess(argv, 0, stdout="+line\n", stderr="")
        serve.subprocess.run = fake_run
        serve.shutil.which = lambda name: "/usr/bin/git" if name == "git" else None
        try:
            code, data = _get(port, "/file/diff?task=t1&path=a.txt")
            assert code == 200 and data["ok"] is True, (code, data)
        finally:
            serve.subprocess.run = orig
            serve.shutil.which = orig_which
        argv = captured["argv"]
        # the endpoint realpaths the workdir (macOS: /var -> /private/var)
        assert argv == ["git", "-C", os.path.realpath(workdir), "--literal-pathspecs",
                        "diff", "--", "a.txt"], argv
        # literal argv elements, no shell: the path is ONE element, and
        # shell must not be requested (subprocess.run default is False)
        assert captured["kwargs"].get("shell") in (None, False), captured["kwargs"]
        assert argv[-1] == "a.txt" and argv[-2] == "--"
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_diff_workdir_not_a_repo_and_git_missing():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_diff_env_")
    workdir = os.path.join(tmp, "wd")
    os.makedirs(workdir)
    with open(os.path.join(workdir, "a.txt"), "w", encoding="utf-8") as f:
        f.write("x")
    try:
        srv, port = _start(tmp, workdir)
        code, data = _get(port, "/file/diff?task=t1&path=a.txt")
        assert code == 200 and data["ok"] is False, data
        assert "不是 git 仓库" in data["error"], data   # explicit, never silent
        # git missing: monkeypatch which
        orig = serve.shutil.which
        serve.shutil.which = lambda name: None
        try:
            code, data = _get(port, "/file/diff?task=t1&path=a.txt")
            assert data["error"] == "git not found", data
        finally:
            serve.shutil.which = orig
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_diff_end_to_end_real_git():
    if shutil.which("git") is None:
        print("== test_diff_end_to_end_real_git == (skipped: no git on PATH)")
        return
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_diff_e2e_")
    workdir = os.path.join(tmp, "wd")
    os.makedirs(workdir)
    with open(os.path.join(workdir, "a.txt"), "w", encoding="utf-8") as f:
        f.write("line one\nline two\n")
    subprocess.run(["git", "-C", workdir, "init", "-q"], check=True)
    subprocess.run(["git", "-C", workdir, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", workdir, "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", workdir, "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", workdir, "commit", "-qm", "init"], check=True)
    with open(os.path.join(workdir, "a.txt"), "w", encoding="utf-8") as f:
        f.write("line one changed\nline three added\n")
    try:
        srv, port = _start(tmp, workdir)
        code, data = _get(port, "/file/diff?task=t1&path=a.txt")
        assert code == 200 and data["ok"] is True, data
        assert "-line two" in data["diff"] or "-line one" in data["diff"], data["diff"]
        assert "+" in data["diff"]
        assert data["truncated"] is False
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_diff_truncates_oversized_output():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_diff_trunc_")
    workdir = os.path.join(tmp, "wd")
    os.makedirs(workdir)
    os.makedirs(os.path.join(workdir, ".git"))
    with open(os.path.join(workdir, "a.txt"), "w", encoding="utf-8") as f:
        f.write("x")
    try:
        srv, port = _start(tmp, workdir)
        orig = serve.subprocess.run
        serve.subprocess.run = lambda argv, **kw: subprocess.CompletedProcess(
            argv, 0, stdout="+" + "x" * (serve.MAX_DIFF_CHARS + 1000), stderr="")
        serve.shutil.which = lambda name: "/usr/bin/git"
        try:
            code, data = _get(port, "/file/diff?task=t1&path=a.txt")
        finally:
            serve.subprocess.run = orig
        assert code == 200 and data["truncated"] is True
        assert len(data["diff"]) <= serve.MAX_DIFF_CHARS
    finally:
        srv.shutdown()
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll git-diff tests passed.")
