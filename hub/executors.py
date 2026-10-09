"""Executors: the unified Agent contract.

Every executor implements `async run(task, context, on_event=None) -> ExecResult`.
The engine/router never care what's behind the interface — this is the
abstraction that later lets us add Coze / WorkBuddy as PlatformAgents.
"""
from __future__ import annotations

import asyncio
import contextlib
import glob
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import yaml
from dataclasses import dataclass
from typing import Callable, Optional

from openai import AsyncOpenAI

from .models import Task
from . import local_discovery


# GUI 启动的进程（双击的 .app / 启动器）从 launchd 继承的 PATH 只有
# /usr/bin:/bin:/usr/sbin:/sbin，而且**不读用户的 shell rc**。本项目驱动
# 的每个 CLI 都装在这之外，于是 shutil.which() 全部落空、启动预检把每个
# agent 都熔断 —— 症状是「终端里跑得好好的，双击图标就全挂」。
# 2026-08-31 实测确认：终端里 claude 在 /usr/local/bin，最小 PATH 里找不到。
#
# 这里**不**去起一个登录 shell 问它的 PATH（`$SHELL -lic 'echo $PATH'`）：
# 那个更准，但用户的 rc 里只要有一句交互式的东西就会挂住，而这条路径在
# 每次 Router 构造时都会走到。宁可用一张众所周知的目录表 + 一个让用户自己
# 补的环境变量（BYO 原则），也不要一个可能永久阻塞的聪明办法。
_EXTRA_BIN_DIRS = (
    "/usr/local/bin",       # npm 全局 / Intel Homebrew
    "/opt/homebrew/bin",    # Apple Silicon Homebrew
    "/opt/local/bin",       # MacPorts
    "~/.local/bin",
    "~/.bun/bin",
    "~/.deno/bin",
    "~/.cargo/bin",
    "~/.npm-global/bin",
    "~/bin",
)


def _which(name: str) -> Optional[str]:
    """Shared read-only lookup, including Windows GUI installation roots."""
    return local_discovery.which(name, extra_dirs=_EXTRA_BIN_DIRS)


def _claude_cmd(claude_path: Optional[str] = None) -> list[str]:
    """Return argv prefix to invoke the claude CLI.

    解析顺序跟 dsh/codex 对齐：CLAUDE_PATH 环境变量 > _which()（PATH +
    GUI 看不见的常见安装目录）。之前这里是三个 CLI 里唯一没有环境变量
    兜底的。

    On Windows, `claude` in PATH is a .cmd shim that cannot be directly
    exec'd; resolve to the real .exe inside node_modules.
    """
    which = claude_path or os.environ.get("CLAUDE_PATH") or _which("claude")
    if which is None:
        return ["claude"]
    if sys.platform == "win32" and which.lower().endswith(".cmd"):
        exe = os.path.join(os.path.dirname(which),
                           "node_modules", "@anthropic-ai", "claude-code",
                           "bin", "claude.exe")
        if os.path.exists(exe):
            return [exe]
        return ["cmd", "/c", which]
    return [which]


