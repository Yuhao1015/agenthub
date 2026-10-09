"""GUI 启动时的 CLI 查找（2026-08-31）。

实测确认的问题：macOS 上 GUI 启动的进程（双击的 .app / 启动器）从
launchd 继承的 PATH 只有 /usr/bin:/bin:/usr/sbin:/sbin，且**不读用户的
shell rc**。而本项目驱动的每个 CLI 都装在这之外：

    终端 PATH 里:  /usr/local/bin/claude
    最小 PATH 里:  未找到

结果是 shutil.which() 全部落空，启动预检把每个 agent 都熔断，症状是
「终端里跑得好好的，双击图标就全挂」——而且看不出原因。

无框架，纯 assert。零 LLM 调用。

Run: python3 tests/test_gui_path_resolution.py
"""
from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hub.executors as E


def _fake_bin(dirpath: str, name: str) -> str:
    """造一个可执行的假 CLI。"""
    if sys.platform == 'win32' and not name.endswith('.exe'):
        name += '.exe'
    os.makedirs(dirpath, exist_ok=True)
    p = os.path.join(dirpath, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\necho ok\n")
    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


class _Env:
    """临时改 PATH / 环境变量，退出时精确还原。"""

    def __init__(self, **kw):
        self.kw = kw
        self.saved = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *a):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_which_delegates_when_path_already_has_it():
    tmp = tempfile.mkdtemp(prefix="ah_path_ok_")
    try:
        real = _fake_bin(tmp, "ah-probe-cli")
        with _Env(PATH=tmp, AGENT_HUB_EXTRA_PATH=None):
            found = E._which("ah-probe-cli")
            assert found and os.path.normcase(found) == os.path.normcase(real), (found, real)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_which_finds_binary_outside_a_minimal_path():
    """这就是 .app 的处境：PATH 里没有，但装在众所周知的目录里。"""
    tmp = tempfile.mkdtemp(prefix="ah_path_extra_")
    try:
        binp = _fake_bin(os.path.join(tmp, "bin"), "ah-probe-cli")
        empty = os.path.join(tmp, "nothing-here")
        os.makedirs(empty, exist_ok=True)
        with _Env(PATH=empty, AGENT_HUB_EXTRA_PATH=os.path.join(tmp, "bin")):
            assert E._which("ah-probe-cli") == binp, \
                "PATH 里没有时必须去补充目录里找"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_which_returns_none_when_genuinely_absent():
    """找不到就是找不到，不能瞎编一个路径回去。"""
    tmp = tempfile.mkdtemp(prefix="ah_path_none_")
    try:
        with _Env(PATH=tmp, AGENT_HUB_EXTRA_PATH=tmp):
            assert E._which("ah-definitely-not-installed") is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_extra_path_accepts_multiple_dirs():
    tmp = tempfile.mkdtemp(prefix="ah_path_multi_")
    try:
        b = _fake_bin(os.path.join(tmp, "b"), "ah-probe-cli")
        os.makedirs(os.path.join(tmp, "a"), exist_ok=True)
        extra = os.pathsep.join([os.path.join(tmp, "a"), os.path.join(tmp, "b")])
        with _Env(PATH=os.path.join(tmp, "empty"), AGENT_HUB_EXTRA_PATH=extra):
            assert E._which("ah-probe-cli") == b
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_claude_honours_an_explicit_env_override():
    """claude 之前是三个 CLI 里唯一没有环境变量兜底的（dsh 有 DSH_PATH、
    codex 有 CODEX_PATH）。补齐，BYO 三级解析对齐。"""
    tmp = tempfile.mkdtemp(prefix="ah_path_claude_")
    try:
        fake = _fake_bin(tmp, "claude-somewhere-else")
        with _Env(CLAUDE_PATH=fake):
            assert E._claude_cmd() == [fake]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_claude_falls_back_to_bare_name_when_nothing_resolves():
    """回归：解析不出来时保持既有行为（返回 ["claude"]，让子进程自己
    报错），不要变成崩溃或 None。

    必须连内置目录表一起清空 —— 否则这台机器上 /usr/local/bin 里就有
    真的 claude，测试根本走不到「找不到」那条路径。
    """
    tmp = tempfile.mkdtemp(prefix="ah_path_bare_")
    saved = E._EXTRA_BIN_DIRS
    E._EXTRA_BIN_DIRS = ()
    try:
        with _Env(PATH=tmp, AGENT_HUB_EXTRA_PATH=None, CLAUDE_PATH=None):
            assert E._claude_cmd() == ["claude"]
    finally:
        E._EXTRA_BIN_DIRS = saved
        shutil.rmtree(tmp, ignore_errors=True)


def test_all_three_clis_share_the_same_lookup():
    """结构断言：三个 CLI 必须走同一个 _which()。

    分开写的话，以后给其中一个加了新的查找位置、另外两个不会跟着变 ——
    而症状是「claude 找得到、dsh 找不到」这种极难定位的偏科故障。
    """
    import re
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "hub", "executors.py"), encoding="utf-8").read()
    for fn in ("_claude_cmd", "_dsh_cmd", "_codex_cmd"):
        body = re.search(rf"def {fn}\(.*?\n(?=\n\ndef |\n\nclass )", src, re.S)
        assert body, f"找不到 {fn}"
        assert "_which(" in body.group(0), f"{fn} 没走共享的 _which()"
        assert "shutil.which(" not in body.group(0), \
            f"{fn} 还在直接调 shutil.which()，GUI 启动时会找不到"


if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll gui-path-resolution tests passed.")
