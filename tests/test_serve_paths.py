"""serve.py desktop plumbing: absolute main.py path, board/events/workdir/
matrix forwarding, per-run directories, rerun reuse, merged matrix deep
merge, config/credentials persistence, data-dir resolution, run index.

No framework, plain asserts; zero real LLM calls (Popen is faked).

Run: python3 tests/test_serve_paths.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

import serve
from test_gate_serve import authorize_local_agents

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


class _FakeProc:
    def __init__(self, argv, **kw):
        self.argv = argv
        self.pid = 5150
        self.returncode = 0

    def poll(self):
        return None

    def wait(self):
        return 0


def test_legacy_spawn_argv_absolute_and_no_extra_args():
    s = _snap()
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

    authorize_local_agents()
    orig_popen = serve.subprocess.Popen
    serve.subprocess.Popen = P
    try:
        serve.RUNS_DIR = ""
        serve._spawn_main(["--mock", "--goal=x"])
    finally:
        serve.subprocess.Popen = orig_popen
        _restore(s)
    argv = captured["argv"]
    assert argv[0] == sys.executable
    assert os.path.isabs(argv[1]) and argv[1].endswith("main.py"), argv
    assert argv[2:] == ["--mock", "--goal=x"], argv   # legacy: no path args


def test_desktop_spawn_creates_run_dir_and_forwards_paths():
    """Brief 1.2 + 4.2: a fresh run gets its own directory, and the engine
    subprocess receives explicit --board/--events/--workdir/--matrix."""
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_paths_")
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

    serve._configure(tmp, "default", 8787)   # BEFORE patching Popen:
                                             # _configure runs git via subprocess.run
    authorize_local_agents()
    orig_popen = serve.subprocess.Popen
    serve.subprocess.Popen = P
    try:
        serve._spawn_main(["--goal=hello world"])
        argv = captured["argv"]
        board, events = serve.BOARD_PATH, serve.EVENTS_PATH
        workdir, matrix = serve.WORKDIR, serve.MATRIX
        run_id = serve._run_meta["id"]
        runs = list(serve._runs)
        matrix_exists = os.path.isfile(matrix)   # captured before the tmp rmtree
    finally:
        serve.subprocess.Popen = orig_popen
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)
    assert os.path.isabs(argv[1]) and argv[1].endswith("main.py"), argv
    assert f"--board={board}" in argv, argv
    assert f"--events={events}" in argv, argv
    assert f"--workdir={workdir}" in argv, argv
    assert f"--matrix={matrix}" in argv, argv
    assert board.startswith(os.path.join(tmp, "default", "runs", run_id)), board
    assert events.endswith(os.path.join(run_id, "events.jsonl")), events
    assert matrix == os.path.join(tmp, "default", "merged_matrix.yaml"), matrix
    assert matrix_exists, "merged matrix must exist on disk"
    assert len(runs) == 1
    assert runs[0]["run_id"] == run_id and runs[0]["goal"] == "hello world"
    # (status is racy here -- the watchdog thread may already have flipped
    # it to "done"; its transitions are asserted deterministically in
    # tests/test_settings_and_watchdog.py)


def test_desktop_rerun_reuses_latest_run_dir():
    """A rerun is the same run, second attempt: same board/events files
    (so --resume sees the tasks and the frontend byte offset continues)."""
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_rerun_")
    captured = []

    class P:
        def __init__(self, argv, **kw):
            captured.append(argv)
            self.pid = 1
            self.returncode = 0

        def poll(self):
            return None

        def wait(self):
            return 0

    serve._configure(tmp, "default", 8787)
    authorize_local_agents()
    orig_popen = serve.subprocess.Popen
    serve.subprocess.Popen = P
    try:
        serve._spawn_main(["--goal=first"])
        first_board = serve.BOARD_PATH
        n_after_first = len(serve._runs)
        serve._spawn_main(["--resume"])
        second_board = serve.BOARD_PATH
        n_after_rerun = len(serve._runs)
    finally:
        serve.subprocess.Popen = orig_popen
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)

    assert first_board == second_board, "rerun must reuse the latest run's files"
    assert n_after_first == n_after_rerun == 1, "rerun adds no new index entry"
    assert "--resume" in captured[-1]
    assert f"--board={second_board}" in captured[-1]


def test_merged_matrix_deep_merges_user_patch():
    """Brief 1.4: user patch (budget + route lists + per-agent fields) is
    deep-merged into ONE derived yaml the subprocess reads via --matrix."""
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_mm_")
    try:
        serve._configure(tmp, "default", 8787)
        with open(serve.MATRIX_BASE, encoding="utf-8") as f:
            base = yaml.safe_load(f)
        with open(serve.MERGED_MATRIX_PATH, encoding="utf-8") as f:
            merged = yaml.safe_load(f)
        policy = merged.pop('local_agent_policy')
        assert policy['confirmed'] is False and policy['enabled_kinds'] == []
        from hub.api_connections import project_api_connections
        assert merged == project_api_connections(base, {}), "base local metadata; no preset API connections"

        cfg = serve._load_config()
        cfg["budget"] = {"max_usd_per_run": 5.5}
        cfg["matrix_patch"] = {
            "budget": {"max_usd_per_run": 5.5},
            "routes": {"coding": ["cc-sonnet"]},              # list replaced wholesale
            "agents": {"cc-opus": {"model": "sonnet"}},       # dict merged field-wise
        }
        serve._save_config(cfg)
        serve._write_merged_matrix()
        with open(serve.MERGED_MATRIX_PATH, encoding="utf-8") as f:
            merged2 = yaml.safe_load(f)
        assert merged2["budget"]["max_usd_per_run"] == 5.5
        assert merged2["routes"]["coding"] == ["cc-sonnet"]
        assert merged2["routes"]["planning"] == base["routes"]["planning"]
        assert merged2["agents"]["cc-opus"]["model"] == "sonnet"
        assert merged2["agents"]["cc-opus"]["kind"] == "claude_code"
        assert serve.MATRIX == serve.MERGED_MATRIX_PATH
    finally:
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


def test_credentials_write_syncs_env_and_0600():
    """Brief 1.8: writing a key must land in os.environ immediately (next
    spawn inherits it), and the file must be 0600."""
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_creds_")
    orig_env = {k: os.environ[k] for k in ("DEEPSEEK_API_KEY", "OPENROUTER_API_KEY")
                if k in os.environ}
    for k in ("DEEPSEEK_API_KEY", "OPENROUTER_API_KEY"):
        os.environ.pop(k, None)
    try:
        serve._configure(tmp, "default", 8787)
        serve._credentials_write({"DEEPSEEK_API_KEY": "sk-test",
                                  "OPENROUTER_API_KEY": ""})
        assert os.environ.get("DEEPSEEK_API_KEY") == "sk-test"
        assert "OPENROUTER_API_KEY" not in serve._credentials_read()
        mode = os.stat(serve.CREDENTIALS_PATH).st_mode & 0o777
        if os.name == 'nt':
            from hub.connection_store import verify_private
            verify_private(serve.CREDENTIALS_PATH)
        else:
            assert mode == 0o600, oct(mode)
    finally:
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)
        os.environ.pop("DEEPSEEK_API_KEY", None)
        for k, v in orig_env.items():
            os.environ[k] = v


def test_credentials_startup_env_wins():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_credsenv_")
    orig = os.environ.get("DEEPSEEK_API_KEY")
    try:
        serve._configure(tmp, "default", 8787)
        serve._credentials_write({"DEEPSEEK_API_KEY": "file-value"})
        os.environ["DEEPSEEK_API_KEY"] = "shell-value"
        serve._credentials_load()
        assert os.environ["DEEPSEEK_API_KEY"] == "shell-value", \
            "operator intent (env) must win over the stored file"
    finally:
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)
        if orig is None:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        else:
            os.environ["DEEPSEEK_API_KEY"] = orig


def test_configure_data_dir_precedence_and_layout():
    s = _snap()
    tmp1 = tempfile.mkdtemp(prefix="ah_dd1_")
    tmp2 = tempfile.mkdtemp(prefix="ah_dd2_")
    orig_env = os.environ.get("AGENT_HUB_HOME")
    try:
        serve._configure(tmp1, "proj", 8790)
        assert serve.DATA_DIR == tmp1 and serve.PROJECT == "proj"
        assert serve.PORT == 8790
        assert serve.RUNTIME_DIR == os.path.join(tmp1, "proj")
        assert serve.WORKDIR == os.path.join(tmp1, "proj", "workdir")
        assert os.path.isdir(serve.WORKDIR) and os.path.isdir(serve.RUNS_DIR)
        _restore(s)

        os.environ["AGENT_HUB_HOME"] = tmp2
        serve._configure(None, None, 8787)
        assert serve.DATA_DIR == tmp2, "explicit > env > platform default"
        assert serve.PROJECT == "default"
    finally:
        _restore(s)
        if orig_env is None:
            os.environ.pop("AGENT_HUB_HOME", None)
        else:
            os.environ["AGENT_HUB_HOME"] = orig_env
        shutil.rmtree(tmp1, ignore_errors=True)
        shutil.rmtree(tmp2, ignore_errors=True)


def test_default_data_dir_is_absolute():
    d = serve._default_data_dir()
    assert d and os.path.isabs(d), d


def test_run_index_bookkeeping_and_adopt_latest():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_idx_")
    try:
        serve._configure(tmp, "default", 8787)
        serve._runs_add("r1", "goal one")
        serve._runs_update("r1", status="done", cost_usd=0.42)
        serve._runs_load()   # reload from disk
        assert len(serve._runs) == 1
        e = serve._runs[0]
        assert e["run_id"] == "r1" and e["status"] == "done"
        assert abs(e["cost_usd"] - 0.42) < 1e-9
        serve._adopt_latest_run()
        assert serve.BOARD_PATH == os.path.join(serve.RUNS_DIR, "r1", "blackboard.jsonl")
        assert serve.EVENTS_PATH == os.path.join(serve.RUNS_DIR, "r1", "events.jsonl")
        assert serve._run_meta["id"] == "r1"
    finally:
        _restore(s)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_legacy_spawn_argv_absolute_and_no_extra_args()
    test_desktop_spawn_creates_run_dir_and_forwards_paths()
    test_desktop_rerun_reuses_latest_run_dir()
    test_merged_matrix_deep_merges_user_patch()
    test_credentials_write_syncs_env_and_0600()
    test_credentials_startup_env_wins()
    test_configure_data_dir_precedence_and_layout()
    test_default_data_dir_is_absolute()
    test_run_index_bookkeeping_and_adopt_latest()
    print("\nAll serve path tests passed.")