def _resolve_codebuddy_paths(node_path: Optional[str],
                              script_path: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Resolve codebuddy's node/script paths, same BYO spirit as
    _claude_cmd() (no hardcoded machine-specific defaults baked into
    committed config -- see CLAUDE.md's "资源配置 BYO 原则" gap note).
    Priority: an explicit value already given (from capability_matrix.yaml
    or passed in directly) > WORKBUDDY_NODE_PATH/WORKBUDDY_SCRIPT_PATH env
    vars > best-effort auto-detect of the default install location.

    Windows discovery also checks registered WorkBuddy installations and
    its per-user Node runtime. Explicit paths retain their legacy contract.
    Deliberately called
    lazily at .run() time, not at construction -- mirrors _claude_cmd()'s
    laziness, and keeps build_executor()/Router() construction free of
    filesystem assumptions (some callers construct a real, non-mocked
    Router purely to inspect config, never intending to actually run
    anything -- see tests/test_difficulty_routing.py).
    """
    node_path = node_path or os.environ.get("WORKBUDDY_NODE_PATH")
    script_path = script_path or os.environ.get("WORKBUDDY_SCRIPT_PATH")
    if sys.platform == 'win32' and not (node_path and script_path):
        found = local_discovery.discover_local('codebuddy',
                    {k: v for k, v in {'node': node_path, 'script': script_path}.items() if v})
        return tuple(found['argv']) if found['status'] == 'found' else (None, None)
    if sys.platform == "darwin":
        if not script_path:
            candidate = ("/Applications/WorkBuddy.app/Contents/Resources/"
                         "app.asar.unpacked/cli/dist/codebuddy.js")
            if os.path.exists(candidate):
                script_path = candidate
        if not node_path:
            matches = sorted(glob.glob(os.path.expanduser(
                "~/.workbuddy/binaries/node/versions/*/bin/node")))
            if matches:
                node_path = matches[-1]  # highest version string, if several installed
    return node_path, script_path


def _dsh_cmd(dsh_path: Optional[str]) -> list[str]:
    """Return argv prefix to invoke the dsh CLI (BYO, mirrors _claude_cmd).
    Explicit dsh_path (from capability_matrix.yaml) wins, then the DSH_PATH
    env var, then `dsh` resolved on PATH. Lazy -- called at .run() time, not
    construction, for the same reason as _resolve_codebuddy_paths (some
    callers build a real Router just to inspect config and must not touch
    the filesystem)."""
    which = dsh_path or os.environ.get("DSH_PATH") or _which("dsh")
    if which is None:
        return []
    if sys.platform == "win32" and which.lower().endswith((".cmd", ".bat")):
        # A .cmd/.bat only runs through cmd.exe, which re-parses dsh's prompt
        # argument: a `"` plus `&` in a goal or an upstream agent's output
        # would run commands (BatBadBut). Run npm's target with node instead.
        return _npm_shim_argv(which)
    return [which]


def _npm_shim_argv(shim: str) -> list[str]:
    """`[node, script]` from npm's cmd-shim for a node CLI: it sets _prog to
    the node.exe beside the shim, else node on PATH, then runs
    `"%_prog%" "%dp0%\\<script>" %*` (the script may have no extension).
    [] for any other shape: refuse, never cmd /c."""
    try:
        with open(shim, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    m = re.search(r'"%_prog%"\s+"%dp0%\\([^"]+)"', text)
    if not m or 'SET "_prog=node"' not in text:
        return []
    base = os.path.dirname(shim)
    script = os.path.join(base, m.group(1))
    node = os.path.join(base, "node.exe")
    node = node if os.path.isfile(node) else _which("node")
    return [node, script] if node and os.path.isfile(script) else []


def _codex_cmd(codex_path: Optional[str]) -> list[str]:
    """Resolve the standalone Codex CLI lazily.

    Explicit matrix configuration wins, then CODEX_PATH, then PATH.  The
    lazy lookup keeps Router construction side-effect free and lets a CLI
    installed after the server starts become available on the next run.
    """
    which = codex_path or os.environ.get("CODEX_PATH") or _which("codex")
    if which is None:
        return []
    if sys.platform == "win32" and which.lower().endswith((".cmd", ".bat")):
        # Never send dynamic prompts through cmd.exe. npm's shim sits next
        # to node_modules; resolve the package's native binary directly.
        direct = os.path.splitext(which)[0] + ".exe"
        candidates = [direct]
        candidates.extend(glob.glob(os.path.join(
            os.path.dirname(which), "node_modules", "@openai", "codex",
            "node_modules", "@openai", "codex-win32-*", "vendor",
            "*-pc-windows-msvc", "bin", "codex.exe")))
        candidates.extend(glob.glob(os.path.join(
            os.path.dirname(which), "node_modules", "@openai", "codex",
            "vendor", "*-pc-windows-msvc", "bin", "codex.exe")))
        native = next((path for path in candidates if os.path.isfile(path)), None)
        return [native] if native else []
    return [which]


def _dsh_model_patch_text(provider: str, model: str, effort: str = '') -> str:
    """A `--patch` overlay for dsh: a top-level YAML ARRAY of loader patch
    entries (NOT a mapping) -- verified against `dsh --dump-config`
    2026-08-15. Each entry targets a plugin by id and REPLACES its config
    wholesale, so `provider` must be supplied too (agent-default-model's
    default config is `{provider, model}`; omitting provider would drop it)."""
    config = {'provider': provider, 'model': model}
    if effort:
        config['reasoningEffort'] = effort
    return yaml.safe_dump([{'id': 'agent-default-model', 'config': config}], sort_keys=False)


def _dsh_unavailable(text: str) -> bool:
    return any(m in text.lower() for m in _DSH_UNAVAILABLE_MARKERS)


# error_kind values (empty = ok, or unclassified failure treated as task_error):
#   transient   -- network jitter / mid-stream rate limit; retry same agent, backoff
#   unavailable -- structurally broken for this run (quota/auth/model missing);
#                  circuit-break the agent, skip it for the rest of the run
#   task_error  -- agent ran but produced an unusable result; degrade to next agent
_UNAVAILABLE_MARKERS = (
    "credits required", "does not have access", "model not found",
    "invalid api key", "authentication_error", "permission denied",
)

_CODEBUDDY_UNAVAILABLE_MARKERS = (
    "insufficient credit", "积分不足", "credit exhausted",
    "unauthorized", "not logged in", "登录已过期", "authentication failed",
)

# dsh (DeepSeek Harness) structural-failure markers: auth/credential/quota
# problems won't fix on retry, so they circuit-break the agent for the run.
# Actual dsh error text is unverified (no real probe yet) -- kept conservative.
_DSH_UNAVAILABLE_MARKERS = (
    "api key", "api_key", "apikey", "authentication", "unauthorized",
    "credentials", "insufficient", "quota", "model not found",
)

_CODEX_UNAVAILABLE_MARKERS = (
    "not logged in", "codex login", "authentication", "unauthorized",
    "invalid api key", "quota", "usage limit",
    "model not found", "does not have access", "not available for this account",
)

_CODEX_TRANSIENT_MARKERS = (
    "429", "rate limit", "overloaded", "temporarily unavailable",
    "connection reset", "connection refused", "connection error",
    "network error", "timed out", "timeout", "try again",
)

_CODEX_STRIPPED_ENV = {
    # Force the documented ChatGPT-login path so cost_usd=0.0 stays true.
    "CODEX_API_KEY", "OPENAI_API_KEY",
    # Credentials loaded by Agent Hub for unrelated connectors must not be
    # inherited by a full agent capable of running shell commands.
    "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY",
}


def _codex_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if key.upper() in _CODEX_STRIPPED_ENV:
            env.pop(key, None)
    return env


@dataclass
class ExecResult:
    ok: bool
    output: str = ""
    error: str = ""
    cost_usd: float = 0.0
    error_kind: str = ""
    usage: Optional[dict] = None   # raw token usage from the provider, if any
    cost_credits: float = 0.0      # workbuddy/codebuddy credit spend, independent of cost_usd
    session_id: Optional[str] = None  # claude_code only -- lets a later request resume
                                        # this exact conversation via `claude -p --resume`


class Executor:
    name = "base"

    async def run(self, task: Task, context: str,
                   on_event: Optional[Callable[[str], None]] = None) -> ExecResult:  # pragma: no cover
        raise NotImplementedError


class MockExecutor(Executor):
    """Fake agent for dry runs and frontend demos."""
    name = "mock"

    def __init__(self, delay: float = 0.3):
        self.delay = delay

    async def run(self, task: Task, context: str,
                   on_event: Optional[Callable[[str], None]] = None) -> ExecResult:
        await asyncio.sleep(self.delay)
        return ExecResult(ok=True, output=f"[mock output for '{task.title}']", cost_usd=0.0)


async def _kill_timed_out(proc) -> None:
    """wait_for(proc.communicate()) timing out only stops the WAITING: the
    CLI keeps running (and editing the workdir) while the engine retries the
    same agent, so two copies end up working one task. Kill and reap it,
    the same way the streaming paths already do."""
    if proc is None or proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=5)


def _spawn_not_found(exc: FileNotFoundError, not_found: str) -> ExecResult:
    """WinError 206 is a command line past Windows' 32,767-char cap, raised by
    Python as FileNotFoundError. For executors whose prompt rides in argv
    that means THIS prompt is too long -- not the CLI missing, which would
    circuit-break the agent for the whole run."""
    if getattr(exc, "winerror", None) == 206:
        return ExecResult(ok=False, error=(
            "prompt too long for the Windows command line (limit 32,767 chars); "
            "this agent takes its prompt as a command-line argument"),
            error_kind="task_error")
    return ExecResult(ok=False, error=not_found, error_kind="unavailable")


class ClaudeCodeExecutor(Executor):
    """Runs `claude -p` headless. Requires Claude Code installed & logged in.

    Streams by default (--output-format stream-json --verbose
    --include-partial-messages): reads stdout line by line, forwards a
    short human-readable summary of each completed content block (thinking
    started / tool used / text drafted) via on_event as the model works,
    and still computes the final ExecResult from the tail `result` line --
    exactly the same classification logic as the non-streaming path. Pass
    stream=False to fall back to the old one-shot --output-format json +
    communicate() path (escape hatch if streaming misbehaves; see
    docs/superpowers/specs/2026-07-23-streaming-thinking-design.md §7).
    Runs inside `workdir` so file-touching tasks operate on your project.
    """
    name = "claude_code"

    def __init__(self, model: str = "sonnet", allowed_tools: str = "Read",
                 workdir: str = ".", timeout: int = 600, stream: bool = True,
                 reasoning_effort: str = '', claude_path: Optional[str] = None):
        self.model = model
        self.claude_path = claude_path
        self.reasoning_effort = reasoning_effort
        self.allowed_tools = allowed_tools
        self.workdir = workdir
        self.timeout = timeout
        self.stream = stream

    def _selection_args(self) -> list[str]:
        args = ['--model', self.model] if self.model else []
        if self.reasoning_effort:
            args.extend(['--effort', self.reasoning_effort])
        return args

    async def run(self, task: Task, context: str,
                   on_event: Optional[Callable[[str], None]] = None) -> ExecResult:
        if self.stream:
            return await self._run_streaming(task, context, on_event)
        return await self._run_blocking(task, context)

    async def _run_blocking(self, task: Task, context: str) -> ExecResult:
        prompt = task.prompt if not context else f"{task.prompt}\n\n<context>\n{context}\n</context>"
        cmd = (_claude_cmd(self.claude_path) if self.claude_path else _claude_cmd()) + [
            "-p",
            *self._selection_args(),
            "--output-format", "json",
            "--allowedTools", self.allowed_tools,
        ]
        return await self._exec_json_cmd(cmd, prompt)

    async def run_resumed(self, message: str, session_id: str) -> ExecResult:
        """One follow-up turn in an existing conversation, via `claude -p
        --resume <session_id>`. Always blocking/non-streaming -- follow-up
        replies are typically short answers, not long coding tasks, and
        there is no on_event consumer for this path yet. `message` is sent
        as-is, NOT wrapped in the usual <context> block -- the resumed
        session already carries the original task's context.
        """
        cmd = (_claude_cmd(self.claude_path) if self.claude_path else _claude_cmd()) + [
            "-p",
            *self._selection_args(),
            "--output-format", "json",
            "--allowedTools", self.allowed_tools,
            "--resume", session_id,
        ]
        return await self._exec_json_cmd(cmd, message)

    async def _exec_json_cmd(self, cmd: list[str], prompt: str) -> ExecResult:
        """Run `cmd` (a full `claude -p ... --output-format json` argv,
        built by the caller) and classify the result. Shared by
        _run_blocking and run_resumed so the subprocess/timeout/parse
        handling is defined exactly once. The prompt goes through stdin,
        never argv: Windows caps a command line at 32,767 chars, and a
        cmd.exe shim would parse whatever is in it."""
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=self.workdir, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")), timeout=self.timeout)
        except asyncio.TimeoutError:
            await _kill_timed_out(proc)
            return ExecResult(ok=False, error=f"claude -p timed out after {self.timeout}s",
                              error_kind="transient")
        except FileNotFoundError:
            return ExecResult(ok=False, error="`claude` CLI not found — is Claude Code installed?",
                              error_kind="unavailable")

        raw = stdout.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            return ExecResult(ok=False, error=(stderr.decode(errors="replace") or raw)[:2000],
                              error_kind="task_error")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # text fallback — still usable
            return ExecResult(ok=True, output=raw)
        return _claude_result_from_dict(data)

    async def _run_streaming(self, task: Task, context: str,
                              on_event: Optional[Callable[[str], None]]) -> ExecResult:
        prompt = task.prompt if not context else f"{task.prompt}\n\n<context>\n{context}\n</context>"
        cmd = (_claude_cmd(self.claude_path) if self.claude_path else _claude_cmd()) + [
            "-p",                   # prompt via stdin, see _exec_json_cmd
            *self._selection_args(),
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--allowedTools", self.allowed_tools,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=self.workdir, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=10 * 1024 * 1024,
            )
        except FileNotFoundError:
            return ExecResult(ok=False, error="`claude` CLI not found — is Claude Code installed?",
                              error_kind="unavailable")

        # Drain stderr concurrently with the stdout readline loop below --
        # otherwise a child that fills the stderr pipe buffer while we're
        # still blocked reading stdout deadlocks (communicate() does this
        # for free in _run_blocking; the manual loop here has to do it itself).
        # Same reason the prompt is fed from its own task.
        stderr_task = asyncio.create_task(proc.stderr.read())
        feed_task = asyncio.create_task(_feed_prompt(proc, prompt))

        last_result: Optional[dict] = None
        try:
            while True:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=self.timeout)
                if not line:
                    break
                obj = _parse_claude_stream_line(line, on_event)
                if obj is not None and obj.get("type") == "result":
                    last_result = obj
        except asyncio.TimeoutError:
            stderr_task.cancel()
            feed_task.cancel()
            proc.kill()
            await proc.wait()
            return ExecResult(ok=False, error=f"claude -p timed out after {self.timeout}s",
                              error_kind="transient")
        except asyncio.LimitOverrunError:
            # Defense-in-depth: with limit=10MiB above this should never
            # actually fire, but a single NDJSON line over the StreamReader
            # buffer must still degrade gracefully rather than crash the task.
            stderr_task.cancel()
            feed_task.cancel()
            proc.kill()
            await proc.wait()
            return ExecResult(ok=False,
                              error="claude -p produced an oversized stream line (over 10MB)",
                              error_kind="task_error")

        await proc.wait()
        await feed_task
        stderr = (await stderr_task).decode(errors="replace")
        if last_result is not None:
            return _claude_result_from_dict(last_result)
        return ExecResult(ok=False,
                          error=(stderr or "claude -p exited without a result line")[:2000],
                          error_kind="task_error")


def _claude_result_from_dict(data: dict) -> ExecResult:
    """Classify a claude result object (either the single blob from
    --output-format json, or the tail `result` line of stream-json --
    both carry the same keys) into an ExecResult. Shared by
    ClaudeCodeExecutor's blocking and streaming paths so the
    success/failure/cost logic is defined exactly once."""
    if data.get("is_error"):
        result_text = str(data.get("result") or "")
        status = data.get("api_error_status")
        duration_api_ms = data.get("duration_api_ms") or 0
        if any(m in result_text.lower() for m in _UNAVAILABLE_MARKERS):
            kind = "unavailable"
        elif status == 429 and duration_api_ms == 0:
            # rejected before any tokens were generated — structural
            # (quota/model access), not a mid-stream rate limit
            kind = "unavailable"
        elif status == 429:
            kind = "transient"
        else:
            kind = "task_error"
        return ExecResult(ok=False, error=result_text[:2000],
                          cost_usd=float(data.get("total_cost_usd") or 0.0),
                          error_kind=kind, session_id=data.get("session_id"))
    return ExecResult(
        ok=True,
        output=str(data.get("result", "")),
        cost_usd=float(data.get("total_cost_usd") or 0.0),
        usage=data.get("usage") or None,
        session_id=data.get("session_id"),
    )


def _parse_claude_stream_line(line: bytes,
                              on_event: Optional[Callable[[str], None]]) -> Optional[dict]:
    """Parse one NDJSON line from --output-format stream-json. Malformed
    lines are swallowed (return None) -- streaming is best-effort, it must
    never crash the task over a line it can't make sense of. `assistant`
    lines get forwarded to on_event via _format_claude_thinking_detail;
    every other line type is returned as-is for the caller to inspect
    (e.g. the final `result` line) or ignore."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("type") == "assistant" and on_event is not None:
        content = (obj.get("message") or {}).get("content") or []
        if content:
            detail = _format_claude_thinking_detail(content[-1])
            if detail:
                on_event(detail)
    return obj


def _format_claude_thinking_detail(block: dict) -> Optional[str]:
    """Turn one completed content block into a short human-readable
    progress line. Claude's `thinking` blocks stream an empty text field
    (only an opaque signature) -- real reasoning content isn't available
    (confirmed via a live probe 2026-07-23), so thinking gets a fixed
    placeholder rather than showing nothing."""
    kind = block.get("type")
    if kind == "thinking":
        return "正在思考…"
    if kind == "tool_use":
        name = block.get("name", "?")
        arg = _first_useful_arg(block.get("input") or {})
        return f"{name}({arg})" if arg else f"{name}(...)"
    if kind == "text":
        text = (block.get("text") or "").strip()
        if not text:
            return None
        return text[:100] + ("…" if len(text) > 100 else "")
    return None


def _first_useful_arg(tool_input: dict) -> str:
    for key in ("file_path", "path", "command", "pattern", "prompt"):
        if key in tool_input:
            return str(tool_input[key])[:60]
    if tool_input:
        return str(next(iter(tool_input.values())))[:60]
    return ""


def _codex_error_kind(text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in _CODEX_UNAVAILABLE_MARKERS):
        return "unavailable"
    if any(marker in lowered for marker in _CODEX_TRANSIENT_MARKERS):
        return "transient"
    return "task_error"


def _codex_event_error(event: dict) -> str:
    """Extract a readable error from both `error` and `turn.failed` events."""
    error = event.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("detail") or error)
    if error:
        return str(error)
    return str(event.get("message") or event.get("detail") or "Codex turn failed")


def _codex_progress(item: dict) -> Optional[str]:
    kind = item.get("type")
    if kind == "reasoning":
        text = str(item.get("text") or "").strip()
        return text[:100] + ("…" if len(text) > 100 else "") if text else "正在思考…"
    if kind == "command_execution":
        command = str(item.get("command") or "").strip()
        if command:
            return f"运行命令：{command[:100]}" + ("…" if len(command) > 100 else "")
    if kind == "file_change":
        changes = item.get("changes") or []
        paths = [str(c.get("path")) for c in changes
                 if isinstance(c, dict) and c.get("path")]
        if paths:
            shown = ", ".join(paths[:3])
            return f"修改文件：{shown}" + ("…" if len(paths) > 3 else "")
        return "正在修改文件…"
    return None


def _posix_group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class _WindowsKillJob:
    """Small ownership wrapper around a kill-on-close Windows Job Object."""

    def __init__(self, handle, kernel32):
        self.handle = handle
        self.kernel32 = kernel32

    def terminate(self) -> bool:
        if not self.handle:
            return True
        return bool(self.kernel32.TerminateJobObject(self.handle, 1))

    def close(self) -> None:
        if self.handle:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None


def _create_windows_kill_job(proc) -> _WindowsKillJob:
    """Assign a freshly spawned Codex process to a kill-on-close Job Object.

    Called only on Windows. Failure is fatal for that executor invocation:
    running a full agent without reliable tree ownership would make timeout
    and user cancellation dishonest.
    """
    import ctypes
    from ctypes import wintypes

    class BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_void_p),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimit),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    job = _WindowsKillJob(handle, kernel32)
    info = ExtendedLimit()
    info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
            handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        error = ctypes.get_last_error()
        job.close()
        raise OSError(error, "SetInformationJobObject failed")
    popen = proc._transport.get_extra_info("subprocess")
    process_handle = getattr(popen, "_handle", None)
    if process_handle is None or not kernel32.AssignProcessToJobObject(
            handle, wintypes.HANDLE(process_handle)):
        error = ctypes.get_last_error()
        job.close()
        raise OSError(error, "AssignProcessToJobObject failed")
    return job


