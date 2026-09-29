"""取消是前端终态：后续 run_finished 只负责收尾，不能改回 done。

Engine 为了让 serve.py 看门狗区分“正常取消”和崩溃，会在
run_cancelled 后继续发 run_finished。前端必须显式保留 cancelled。

Run: python3 tests/test_frontend_cancelled_terminal.py
"""
from __future__ import annotations

import os
import re


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_finished_branch() -> str:
    with open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8") as f:
        src = f.read()
    match = re.search(
        r'if\(e\.type==="run_finished"\)\{(.*?)\n    \}\n  \}\n  S\.busy=',
        src,
        re.S,
    )
    assert match, "找不到 ingest() 的 run_finished 分支"
    return match.group(1)


def test_run_finished_preserves_cancelled_terminal_state():
    branch = _run_finished_branch()
    assert 'sess.status==="cancelled"' in branch, \
        "run_finished 没有识别先到达的 run_cancelled"
    assert "if(!wasCancelled)" in branch, \
        "run_finished 仍可能把 cancelled 覆盖成 done"


def test_cancelled_run_does_not_append_success_message():
    branch = _run_finished_branch()
    assert "if(!wasCancelled){" in branch and "sess.chat.push" in branch, \
        "成功消息必须只在非取消运行里追加"


if __name__ == "__main__":
    for name in sorted(k for k in globals() if k.startswith("test_")):
        print(f"== {name} ==")
        globals()[name]()
    print("\nAll frontend-cancelled-terminal tests passed.")
