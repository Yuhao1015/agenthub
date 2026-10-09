"""dashboard.html 的工作区入口（spec §7.4）：纯前端接线，后端零改动。

锁住工作区入口、现有后端端点、非 macOS 降级，以及按钮重绘后仍可点击。
无框架，纯 assert。零 LLM 调用。

Run: python3 tests/test_dashboard_workdir_entry.py
"""
from __future__ import annotations

import os
import re


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src() -> str:
    with open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8") as f:
        return f.read()


def test_run_bar_exists_and_templates_are_gone():
    src = _src()
    assert 'id="run-bar"' in src, "工作区入口要挂在 #run-bar 上"
    assert "tpl-bar" not in src, "Task 1 应该已经把模板选择器删干净了"


def test_calls_the_existing_endpoints():
    src = _src()
    for endpoint in ("/settings/pick-workdir", "/settings/workdir"):
        assert endpoint in src, f"dashboard 没有接 {endpoint}"


def test_copy_says_only_later_submissions_use_default():
    src = _src()
    assert "后续提交的目标使用此工作区" in src and "已提交目标保留原工作区" in src, (
        "工作区文案必须说明已经提交的目标保留原工作区"
    )
    match = re.search(r"立即生效[^\n]{0,40}工作区|工作区[^\n]{0,40}立即生效", src)
    assert match is None, (
        f"工作区文案不能说『立即生效』：{match.group(0) if match else ''}"
    )


def test_non_macos_fallback_present():
    src = _src()
    assert 'id="wk-path"' in src, "缺少手动粘贴路径的输入框（非 macOS 降级）"


def test_title_uses_dom_property_without_replacing_the_button():
    src = _src()
    match = re.search(
        r"function renderWorkdirChip\(\)\{(?P<body>.*?)\n\}", src, re.S
    )
    assert match, "缺少 renderWorkdirChip()"
    body = match.group("body")
    assert re.search(r"\b(?:chip|c)\.title\s*=", body), (
        "完整路径应通过 DOM title 属性赋值，避免 HTML 属性拼接"
    )
    assert re.search(r"\.outerHTML\s*=", body) is None, (
        "不能用 outerHTML 重建 #wk-chip，否则会丢失已绑定的 onclick"
    )


if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll dashboard-workdir-entry tests passed.")