async def _terminate_codex_process(proc, process_group: Optional[int] = None,
                                   windows_job: Optional[_WindowsKillJob] = None) -> bool:
    """Deterministically terminate Codex and its tool descendants.

    POSIX uses the PGID captured at spawn even after the group leader exits;
    checking only proc.returncode would orphan children that inherited a pipe.
    Windows uses taskkill /T because Process.kill() covers only the leader.
    """
    if os.name == "nt":
        if windows_job is not None:
            try:
                tree_killed = await asyncio.to_thread(windows_job.terminate)
            finally:
                windows_job.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=3)
            return tree_killed
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            result = None
        tree_killed = bool(result is not None and result.returncode == 0)
        if not tree_killed and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=3)
        return tree_killed

    pgid = process_group or proc.pid
    existed = _posix_group_alive(pgid)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGTERM)
    deadline = asyncio.get_running_loop().time() + 1.0
    while _posix_group_alive(pgid) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
    if _posix_group_alive(pgid):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)
    elif not existed and proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
    if proc.returncode is None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=3)
    deadline = asyncio.get_running_loop().time() + 1.0
    while _posix_group_alive(pgid) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
    return not _posix_group_alive(pgid)


async def _read_bounded_tail(reader, max_bytes: int = 64 * 1024) -> bytes:
    """Drain a pipe without retaining unbounded diagnostics in memory."""
    tail = bytearray()
    while True:
        chunk = await reader.read(8192)
        if not chunk:
            break
        tail.extend(chunk)
        if len(tail) > max_bytes:
            del tail[:-max_bytes]
    return bytes(tail)


