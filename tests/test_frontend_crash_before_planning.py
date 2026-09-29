"""引擎在 plan_created 之前崩溃，必须让用户看见（2026-08-31）。

真实故障：用户看到经理说「收到，马上安排开工」，然后**永远的沉默**，
没有任何提示。查真实数据发现 run 状态是 crashed、事件流里只有一行：

    {"type":"run_crashed","error":"engine exited with code 2 before run_finished"}

后端行为是对的（看门狗补发了事件、index 记了 crashed）。问题在前端：
run_crashed 的处理写在「必须已有会话」的分支里，而会话只由 plan_created
创建 —— 引擎在拆解任务**之前**就死掉时（argv 错误、找不到 main.py、
import 期异常，恰恰是最常见的崩溃形态），S.sessions 是空的，事件被
`if(!sess)return;` 静默丢弃。

这是最坏的一类 bug：系统知道出了什么事，也写进了磁盘，但就是不告诉用户。

Run: python3 tests/test_frontend_crash_before_planning.py
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src() -> str:
    with open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8") as f:
        return f.read()


def _no_session_branch(src: str) -> str:
    m = re.search(r"const sess=S\.sessions\[S\.sessions\.length-1\];\s*\n?\s*if\(!sess\)\{(.*?)\n    \}",
                  src, re.S)
    assert m, "找不到无会话时的兜底分支（还是裸 return？）"
    return m.group(1)


def test_no_bare_return_that_swallows_terminal_events():
    src = _src()
    assert "if(!sess)return;" not in src, \
        "裸 return 会把 plan_created 之前的 run_crashed 静默丢掉"


def test_crash_before_planning_reaches_the_user():
    body = _no_session_branch(_src())
    assert "run_crashed" in body, "无会话时没有处理 run_crashed"
    assert "preChat" in body, \
        "此刻还没有会话对象，消息必须进 S.preChat —— 那是用户正在看的地方"


def test_cancel_before_planning_also_reaches_the_user():
    """同一个洞：run 起来了但还没拆解就被取消，也会掉进去。"""
    body = _no_session_branch(_src())
    assert "run_cancelled" in body


def test_the_message_says_what_actually_happened():
    """不能只说「出错了」。真实错误串（argparse 退出码之类）要带出来，
    否则用户和以后的我都无从查起。"""
    body = _no_session_branch(_src())
    assert "e.error" in body, "崩溃消息没有带上后端给的 error 字段"


if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll crash-before-planning tests passed.")
