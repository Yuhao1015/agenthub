"""门卡片与三个开关的前端结构约束（spec §4.1/§4.6/§7.5）。"""
from __future__ import annotations

import os
import re


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src() -> str:
    with open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8") as stream:
        return stream.read()


def test_both_gate_card_kinds_exist():
    src = _src()
    assert 'c.kind==="gate-plan"' in src and 'c.kind==="gate-task"' in src


def test_plan_gate_reuses_parameterised_render_dag():
    match = re.search(r'c\.kind==="gate-plan"\)\{(.*?)\n  \}', _src(), re.S)
    assert match, "找不到 gate-plan 分支"
    assert "renderDag(" in match.group(1), "计划门卡片没复用 renderDag"


def test_three_switches_in_run_bar():
    src = _src()
    for selector in ('id="sw-manager"', 'id="sw-assign"', 'id="sw-gate"'):
        assert selector in src, f"#run-bar 缺少 {selector}"


def test_switches_persist_through_settings_config():
    assert "ui_prefs" in _src(), "三个开关没有走 /settings/config 的 ui_prefs 持久化"


def test_gate_posts_to_gate_endpoint():
    src = _src()
    assert '"/gate"' in src, "没有接 POST /gate"
    assert "gate_token:card.gateToken" in src, "POST /gate 没带本次门实例 token"


def test_frozen_after_resolved():
    src = _src()
    assert "gate_resolved" in src, "没有处理 gate_resolved 事件"
    match = re.search(r'c\.kind==="gate-plan"\)\{(.*?)\n  \}', src, re.S)
    assert match and "frozen" in match.group(1), "计划门卡片没有 frozen 只读状态"
    assert 'card.gateToken===(e.gate_token||"")' in src, \
        "gate_resolved 会误冻结同 gate_id 的历史卡片"


def test_agent_option_text_uses_textcontent():
    match = re.search(r"function renderAgentPicker\((.*?)\n\}\n", _src(), re.S)
    assert match, "找不到 renderAgentPicker"
    assert "textContent" in match.group(1)


def test_openrouter_manager_warning_present():
    src = _src()
    assert "护栏" in src and "openrouter" in src, "缺少 openrouter 类经理的预算护栏警告"


def test_scheduled_runs_always_auto_notice():
    src = _src()
    assert "定时运行" in src and "自动" in src, "缺少定时运行始终自动的说明"


def test_preference_writes_are_serialized_before_work_starts():
    src = _src()
    assert "prefsSave=" in src and "await prefsSave" in src, \
        "设置保存与立即发送目标之间仍有竞态"
    for key in ("manager_agent", "assign_mode", "gate_mode"):
        assert key in src, f"偏好全量快照缺少 {key}"


def test_sending_waits_once_and_can_retry_failed_preferences():
    src = _src()
    assert 'S.busy&&S.mode==="live")||S.sendPending' in src, \
        "等待偏好保存时没有同步防止重复发送"
    assert "S.sendPending=true" in src and "S.sendPending=false" in src, \
        "sendPending 没有覆盖完整发送生命周期"
    assert "if(!prefsOk)" in src and "pending=savePrefs()" in src, \
        "偏好保存失败后再次发送不会真正重试保存"


def test_sending_waits_for_latest_preferences_and_locks_shortcuts():
    src = _src()
    assert "pending!==prefsSave" in src and "await waitForPrefs()" in src, \
        "发送只等了旧 Promise，没有等偏好保存队列稳定"
    # 以前逐个断言 `$("#sw-x").disabled=S.sendPending`，那是钉死写法：
    # 七个控件各写一遍布尔式，加 restoringSessions 那次就漏改过 #mode-btn。
    # 现在断言的是契约——控件在统一登记表里，判据只有一处。
    controls = re.search(r"const DRAFT_CONTROLS=\[(.*?)\];", src)
    assert controls, "草稿区控件必须有一处统一登记"
    for selector in ("#sw-manager", "#sw-assign", "#sw-gate"):
        assert f'"{selector}"' in controls.group(1), \
            f"发送期间仍能修改 {selector}"
    send_text = re.search(r"function sendText\(t\)\{(.*?)\}", src, re.S)
    assert send_text and "setPromptText" in send_text.group(1), \
        "重跑快捷入口会在发送期间覆盖输入框"


def test_sending_locks_prompt_and_choice_submission():
    src = _src()
    controls = re.search(r"const DRAFT_CONTROLS=\[(.*?)\];", src)
    assert controls and '"#prompt"' in controls.group(1), \
        "旧发送等待期间的新输入会被稍后的清空动作吞掉"
    submit = re.search(r"function submitChoices\(card\)\{(.*?)\n\}", src, re.S)
    assert submit and "S.sendPending" in submit.group(1), \
        "选择题提交会在发送期间覆盖输入并冻结卡片"
    assert submit.group(1).index("S.sendPending") < submit.group(1).index("const lines"), \
        "选择题防重入必须发生在任何卡片状态修改前"