async def _feed_prompt(proc, prompt: str) -> None:
    """Send the dynamic prompt through stdin and close it deterministically."""
    try:
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        proc.stdin.close()
        wait_closed = getattr(proc.stdin, "wait_closed", None)
        if wait_closed is not None:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await wait_closed()


class CodexExecutor(Executor):
    """Run the authenticated local Codex CLI in non-interactive JSONL mode.

    No model is supplied by default, so the user's Codex configuration stays
    authoritative.  Agent Hub owns the working-directory boundary and uses
    Codex's `workspace-write` sandbox; it never enables danger-full-access.
    ChatGPT-login calls are subscription-backed, therefore cost_usd is zero
    while the provider's raw token usage is still retained for reporting.
    """
    name = "codex"

    def __init__(self, model: str = "", workdir: str = ".", timeout: int = 900,
                 codex_path: Optional[str] = None, reasoning_effort: str = ''):
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.workdir = workdir
        self.timeout = timeout
        self.codex_path = codex_path

    def _argv(self, prompt: str, sandbox: str = "workspace-write") -> list[str]:
        cmd = _codex_cmd(self.codex_path)
        if not cmd:
            return []
        argv = cmd + [
            "exec", "--json", "--ephemeral",
            "--sandbox", sandbox,
            "--skip-git-repo-check", "--color", "never",
        ]
        if self.model:
            argv.extend(["--model", self.model])
        if self.reasoning_effort:
            argv.extend(['-c', 'model_reasoning_effort=' + json.dumps(self.reasoning_effort)])
        # `-` is Codex's documented stdin prompt. Keeping user/upstream text
        # out of argv prevents option confusion, command-shell parsing on
        # Windows, and platform command-line length limits.
        argv.append("-")
        return argv

    async def run(self, task: Task, context: str,
                  on_event: Optional[Callable[[str], None]] = None) -> ExecResult:
        return await self._run(task, context, on_event, sandbox="workspace-write")

    async def run_preflight(self, task: Task, context: str,
                            on_event: Optional[Callable[[str], None]] = None) -> ExecResult:
        """Connectivity/auth probe with no permission to modify the workspace."""
        return await self._run(task, context, on_event, sandbox="read-only")

    async def _run(self, task: Task, context: str,
                   on_event: Optional[Callable[[str], None]],
                   sandbox: str) -> ExecResult:
        prompt = task.prompt if not context else (
            f"{task.prompt}\n\n<context>\n{context}\n</context>")
        argv = self._argv(prompt, sandbox=sandbox)
        if not argv:
            return ExecResult(
                ok=False,
                error="`codex` CLI not found -- install @openai/codex or set CODEX_PATH",
                error_kind="unavailable",
            )

        popen_kwargs = ({"start_new_session": True} if os.name != "nt" else {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP})
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=self.workdir,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=10 * 1024 * 1024,
                env=_codex_env(),
                **popen_kwargs,
            )
        except FileNotFoundError:
            return ExecResult(ok=False, error=f"Codex CLI not found at {argv[0]}",
                              error_kind="unavailable")

        process_group = proc.pid if os.name != "nt" else None
        windows_job = None
        if os.name == "nt":
            try:
                windows_job = _create_windows_kill_job(proc)
            except OSError as exc:
                await _terminate_codex_process(proc)
                return ExecResult(
                    ok=False,
                    error=f"Codex process-tree isolation could not be established: {exc}"[:2000],
                    error_kind="unavailable")
        stderr_task = asyncio.create_task(_read_bounded_tail(proc.stderr))
        work = asyncio.gather(
            self._consume_jsonl(proc, on_event),
            _feed_prompt(proc, prompt),
        )
        try:
            state, _ = await asyncio.wait_for(work, timeout=self.timeout)
            stderr = (await stderr_task).decode("utf-8", errors="replace").strip()
        except asyncio.TimeoutError:
            work.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await work
            stderr_task.cancel()
            cleaned = await _terminate_codex_process(proc, process_group, windows_job)
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task
            if not cleaned:
                return ExecResult(ok=False,
                                  error="codex exec timed out and its process tree could not be terminated",
                                  error_kind="task_error")
            return ExecResult(ok=False,
                              error=f"codex exec timed out after {self.timeout}s",
                              error_kind="transient")
        except asyncio.CancelledError:
            work.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await work
            stderr_task.cancel()
            cleaned = await _terminate_codex_process(proc, process_group, windows_job)
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task
            if not cleaned:
                raise RuntimeError("Codex process tree could not be terminated after cancellation")
            raise
        except (asyncio.LimitOverrunError, ValueError):
            work.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await work
            stderr_task.cancel()
            cleaned = await _terminate_codex_process(proc, process_group, windows_job)
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task
            if not cleaned:
                return ExecResult(ok=False,
                                  error="oversized Codex output and process-tree cleanup failed",
                                  error_kind="task_error")
            return ExecResult(ok=False,
                              error="codex exec produced an oversized JSONL line (over 10MB)",
                              error_kind="task_error")
        except Exception as exc:
            work.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await work
            stderr_task.cancel()
            cleaned = await _terminate_codex_process(proc, process_group, windows_job)
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task
            if not cleaned:
                return ExecResult(ok=False,
                                  error=f"codex exec failed ({exc}); process-tree cleanup also failed"[:2000],
                                  error_kind="task_error")
            return ExecResult(ok=False,
                              error=f"codex exec failed: {exc}"[:2000],
                              error_kind="task_error")

        # Successful Codex exits should not leave daemonized tool children
        # behind either. Usually the group is already gone, making this a
        # no-op; the saved PGID handles a leader-exited orphan correctly.
        if not await _terminate_codex_process(proc, process_group, windows_job):
            return ExecResult(ok=False,
                              error="codex exec exited but left a process tree that could not be terminated",
                              error_kind="task_error", usage=state["usage"] or None)
        failure = state["failure"]
        if proc.returncode != 0 or failure:
            text = failure or stderr or f"codex exec exited with code {proc.returncode}"
            return ExecResult(ok=False, error=text[:2000],
                              error_kind=_codex_error_kind(text),
                              usage=state["usage"] or None)
        output = state["output"]
        if not output:
            return ExecResult(ok=False,
                              error="codex exec exited without an agent message",
                              error_kind="task_error", usage=state["usage"] or None)
        return ExecResult(ok=True, output=output, cost_usd=0.0,
                          usage=state["usage"] or None)

    async def _consume_jsonl(self, proc,
                             on_event: Optional[Callable[[str], None]]) -> dict:
        state = {"output": "", "usage": None, "failure": ""}
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            if event_type == "item.completed":
                item = event.get("item") or {}
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "agent_message":
                    text = str(item.get("text") or "").strip()
                    if text:
                        state["output"] = text
                elif on_event is not None:
                    detail = _codex_progress(item)
                    if detail:
                        on_event(detail)
            elif event_type == "turn.completed":
                usage = event.get("usage")
                state["usage"] = usage if isinstance(usage, dict) else None
            elif event_type in ("error", "turn.failed"):
                state["failure"] = _codex_event_error(event)
        await proc.wait()
        return state


