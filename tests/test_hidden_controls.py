"""HTML hidden 属性不能被组件的 display 样式覆盖。"""
from __future__ import annotations

import os
import re


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_global_hidden_rule_wins_over_component_display():
    with open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8") as stream:
        source = stream.read()
    assert re.search(r"\[hidden\]\s*\{[^}]*display\s*:\s*none\s*!important", source), (
        ".btn{display:flex} 会覆盖浏览器默认 [hidden]；需要全局强制隐藏规则"
    )


if __name__ == "__main__":
    test_global_hidden_rule_wins_over_component_display()
    print("All hidden-controls tests passed.")