def test_all_programmatic_prompt_writes_share_pending_guard():
    src = _src()
    helper = re.search(r"function setPromptText\(text\)\{(.*?)\n\}", src, re.S)
    assert helper and "S.sendPending" in helper.group(1), \
        "程序化写输入框没有统一经过 pending 防护"
    assert '$("#prompt").value="@"+c.task' not in src, \
        "失败卡片追问入口仍绕过统一输入 helper"
    assert 'p.value="@"+S.drawer' not in src, \
        "任务追问入口仍绕过统一输入 helper"
    new_start = src.index('$("#new-btn").onclick=()=>{')
    new_body = src[new_start:src.index("function setPromptText", new_start)]
    assert "S.sendPending" in new_body, \
        "发送期间仍能重置为新对话"
    controls = re.search(r"const DRAFT_CONTROLS=\[(.*?)\];", src)
    assert controls and '"#new-btn"' in controls.group(1), \
        "发送期间新对话按钮没有禁用"


def test_picker_has_sprite_and_fallback_hover_copy():
    src = _src()
    picker = re.search(r"function renderAgentPicker\((.*?)\n\}", src, re.S)
    assert picker, "找不到 agent picker"
    body = picker.group(1)
    assert "paintSprite" in body and "agent.animal" in body, "agent picker 没有动物 sprite"
    assert "fallback" in body and "button.title" in body, "无 blurb 的 agent 没有保底 hover 说明"


def test_openrouter_warning_is_persistent_badge():
    src = _src()
    assert 'id="manager-warning"' in src, "缺少经理预算常驻警告元素"
    assert "warning.hidden" in src, "经理预算警告没有随选择持续显示/隐藏"


def test_selected_agent_is_visible_on_the_plan_node():
    src = _src()
    assert "已指定 " in src and "c.overrides" in src, (
        "选完模型后流程图节点必须显示实际钉选，不能看起来像选择丢了"
    )


def test_reused_dag_gets_shared_svg_layout_class():
    src = _src()
    assert 'box.classList.add("dag-grid")' in src, (
        "renderDag 只参数化 JS 不够；SVG 样式不能继续写死在 #dag"
    )
    assert ".dag-grid svg" in src, "对话卡片里的 SVG 缺少共享定位样式"



def test_draft_lock_has_exactly_one_definition():
    """判据只许有一处，控件只许有一份登记。

    这条是为当初那个真实疏漏写的：加 S.restoringSessions 时，七个控件里
    #mode-btn 被漏掉了。七份重复的布尔式，漏一个不会有任何提示 —— 界面
    只是安静地允许了一次本不该允许的操作。

    所以断言的不是「某个控件写了某个表达式」（那是钉死写法，重构一次就
    会替旧写法站岗），而是「判据没有第二份拷贝」。
    """
    src = _src()
    predicate = re.search(r"function draftLocked\(\)\{(.*?)\n\}", src, re.S)
    assert predicate, "必须有一处集中的草稿锁判据"
    # 判据表达式在 draftLocked 之外不该再出现。先剥注释：注释里引用旧写法
    # 是正当的（本文件的注释就在讲这段历史），因注释误报的测试是坏测试。
    code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    code = re.sub(r"^\s*//[^\n]*", "", code, flags=re.M)
    expr = "S.sendPending||S.restoringSessions"
    assert code.count(expr) == 1, \
        f"判据被复制了 {code.count(expr)} 份；应当只在 draftLocked() 里出现一次"
    assert expr in predicate.group(1)

    # renderAll 里不许有手写的 disabled 布尔式（#send 例外：它多一条 run 条件，
    # 但也必须复用 locked）
    render = re.search(r"\nfunction renderAll\(\)\{(.*?)\n\}\n", src, re.S)
    assert render
    hand = re.findall(r'\$\("#[\w-]+"\)\.disabled=(?!locked)', render.group(1))
    assert not hand, f"renderAll 里仍有 {len(hand)} 处绕过统一判据的 disabled 赋值"


def test_every_draft_control_exists_in_the_page():
    """登记表里写错选择器不会报错，只会静默地少禁用一个控件。"""
    src = _src()
    controls = re.search(r"const DRAFT_CONTROLS=\[(.*?)\];", src).group(1)
    for sel in re.findall(r'"#([\w-]+)"', controls):
        assert f'id="{sel}"' in src, f"DRAFT_CONTROLS 里的 #{sel} 在页面上不存在"

if __name__ == "__main__":
    for fn in sorted(key for key in list(globals()) if key.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll frontend-gate-card tests passed.")