class OpenRouterExecutor(Executor):
    """Chat-completions call via OpenRouter, through the official `openai`
    SDK pointed at OpenRouter's base_url (migrated from raw httpx
    2026-07-23 as part of adding streaming). Needs OPENROUTER_API_KEY.
    Streams by default; unlike claude_code/codebuddy this executor never
    sends a `tools` param, so its on_event progress is text-draft previews
    only (batched every ~100 characters), no tool-use summaries.
    """
    name = "openrouter"
    BASE_URL = "https://openrouter.ai/api/v1"

    def __init__(self, model: str, timeout: int = 300, stream: bool = True):
        self.model = model
        self.timeout = timeout
        self.stream = stream

    async def run(self, task: Task, context: str,
                   on_event: Optional[Callable[[str], None]] = None) -> ExecResult:
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            return ExecResult(ok=False, error="OPENROUTER_API_KEY not set",
                              error_kind="unavailable")
        messages = []
        if context:
            messages.append({"role": "user",
                             "content": f"Context from upstream tasks:\n{context}"})
        messages.append({"role": "user", "content": task.prompt})

        client = AsyncOpenAI(base_url=self.BASE_URL, api_key=key, timeout=self.timeout, max_retries=0)
        try:
            if self.stream:
                return await self._run_streaming(client, messages, on_event)
            return await self._run_blocking(client, messages)
        finally:
            # AsyncOpenAI.close() is a coroutine that releases the
            # underlying httpx connection pool -- a long-running DAG can
            # create many OpenRouterExecutor.run() calls in sequence, each
            # with a fresh client, and never closing them would leak open
            # connections across the run. getattr-guarded so the test
            # suite's lightweight fake client (no .close attribute) is
            # unaffected.
            close = getattr(client, "close", None)
            if close is not None:
                await close()

    async def _run_blocking(self, client, messages: list) -> ExecResult:
        try:
            completion = await client.chat.completions.create(model=self.model, messages=messages)
        except Exception as e:
            return _openrouter_error_result(e)
        return _openrouter_result_from_completion(completion)

    async def _run_streaming(self, client, messages: list,
                              on_event: Optional[Callable[[str], None]]) -> ExecResult:
        buffer = ""
        try:
            async with client.chat.completions.stream(model=self.model, messages=messages) as stream:
                async for event in stream:
                    if event.type == "content.delta":
                        buffer += event.delta
                        if on_event is not None and len(buffer) >= 100:
                            on_event(buffer)
                            buffer = ""
                if on_event is not None and buffer:
                    on_event(buffer)
                completion = await stream.get_final_completion()
        except Exception as e:
            return _openrouter_error_result(e)
        return _openrouter_result_from_completion(completion)


