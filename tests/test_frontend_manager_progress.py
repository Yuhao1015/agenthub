"""经理思考进度的前端结构约束（2026-08-31）。

后端只给得出推理的**形状**（`claude -p` 的 thinking 块是空的，
2026-08-31 复测确认）。所以前端不能出现任何编造的旁白文案 —— 展示的
每一个字都必须来自 /chat/progress 返回的 detail。

Run: python3 tests/test_frontend_manager_progress.py
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src() -> str:
    with open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8") as f:
        return f.read()


def test_polls_the_progress_endpoint():
    assert "/chat/progress" in _src(), "没有接 GET /chat/progress"


def test_polling_stops_when_the_turn_ends():
    """轮询必须有停止条件，否则一次对话留下一个永久定时器。"""
    src = _src()
    m = re.search(r"function stopManagerProgress\(", src)
    assert m, "缺少停止轮询的入口"
    assert "clearInterval" in src


def test_progress_detail_uses_textContent():
    """detail 来自 LLM 侧（工具名、文本预览），进 DOM 必须走 textContent，
    同选择题卡片的理由（CLAUDE.md §6.9）。"""
    src = _src()
    m = re.search(r"function renderManagerProgress\((.*?)\n\}\n", src, re.S)
    assert m, "找不到 renderManagerProgress"
    body = m.group(1)
    assert "textContent" in body, "进度文本没走 textContent"
    assert not re.search(r"innerHTML\s*\+?=\s*[^;]*\.detail", body), \
        "detail 走了 innerHTML 拼接"


def test_no_fabricated_narration():
    """后端给不出推理内容，前端就绝不能自己编一段像是「经理在想什么」
    的文案。只允许出现来自 detail 的内容 + 中性的进度词。"""
    src = _src()
    m = re.search(r"function renderManagerProgress\((.*?)\n\}\n", src, re.S)
    body = m.group(1)
    for banned in ("正在考虑", "经理认为", "正在权衡", "分析用户"):
        assert banned not in body, f"出现了编造的旁白：{banned}"


def test_elapsed_is_shown():
    """能给的信息里「想了多久」是最有价值的一个（各家 chatbot 的
    Thought for N seconds）。"""
    src = _src()
    m = re.search(r"function renderManagerProgress\((.*?)\n\}\n", src, re.S)
    assert re.search(r"\bts\b", m.group(1)), "没有用到步骤时间戳算耗时"


def test_session_id_exists_before_the_request_is_sent():
    """第一轮的进度是最需要看的，而服务端的 session_id 只随这一轮的**响应**
    回来 —— 那时这轮已经结束。所以前端必须在发请求前自己生成 id，否则
    整个第一轮轮询空转（实测踩到过）。"""
    src = _src()
    assert "function newSessionId(" in src, "没有前端侧的 session id 生成器"
    m = re.search(r"if\(!S\.chatSession\)S\.chatSession=newSessionId\(\);"
                  r"\s*\n\s*startManagerProgress\(\);", src)
    assert m, "必须先定下 session id 再启动轮询"


def test_restored_pending_turn_does_not_call_send_prompt():
    src = _src()
    match = re.search(r"async function pollRestoredManagerTurn\(\)\{(.*?)\n\}", src, re.S)
    assert match
    assert "sendPrompt(" not in match.group(1)


def test_every_poll_goes_through_the_timer_registry():
    """没有裸 setInterval：句柄散在三处正是缺陷的温床。

    这条以前断言的是 `clearInterval(S.restoredSessionTimer)` 这种写法，
    等于把实现钉死；重构一次它就会替旧写法站岗，而不是保护行为。
    """
    src = _src()
    raw = [m for m in re.findall(r"[^.\w](setInterval|clearInterval)\(", src)]
    registry = re.search(r"const Timers=\{(.*?)\n\};", src, re.S)
    assert registry, "必须有一处集中的定时器登记处"
    inside = len(re.findall(r"(setInterval|clearInterval)\(", registry.group(1)))
    assert len(raw) == inside, \
        f"注册表之外还有 {len(raw) - inside} 处裸定时器调用"


def test_starting_a_poll_twice_cannot_leak_the_first_one():
    src = _src()
    start = re.search(r"start\(name,fn,ms\)\{([^\n]*)", src)
    assert start, "Timers.start 必须存在"
    assert start.group(1).index("this.stop(name)") < start.group(1).index("setInterval"), \
        "start 必须先停旧的，否则重复启动会漏掉一个句柄"


def test_leaving_and_returning_are_symmetric():
    """只停不装正是那个把界面锁死的缺陷。

    旧版本只断言 pagehide 停了哪几条 —— 于是「谁负责装回来」这个真正的
    契约无人看守，bfcache 恢复后 S.sendPending 永远清不掉。
    """
    src = _src()
    hide = re.search(r'window\.addEventListener\("pagehide",([^\n]+)', src)
    show = re.search(r'window\.addEventListener\("pageshow",(.*?)\}\);', src, re.S)
    assert hide and show, "离开与返回都必须有人管"
    assert len(re.findall(r'addEventListener\("pagehide"', src)) == 1
    assert len(re.findall(r'addEventListener\("pageshow"', src)) == 1
    # 停：一次停光，不逐条点名（逐条点名就会漏）
    assert "stopAll()" in hide.group(1), "离开时应当一次停光，而不是逐条点名"
    # 装回来：live 无状态直接重启，session/progress 的真相在服务端
    assert "persisted" in show.group(1), "只有 bfcache 恢复才需要重新推导"
    assert "startLive()" in show.group(1)
    assert "restoreManagerSessions()" in show.group(1)


def test_new_goal_and_started_response_stop_manager_timers():
    src = _src()
    new_goal = re.search(r'\$\("#new-btn"\)\.onclick=async\(\)=>\{(.*?)\n\};', src, re.S)
    assert new_goal
    assert "stopRestoredManagerPolling()" in new_goal.group(1)
    assert "stopManagerProgress()" in new_goal.group(1)
    send = re.search(r"async function sendPrompt\(\)\{(.*?)\n\}\n", src, re.S).group(1)
    started = send[send.index('data.status==="started"') :]
    assert "stopRestoredManagerPolling()" in started
    assert "stopManagerProgress()" in started


def test_disconnect_does_not_abandon_an_active_draft_for_demo():
    src = _src()
    branch = re.search(r"function onLiveDown\(\)\{(.*?)\n\}", src, re.S)
    assert branch
    assert re.search(r"!S\.sessions\.length\s*&&\s*!S\.chatSession", branch.group(1))


if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll frontend manager-progress tests passed.")
