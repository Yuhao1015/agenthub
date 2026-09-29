"""CodeBuddyExecutor tests: argv construction, JSON-array-tail parsing,
credit extraction, error classification. No framework -- plain assert,
exit 1 on failure. Zero real subprocess/LLM calls.

Run: python tests/test_codebuddy_executor.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.models import Task, TaskType


class _FakeProc:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr


def _patch_subprocess(stdout_obj, returncode: int = 0, stderr: bytes = b""):
    stdout_bytes = (json.dumps(stdout_obj).encode("utf-8")
                    if isinstance(stdout_obj, (list, dict)) else stdout_obj)
    async def fake_exec(*args, **kwargs):
        return _FakeProc(stdout_bytes, stderr, returncode)
    asyncio.create_subprocess_exec = fake_exec


# Shape matches a real probe response captured 2026-07-21 (see
# docs/research/2026-07-21-workbuddy-cli.md): a JSON array, the assistant
# message carries the real credit spend under providerData.rawUsage.credit,
# and the final element is the result object (mirrors claude's is_error/
# result/duration_api_ms shape, but total_cost_usd is always 0 here).
_SUCCESS_MESSAGES = [
    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
    {"type": "reasoning", "content": []},
    {
        "id": "m1", "type": "message", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": "ok"}],
        "providerData": {"model": "deepseek-v3-2-volc",
                         "rawUsage": {"credit": 0.09, "prompt_tokens": 100}},
    },
    {"type": "result", "subtype": "success", "is_error": False, "result": "ok",
     "total_cost_usd": 0, "duration_api_ms": 3305},
]


def test_argv_includes_model_and_disables_tools():
    original = asyncio.create_subprocess_exec
    captured = {}

    async def capturing_exec(*args, **kwargs):
        captured["args"] = args
        return _FakeProc(json.dumps(_SUCCESS_MESSAGES).encode())
    asyncio.create_subprocess_exec = capturing_exec
    try:
        from hub.executors import CodeBuddyExecutor

        task = Task(id="t1", title="x", type=TaskType.WRITING, prompt="hello")
        executor = CodeBuddyExecutor(node_path="/fake/node", script_path="/fake/codebuddy.js",
                                     model="deepseek-v3-2-volc", workdir=".", stream=False)
        asyncio.run(executor.run(task, ""))
    finally:
        asyncio.create_subprocess_exec = original

    args = captured["args"]
    assert args[0] == "/fake/node" and args[1] == "/fake/codebuddy.js"
    assert "--model" in args and args[args.index("--model") + 1] == "deepseek-v3-2-volc"
    assert "--tools" in args and args[args.index("--tools") + 1] == "", \
        "empty --tools value must be passed verbatim to disable all tools"
    assert "-p" in args and args[args.index("-p") + 1] == "hello"
    print("PASS: test_argv_includes_model_and_disables_tools")


# 2026-08-30 真实探针样本（hy3，prompt「回复 ok 两个字」）的 rawUsage
# 形状。credit 为 0 是真的 —— 限免模型不扣积分，这正是用量面板不能只靠
# credit 折算的原因（见 CLAUDE.md §6.12.1）。
_REAL_RAW_USAGE = {
    "prompt_tokens": 6263,
    "completion_tokens": 41,
    "total_tokens": 6304,
    "prompt_cache_hit_tokens": 2880,
    "prompt_cache_miss_tokens": 3383,
    "completion_thinking_tokens": 38,
    "credit": 0,
}


def test_usage_is_extracted_from_raw_usage():
    """ExecResult.usage 一直在 Executor 契约里（"raw token usage from the
    provider, if any"），但 codebuddy 从来没填过 —— 数据就躺在
    providerData.rawUsage 里，只被读走了 credit 一个字段。

    存**原始形状**，不做归一化：契约字段本身写的就是 raw，而且
    claude_code/openrouter 各存各的 provider 原始形状。三家统一归一化要
    等三份真实样本都在手上再做，现在归一化就是在猜（CLAUDE.md §6.6 的规矩）。
    """
    from hub.executors import CodeBuddyExecutor

    messages = json.loads(json.dumps(_SUCCESS_MESSAGES))
    messages[2]["providerData"]["rawUsage"] = dict(_REAL_RAW_USAGE)

    original = asyncio.create_subprocess_exec
    _patch_subprocess(messages)
    try:
        task = Task(id="t1", title="x", type=TaskType.WRITING, prompt="p")
        executor = CodeBuddyExecutor(node_path="/fake/node", script_path="/fake/cb.js",
                                     model="hy3", workdir=".", stream=False)
        result = asyncio.run(executor.run(task, ""))
    finally:
        asyncio.create_subprocess_exec = original

    assert result.ok is True
    assert result.usage == _REAL_RAW_USAGE, \
        f"usage 应原样带出 rawUsage，got {result.usage}"
    # 缓存命中拆分必须保留 —— 6263 个 prompt token 里 2880 是命中的，
    # 缓存通常按正常价一成计费，丢了这个字段折算就会显著高估
    assert result.usage["prompt_cache_hit_tokens"] == 2880
    # credit 为 0 不影响 usage 被提取：这正是限免模型的形状
    assert result.cost_credits == 0.0
    print("PASS: test_usage_is_extracted_from_raw_usage")


def test_usage_is_none_when_provider_gives_nothing():
    """没有 assistant message / 没有 rawUsage 时 usage 是 None，不是 {}
    —— None 表示「provider 没给」，{} 会被误读成「给了但都是零」。"""
    from hub.executors import CodeBuddyExecutor

    messages = json.loads(json.dumps(_SUCCESS_MESSAGES))
    del messages[2]["providerData"]["rawUsage"]

    original = asyncio.create_subprocess_exec
    _patch_subprocess(messages)
    try:
        task = Task(id="t1", title="x", type=TaskType.WRITING, prompt="p")
        executor = CodeBuddyExecutor(node_path="/fake/node", script_path="/fake/cb.js",
                                     model="hy3", workdir=".", stream=False)
        result = asyncio.run(executor.run(task, ""))
    finally:
        asyncio.create_subprocess_exec = original

    assert result.usage is None, f"provider 没给用量时应为 None，got {result.usage}"
    print("PASS: test_usage_is_none_when_provider_gives_nothing")


def test_success_parses_last_array_element_and_extracts_credit():
    from hub.executors import CodeBuddyExecutor

    original = asyncio.create_subprocess_exec
    _patch_subprocess(_SUCCESS_MESSAGES)
    try:
        task = Task(id="t1", title="x", type=TaskType.WRITING, prompt="p")
        executor = CodeBuddyExecutor(node_path="/fake/node", script_path="/fake/cb.js",
                                     model="deepseek-v3-2-volc", workdir=".", stream=False)
        result = asyncio.run(executor.run(task, ""))
    finally:
        asyncio.create_subprocess_exec = original

    assert result.ok is True
    assert result.output == "ok"
    assert result.cost_credits == 0.09, \
        f"expected credit extracted from assistant message, got {result.cost_credits}"
    print("PASS: test_success_parses_last_array_element_and_extracts_credit")


def test_missing_assistant_message_defaults_credit_to_zero():
    """No assistant message anywhere in the array -- credit extraction
    must default to 0.0, not crash, and the call is still a success if the
    result object itself says so."""
    from hub.executors import CodeBuddyExecutor

    original = asyncio.create_subprocess_exec
    messages = [
        {"type": "message", "role": "user", "content": []},
        {"type": "result", "subtype": "success", "is_error": False, "result": "fine",
         "total_cost_usd": 0},
    ]
    _patch_subprocess(messages)
    try:
        task = Task(id="t1", title="x", type=TaskType.WRITING, prompt="p")
        executor = CodeBuddyExecutor(node_path="/fake/node", script_path="/fake/cb.js",
                                     model="glm-5.2", workdir=".", stream=False)
        result = asyncio.run(executor.run(task, ""))
    finally:
        asyncio.create_subprocess_exec = original

    assert result.ok is True
    assert result.output == "fine"
    assert result.cost_credits == 0.0, "missing assistant message must default credit to 0.0, not raise"
    print("PASS: test_missing_assistant_message_defaults_credit_to_zero")


def test_is_error_result_classified_task_error_by_default():
    from hub.executors import CodeBuddyExecutor

    original = asyncio.create_subprocess_exec
    messages = [
        {"type": "result", "subtype": "error", "is_error": True,
         "result": "something went wrong mid-generation", "total_cost_usd": 0},
    ]
    _patch_subprocess(messages)
    try:
        task = Task(id="t1", title="x", type=TaskType.WRITING, prompt="p")
        executor = CodeBuddyExecutor(node_path="/fake/node", script_path="/fake/cb.js",
                                     model="glm-5.2", workdir=".", stream=False)
        result = asyncio.run(executor.run(task, ""))
    finally:
        asyncio.create_subprocess_exec = original

    assert result.ok is False
    assert result.error_kind == "task_error"
    print("PASS: test_is_error_result_classified_task_error_by_default")


def test_is_error_result_with_credit_marker_classified_unavailable():
    from hub.executors import CodeBuddyExecutor

    original = asyncio.create_subprocess_exec
    messages = [
        {"type": "result", "subtype": "error", "is_error": True,
         "result": "Request rejected: insufficient credit balance", "total_cost_usd": 0},
    ]
    _patch_subprocess(messages)
    try:
        task = Task(id="t1", title="x", type=TaskType.WRITING, prompt="p")
        executor = CodeBuddyExecutor(node_path="/fake/node", script_path="/fake/cb.js",
                                     model="glm-5.2", workdir=".", stream=False)
        result = asyncio.run(executor.run(task, ""))
    finally:
        asyncio.create_subprocess_exec = original

    assert result.ok is False
    assert result.error_kind == "unavailable", \
        f"'insufficient credit' must be classified unavailable, got {result.error_kind}"
    print("PASS: test_is_error_result_with_credit_marker_classified_unavailable")


def test_missing_binary_classified_unavailable():
    from hub.executors import CodeBuddyExecutor

    original = asyncio.create_subprocess_exec
    async def raise_not_found(*args, **kwargs):
        raise FileNotFoundError()
    asyncio.create_subprocess_exec = raise_not_found
    try:
        task = Task(id="t1", title="x", type=TaskType.WRITING, prompt="p")
        executor = CodeBuddyExecutor(node_path="/does/not/exist", script_path="/nope.js",
                                     model="glm-5.2", workdir=".", stream=False)
        result = asyncio.run(executor.run(task, ""))
    finally:
        asyncio.create_subprocess_exec = original

    assert result.ok is False
    assert result.error_kind == "unavailable"
    print("PASS: test_missing_binary_classified_unavailable")


def test_build_executor_creates_codebuddy_executor():
    from hub.executors import CodeBuddyExecutor, build_executor

    spec = {"kind": "codebuddy", "node_path": "/n", "script_path": "/s.js", "model": "kimi-k2.7"}
    executor = build_executor(spec, workdir=".")
    assert isinstance(executor, CodeBuddyExecutor)
    assert executor.node_path == "/n" and executor.script_path == "/s.js"
    assert executor.model == "kimi-k2.7"
    print("PASS: test_build_executor_creates_codebuddy_executor")


def test_build_executor_spec_without_paths_still_constructs():
    """capability_matrix.yaml no longer commits machine-specific
    node_path/script_path (BYO principle) -- a spec that omits them
    entirely must not KeyError; resolution happens lazily at .run() time."""
    from hub.executors import CodeBuddyExecutor, build_executor

    spec = {"kind": "codebuddy", "model": "kimi-k2.7"}
    executor = build_executor(spec, workdir=".")
    assert isinstance(executor, CodeBuddyExecutor)
    assert executor.node_path is None and executor.script_path is None
    print("PASS: test_build_executor_spec_without_paths_still_constructs")


def _clear_workbuddy_env():
    saved = {k: os.environ.pop(k, None) for k in
              ("WORKBUDDY_NODE_PATH", "WORKBUDDY_SCRIPT_PATH")}
    return saved


def _restore_env(saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_resolve_paths_prefers_explicit_over_env_and_autodetect():
    from hub.executors import _resolve_codebuddy_paths

    saved = _clear_workbuddy_env()
    os.environ["WORKBUDDY_NODE_PATH"] = "/env/node"
    os.environ["WORKBUDDY_SCRIPT_PATH"] = "/env/cb.js"
    try:
        node, script = _resolve_codebuddy_paths("/explicit/node", "/explicit/cb.js")
        assert (node, script) == ("/explicit/node", "/explicit/cb.js")
    finally:
        _restore_env(saved)
    print("PASS: test_resolve_paths_prefers_explicit_over_env_and_autodetect")


def test_resolve_paths_uses_env_vars_when_no_explicit_value():
    from hub.executors import _resolve_codebuddy_paths

    saved = _clear_workbuddy_env()
    os.environ["WORKBUDDY_NODE_PATH"] = "/env/node"
    os.environ["WORKBUDDY_SCRIPT_PATH"] = "/env/cb.js"
    try:
        node, script = _resolve_codebuddy_paths(None, None)
        assert (node, script) == ("/env/node", "/env/cb.js")
    finally:
        _restore_env(saved)
    print("PASS: test_resolve_paths_uses_env_vars_when_no_explicit_value")


def test_resolve_paths_none_when_nothing_configured_non_macos():
    """Force a non-macOS platform so auto-detect (macOS-only, see the
    function's docstring) can't accidentally succeed just because THIS
    test happens to run on a real machine with WorkBuddy installed."""
    import hub.executors as executors_mod

    saved = _clear_workbuddy_env()
    original_platform = executors_mod.sys.platform
    executors_mod.sys.platform = "linux"
    try:
        node, script = executors_mod._resolve_codebuddy_paths(None, None)
        assert (node, script) == (None, None)
    finally:
        executors_mod.sys.platform = original_platform
        _restore_env(saved)
    print("PASS: test_resolve_paths_none_when_nothing_configured_non_macos")


def test_resolve_paths_macos_autodetect_picks_highest_version():
    """Simulate the default macOS WorkBuddy install layout without
    depending on whether THIS machine actually has one -- fakes
    os.path.exists (script) and glob.glob (versioned node binary)."""
    import hub.executors as executors_mod

    saved = _clear_workbuddy_env()
    original_platform = executors_mod.sys.platform
    original_exists = executors_mod.os.path.exists
    original_glob = executors_mod.glob.glob
    executors_mod.sys.platform = "darwin"
    executors_mod.os.path.exists = lambda p: p.endswith("codebuddy.js")
    executors_mod.glob.glob = lambda pattern: [
        "/fake/.workbuddy/binaries/node/versions/20.1.0/bin/node",
        "/fake/.workbuddy/binaries/node/versions/22.22.2/bin/node",
    ]
    try:
        node, script = executors_mod._resolve_codebuddy_paths(None, None)
        assert node == "/fake/.workbuddy/binaries/node/versions/22.22.2/bin/node", node
        assert script and script.endswith("codebuddy.js")
    finally:
        executors_mod.sys.platform = original_platform
        executors_mod.os.path.exists = original_exists
        executors_mod.glob.glob = original_glob
        _restore_env(saved)
    print("PASS: test_resolve_paths_macos_autodetect_picks_highest_version")


def test_unconfigured_paths_return_clean_unavailable_result():
    """No explicit paths, no env vars, non-macOS -- .run() must fail
    cleanly (error_kind=unavailable) instead of crashing on None being
    passed to asyncio.create_subprocess_exec."""
    import hub.executors as executors_mod
    from hub.executors import CodeBuddyExecutor

    saved = _clear_workbuddy_env()
    original_platform = executors_mod.sys.platform
    executors_mod.sys.platform = "linux"
    try:
        task = Task(id="t1", title="x", type=TaskType.WRITING, prompt="p")
        executor = CodeBuddyExecutor(node_path=None, script_path=None,
                                     model="glm-5.2", workdir=".", stream=False)
        result = asyncio.run(executor.run(task, ""))
    finally:
        executors_mod.sys.platform = original_platform
        _restore_env(saved)

    assert result.ok is False
    assert result.error_kind == "unavailable"
    assert "WORKBUDDY_NODE_PATH" in result.error
    print("PASS: test_unconfigured_paths_return_clean_unavailable_result")


if __name__ == "__main__":
    test_argv_includes_model_and_disables_tools()
    test_usage_is_extracted_from_raw_usage()
    test_usage_is_none_when_provider_gives_nothing()
    test_success_parses_last_array_element_and_extracts_credit()
    test_missing_assistant_message_defaults_credit_to_zero()
    test_is_error_result_classified_task_error_by_default()
    test_is_error_result_with_credit_marker_classified_unavailable()
    test_missing_binary_classified_unavailable()
    test_build_executor_creates_codebuddy_executor()
    test_build_executor_spec_without_paths_still_constructs()
    test_resolve_paths_prefers_explicit_over_env_and_autodetect()
    test_resolve_paths_uses_env_vars_when_no_explicit_value()
    test_resolve_paths_none_when_nothing_configured_non_macos()
    test_resolve_paths_macos_autodetect_picks_highest_version()
    test_unconfigured_paths_return_clean_unavailable_result()
    print("\nAll codebuddy executor tests passed.")