def _openrouter_result_from_completion(completion) -> ExecResult:
    data = completion.model_dump()
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ExecResult(ok=False, error=f"unexpected response: {str(data)[:500]}",
                          error_kind="task_error")
    usage = data.get("usage") or {}
    cost = float(usage.get("cost") or 0.0)
    return ExecResult(ok=True, output=text or "", cost_usd=cost, usage=usage or None)


def _openrouter_error_result(e: Exception) -> ExecResult:
    """Classify an openai-SDK exception into an ExecResult, mirroring the
    old httpx-based status-code branching (401/403/404 -> unavailable, 429
    with a retry-after header -> transient else unavailable, 5xx ->
    transient, connection/timeout -> transient, everything else ->
    task_error). See https://github.com/openai/openai-python#error-handling
    for the exception hierarchy."""
    import openai

    if isinstance(e, (openai.APIConnectionError, openai.APITimeoutError)):
        return ExecResult(ok=False, error=f"connection error: {e}"[:500], error_kind="transient")
    if isinstance(e, openai.RateLimitError):
        headers = getattr(getattr(e, "response", None), "headers", {}) or {}
        kind = "transient" if "retry-after" in headers else "unavailable"
        return ExecResult(ok=False, error=str(e)[:500], error_kind=kind)
    if isinstance(e, (openai.AuthenticationError, openai.PermissionDeniedError, openai.NotFoundError)):
        return ExecResult(ok=False, error=str(e)[:500], error_kind="unavailable")
    if isinstance(e, openai.InternalServerError):
        return ExecResult(ok=False, error=str(e)[:500], error_kind="transient")
    return ExecResult(ok=False, error=str(e)[:500], error_kind="task_error")


