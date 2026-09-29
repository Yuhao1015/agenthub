"""renderDag 参数化：仪表区和对话卡片复用同一个渲染器。

内联 JS 无独立测试基建，因此锁住签名、依赖注入和原有连线机制；
视觉与点击行为另做真实浏览器验证。

Run: python3 tests/test_render_dag_params.py
"""
from __future__ import annotations

import os
import re


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src() -> str:
    with open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8") as stream:
        return stream.read()


def _body() -> tuple[str, str]:
    match = re.search(r"function renderDag\((.*?)\)\{(.*?)\n\}\n", _src(), re.S)
    assert match, "找不到 renderDag"
    return match.group(1), match.group(2)


def test_signature_takes_box_tasks_opts():
    signature, _ = _body()
    args = [arg.strip() for arg in signature.split(",")]
    assert args[:2] == ["box", "tasks"], (
        f"签名应为 (box, tasks, opts)，实为 ({signature})"
    )
    assert len(args) >= 3, signature


def test_no_hardcoded_dag_selector_or_view_tasks_inside():
    _, body = _body()
    assert '$("#dag")' not in body, "renderDag 内部还写死着 #dag"
    assert "view.tasks" not in body, "renderDag 内部还写死着 view.tasks"
    assert "openDrawer" not in body, "点击回调还写死着 openDrawer"


def test_click_goes_through_opts():
    _, body = _body()
    assert re.search(r"opts\s*(&&|\.|\[)", body), "没有从 opts 取回调"


def test_layout_and_svg_machinery_preserved():
    _, body = _body()
    for token in ("gridTemplateColumns", "requestAnimationFrame",
                  "getBoundingClientRect", "createElementNS"):
        assert token in body, f"参数化过程中弄丢了 {token}"


def test_instrument_pane_call_site_updated():
    assert re.search(r'renderDag\(\s*\$\("#dag"\)\s*,', _src()), (
        "仪表区调用点没跟着改成新签名"
    )


def test_intended_agent_rendered_and_labelled_as_prediction():
    src = _src()
    assert "t.intended" in src, "renderDag 没有渲染 intended agent"
    assert "预计 " in src, "没有标注这只是预计，不是既成事实"
    assert "intended_agent" in src, "plan_created 的 intended_agent 字段没被消费"


def test_layout_lives_in_the_shared_class_not_duplicated():
    """网格布局只能有一份定义。

    renderDag 会给传进来的任何 box 加 .dag-grid，所以布局属性放在这个类
    上，仪表区(#dag)和对话卡片就自动一致。分成「#dag 的规则 + 卡片的
    内联 cssText」两份的话，以后改 gap 或列宽只会改到一处，两个视图
    悄悄分叉 —— 而且不会有测试红。
    """
    src = _src()
    m = re.search(r"\.dag-grid\{([^}]*)\}", src)
    assert m, "缺少 .dag-grid 的布局规则"
    rule = m.group(1)
    for prop in ("display:grid", "position:relative"):
        assert prop in rule.replace(" ", ""), f".dag-grid 少了 {prop}: {rule}"
    # 卡片不该再自己声明这两条
    card = re.search(r'box\.style\.cssText="([^"]*)"', src)
    assert card, "找不到门卡片容器的内联样式"
    inline = card.group(1).replace(" ", "")
    assert "display:grid" not in inline, f"卡片重复声明了 display:grid: {inline}"
    assert "position:relative" not in inline, f"卡片重复声明了 position:relative: {inline}"


if __name__ == "__main__":
    for fn in sorted(key for key in list(globals()) if key.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll render-dag-params tests passed.")