class CodeBuddyExecutor(Executor):
    """Runs the CodeBuddy CLI bundled with WorkBuddy (see
    docs/research/2026-07-21-workbuddy-cli.md). Same headless JSON contract
    family as ClaudeCodeExecutor. Streams by default (--output-format
    stream-json --verbose --include-partial-messages), same NDJSON event
    family as ClaudeCodeExecutor (content_block_start/delta/stop,
    `assistant` snapshot lines, tail `result` line) -- confirmed via a real
    probe 2026-07-23 (hy3 model). One real difference: `thinking` blocks
    here DO stream real, readable text (unlike claude's empty-text-plus-
    signature-only) -- this executor's thinking summary shows an actual
    text preview instead of a fixed placeholder. Three other differences
    from ClaudeCodeExecutor, unchanged from before this task:
      - non-streaming stdout is a JSON ARRAY of messages; the result object
        is the LAST element, not the only element
      - spend is in workbuddy credits, read off the assistant message's
        providerData.rawUsage.credit -- NOT the result object's
        total_cost_usd, which codebuddy always reports as 0
      - --tools "" genuinely disables all tools (verified 2026-07-21;
        unlike claude's --allowedTools "" which does NOT restrict, see
        commit 5934e3d)
    """
    name = "codebuddy"

    def __init__(self, node_path: Optional[str], script_path: Optional[str], model: str,
                 workdir: str = ".", timeout: int = 600, stream: bool = True):
        self.node_path = node_path
        self.script_path = script_path
        self.model = model
        self.workdir = workdir
        self.timeout = timeout
        self.stream = stream

    async def run(self, task: Task, context: str,
                   on_event: Optional[Callable[[str], None]] = None) -> ExecResult:
        node_path, script_path = _resolve_codebuddy_paths(self.node_path, self.script_path)
        if not node_path or not script_path:
            return ExecResult(ok=False, error=(
                "codebuddy node/script path not configured -- set WORKBUDDY_NODE_PATH "
                "and WORKBUDDY_SCRIPT_PATH env vars, or node_path/script_path directly "
                "in capability_matrix.yaml (auto-detect only covers the default macOS "
                "WorkBuddy install location)"), error_kind="unavailable")
        if self.stream:
            return await self._run_streaming(task, context, on_event, node_path, script_path)
        return await self._run_blocking(task, context, node_path, script_path)

    async def _run_blocking(self, task: Task, context: str,
                             node_path: str, script_path: str) -> ExecResult:
        prompt = task.prompt if not context else f"{task.prompt}\n\n<context>\n{context}\n</context>"
        cmd = [
            node_path, script_path,
            "-p", prompt,
            "--model", self.model,
            "--output-format", "json",
            "--tools", "",
        ]
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=self.workdir,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            await _kill_timed_out(proc)
            return ExecResult(ok=False, error=f"codebuddy timed out after {self.timeout}s",
                              error_kind="transient")
        except FileNotFoundError as exc:
            return _spawn_not_found(exc, f"codebuddy CLI not found at {script_path} "
                                         f"(node: {node_path}) -- check capability_matrix.yaml")

        raw = stdout.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            return ExecResult(ok=False, error=(stderr.decode(errors="replace") or raw)[:2000],
                              error_kind="task_error")
        try:
            messages = json.loads(raw)
        except json.JSONDecodeError:
            return ExecResult(ok=False, error=f"unparseable codebuddy output: {raw[:500]}",
                              error_kind="task_error")
        if not isinstance(messages, list) or not messages:
            return ExecResult(ok=False, error=f"unexpected codebuddy output shape: {raw[:500]}",
                              error_kind="task_error")

        credits = _extract_codebuddy_credit(messages)
        return _codebuddy_exec_result(messages[-1], credits,
                                      _extract_codebuddy_usage(messages))

    async def _run_streaming(self, task: Task, context: str,
                              on_event: Optional[Callable[[str], None]],
                              node_path: str, script_path: str) -> ExecResult:
        prompt = task.prompt if not context else f"{task.prompt}\n\n<context>\n{context}\n</context>"
        cmd = [
            node_path, script_path,
            "-p", prompt,
            "--model", self.model,
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--tools", "",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=self.workdir,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=10 * 1024 * 1024,
            )
        except FileNotFoundError as exc:
            return _spawn_not_found(exc, f"codebuddy CLI not found at {script_path} "
                                         f"(node: {node_path}) -- check capability_matrix.yaml")

        # Drain stderr concurrently with the stdout readline loop below --
        # otherwise a child that fills the stderr pipe buffer while we're
        # still blocked reading stdout deadlocks (communicate() does this
        # for free in _run_blocking; the manual loop here has to do it itself).
        stderr_task = asyncio.create_task(proc.stderr.read())

        last_result: Optional[dict] = None
        assistant_messages: list = []
        try:
            while True:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=self.timeout)
                if not line:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") == "assistant":
                    msg = obj.get("message") or {}
                    assistant_messages.append(msg)
                    content = msg.get("content") or []
                    if content and on_event is not None:
                        detail = _format_codebuddy_thinking_detail(content[-1])
                        if detail:
                            on_event(detail)
                elif obj.get("type") == "result":
                    last_result = obj
        except asyncio.TimeoutError:
            stderr_task.cancel()
            proc.kill()
            await proc.wait()
            return ExecResult(ok=False, error=f"codebuddy timed out after {self.timeout}s",
                              error_kind="transient")
        except asyncio.LimitOverrunError:
            # Defense-in-depth: with limit=10MiB above this should never
            # actually fire, but a single NDJSON line over the StreamReader
            # buffer must still degrade gracefully rather than crash the task.
            stderr_task.cancel()
            proc.kill()
            await proc.wait()
            return ExecResult(ok=False,
                              error="codebuddy produced an oversized stream line (over 10MB)",
                              error_kind="task_error")

        await proc.wait()
        stderr = (await stderr_task).decode(errors="replace")
        credits = _extract_codebuddy_credit(assistant_messages)
        usage = _extract_codebuddy_usage(assistant_messages)
        if last_result is not None:
            return _codebuddy_exec_result(last_result, credits, usage)
        return ExecResult(ok=False,
                          error=(stderr or "codebuddy exited without a result line")[:2000],
                          cost_credits=credits, usage=usage, error_kind="task_error")


def _codebuddy_exec_result(result_obj: dict, credits: float,
                           usage: Optional[dict] = None) -> ExecResult:
    """Classify a codebuddy result object (`messages[-1]` from the
    non-streaming JSON-array response, or the tail `result`-type line of
    stream-json -- both carry the same is_error/result keys) into an
    ExecResult. Shared by the blocking and streaming paths.

    `usage` is carried on failures too: a call that burned tokens and then
    errored still spent them, same reasoning as cost_credits.
    """
    if result_obj.get("is_error"):
        result_text = str(result_obj.get("result") or "")
        kind = ("unavailable"
               if any(m in result_text.lower() for m in _CODEBUDDY_UNAVAILABLE_MARKERS)
               else "task_error")
        return ExecResult(ok=False, error=result_text[:2000],
                          cost_credits=credits, error_kind=kind, usage=usage)
    return ExecResult(ok=True, output=str(result_obj.get("result", "")),
                      cost_credits=credits, usage=usage)


def _format_codebuddy_thinking_detail(block: dict) -> Optional[str]:
    """Same shape family as _format_claude_thinking_detail, but codebuddy's
    `thinking` blocks stream real text (confirmed via a real probe against
    the hy3 model, 2026-07-23) -- show an actual preview instead of a fixed
    placeholder. Falls back to the same placeholder if the text does come
    back empty (defensive -- not every codebuddy model is guaranteed to
    behave like hy3)."""
    kind = block.get("type")
    if kind == "thinking":
        text = (block.get("thinking") or "").strip()
        if not text:
            return "正在思考…"
        return text[:100] + ("…" if len(text) > 100 else "")
    if kind == "tool_use":
        name = block.get("name", "?")
        arg = _first_useful_arg(block.get("input") or {})
        return f"{name}({arg})" if arg else f"{name}(...)"
    if kind == "text":
        text = (block.get("text") or "").strip()
        if not text:
            return None
        return text[:100] + ("…" if len(text) > 100 else "")
    return None


def _codebuddy_raw_usage(messages: list) -> Optional[dict]:
    """The assistant reply's providerData.rawUsage, or None.

    Position varies with num_turns, so this can't be a fixed index.
    Shared by _extract_codebuddy_credit and _extract_codebuddy_usage so
    the two never disagree about which message they read.
    """
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            try:
                raw = msg["providerData"]["rawUsage"]
            except (KeyError, TypeError):
                return None
            return raw if isinstance(raw, dict) else None
    return None


def _extract_codebuddy_credit(messages: list) -> float:
    """Real workbuddy credit spend off providerData.rawUsage.credit.
    Missing data (no assistant message, or a response shape without
    rawUsage) is not an error -- treat as 0.0 rather than raising, so a
    missing credit figure never crashes an otherwise-successful call.

    NB: 0.0 does NOT mean free. A 2026-08-30 real probe against `hy3`
    returned credit=0 because that model is currently on WorkBuddy's
    limited-time-free tier -- see CLAUDE.md §6.12.1. Anything reasoning
    about spend must look at tokens too, not just this number.
    """
    raw = _codebuddy_raw_usage(messages)
    if raw is None:
        return 0.0
    try:
        return float(raw["credit"])
    except (KeyError, TypeError, ValueError):
        return 0.0


def _extract_codebuddy_usage(messages: list) -> Optional[dict]:
    """Token usage for ExecResult.usage, kept in the provider's RAW shape.

    The contract field is documented as "raw token usage from the provider,
    if any", and claude_code/openrouter each store their own provider shape
    too -- normalising codebuddy alone would make it the odd one out.
    A common normalised shape can only be designed once real samples from
    all three are in hand; inventing one now would be guessing (CLAUDE.md
    §6.6's rule). See CLAUDE.md §6.12 for the usage-panel direction.

    Returns None (not {}) when the provider gave nothing -- None means
    "not reported", {} would read as "reported, all zeros".
    """
    raw = _codebuddy_raw_usage(messages)
    return dict(raw) if raw else None


class DshExecutor(Executor):
    """Runs `dsh --profile headless` -- DeepSeek Harness's one-shot agent mode.

    One task = one fresh DSH agent (it drives itself to quiescence with its
    own tools/subagents/skills), prints ONLY the final assistant text to
    stdout, exit 0 = completed / 1 = error. Model selection goes through a
    generated `--patch` overlay (see _dsh_model_patch_text). No streaming:
    on_event is unused in v1 (headless prints nothing until the end), so the
    frontend shows a plain "working" state for dsh agents.

    Cost is NOT reported on stdout in rc.6 -- the DeepSeek spend bills the
    user's own DEEPSEEK_API_KEY directly, so cost_usd stays 0.0. This is a
    known gap (dsh is pay-as-you-go but unmeasured); documented in
    capability_matrix.yaml. Do NOT add "dsh" to router.PAID_API_KINDS until
    this executor can actually report spend, or the budget gate would read a
    permanent 0.0 and silently never fire for it.
    """
    name = "dsh"

    def __init__(self, model: str = "deepseek-v4-flash",
                 provider: str = "deepseek-official",
                 workdir: str = ".", timeout: int = 900,
                 dsh_path: Optional[str] = None, reasoning_effort: str = ''):
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.provider = provider
        self.workdir = workdir
        self.timeout = timeout
        self.dsh_path = dsh_path

    async def run(self, task: Task, context: str,
                  on_event: Optional[Callable[[str], None]] = None) -> ExecResult:
        cmd = _dsh_cmd(self.dsh_path)
        if not cmd:
            return ExecResult(ok=False, error=(
                "`dsh` CLI not found -- install it (npm i -g @deepseek-ai/dsh) "
                "or set dsh_path in capability_matrix.yaml / DSH_PATH env var"),
                error_kind="unavailable")
        prompt = task.prompt if not context else f"{task.prompt}\n\n<context>\n{context}\n</context>"
        patch_path: Optional[str] = None
        proc = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
                f.write(_dsh_model_patch_text(self.provider, self.model, self.reasoning_effort))
                patch_path = f.name
            argv = cmd + ["--profile", "headless", "--patch", patch_path, prompt]
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=self.workdir,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            await _kill_timed_out(proc)
            return ExecResult(ok=False, error=f"dsh timed out after {self.timeout}s",
                              error_kind="transient")
        except FileNotFoundError as exc:
            return _spawn_not_found(exc, "`dsh` CLI not found")
        finally:
            if patch_path is not None and os.path.exists(patch_path):
                os.remove(patch_path)

        raw = stdout.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            err = (stderr.decode(errors="replace") or raw)[:2000]
            kind = "unavailable" if _dsh_unavailable(err) else "task_error"
            return ExecResult(ok=False, error=err, error_kind=kind)
        if not raw:
            return ExecResult(ok=False, error="dsh exited 0 with empty output",
                              error_kind="task_error")
        return ExecResult(ok=True, output=raw)


def build_executor(spec: dict, workdir: str, stream: bool = True, *, api_secret_provider=None) -> Executor:
    kind = spec["kind"]
    if kind == "mock":
        return MockExecutor()
    if kind == 'api':
        from .api_executor import ApiExecutor
        connection = dict(spec.get('connection', {}))
        connection['model'] = spec.get('model', connection.get('model', ''))
        return ApiExecutor(connection, api_secret_provider, stream=stream)
    if kind == "claude_code":
        return ClaudeCodeExecutor(
            model=spec.get("model", "sonnet"),
            allowed_tools=spec.get("allowed_tools", "Read"),
            workdir=workdir,
            stream=stream,
            reasoning_effort=str(spec.get('reasoning_effort') or ''),
            claude_path=spec.get('claude_path'),
        )
    if kind == "codex":
        return CodexExecutor(
            model=str(spec.get("model") or ""),
            workdir=workdir,
            codex_path=spec.get("codex_path"),
            reasoning_effort=str(spec.get('reasoning_effort') or ''),
        )
    if kind == "codebuddy":
        return CodeBuddyExecutor(
            # optional -- unset falls through to WORKBUDDY_NODE_PATH/
            # WORKBUDDY_SCRIPT_PATH env vars or best-effort auto-detect,
            # resolved lazily at .run() time (see _resolve_codebuddy_paths)
            node_path=spec.get("node_path"),
            script_path=spec.get("script_path"),
            model=spec["model"],
            workdir=workdir,
            stream=stream,
        )
    if kind == "openrouter":
        return OpenRouterExecutor(model=spec["model"], stream=stream)
    if kind == "dsh":
        return DshExecutor(
            model=spec.get("model", "deepseek-v4-flash"),
            provider=spec.get("provider", "deepseek-official"),
            workdir=workdir,
            dsh_path=spec.get("dsh_path"),
            reasoning_effort=str(spec.get('reasoning_effort') or ''),
        )
    raise ValueError(f"unknown executor kind: {kind}")
