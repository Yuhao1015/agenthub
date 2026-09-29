"""选择题卡片的前端结构约束（spec §5.7）。

无框架，纯 assert。零 LLM 调用。

Run: python3 tests/test_frontend_choices.py
"""
from __future__ import annotations

import os
import re


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src() -> str:
    with open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8") as f:
        return f.read()


def _choices_branch(src: str) -> str:
    match = re.search(r'c\.kind==="choices"\)\{(.*?)\n  \}else', src, re.S)
    assert match, "cardEl 里找不到 choices 分支"
    return match.group(1)


def test_choices_card_kind_exists():
    assert 'c.kind==="choices"' in _src(), "cardEl 没有 choices 分支"


def test_answers_live_in_state_not_dom():
    body = _choices_branch(_src())
    assert re.search(r"c\.answers\s*(\[|=)", body), (
        "选项点击没有把答案写回 c.answers —— renderChat 全量重建会抹掉 DOM 状态"
    )


def test_option_text_uses_textcontent_not_innerhtml():
    body = _choices_branch(_src())
    assert "textContent" in body, "选项文本没走 textContent"
    bad = re.search(r"innerHTML\s*[+]?=[^;]*\bo\.label\b", body)
    assert bad is None, f"选项 label 走了 innerHTML 拼接：{bad.group(0) if bad else ''}"
    bad2 = re.search(r"innerHTML\s*[+]?=[^;]*\bq\.question\b", body)
    assert bad2 is None, f"题干走了 innerHTML 拼接：{bad2.group(0) if bad2 else ''}"


def test_frozen_flag_present():
    assert "frozen" in _choices_branch(_src()), "卡片没有 frozen 只读状态"


def test_send_prompt_freezes_open_choices_card():
    src = _src()
    match = re.search(r"async function sendPrompt\(\)\{(.*?)\n\}\n", src, re.S)
    assert match, "找不到 sendPrompt"
    assert "freezeOpenChoices" in match.group(1), (
        "sendPrompt 没有冻结开着的选择题卡片 —— 会留下点了没反应的僵尸卡片"
    )


def test_questions_field_consumed_from_chat_response():
    assert "data.questions" in _src(), "sendPrompt 没有消费 /chat 响应的 questions 字段"


def test_other_choice_requires_the_users_detail():
    src = _src()
    match = re.search(r"function submitChoices\(card\)\{(.*?)\n\}", src, re.S)
    assert match, "找不到 submitChoices"
    body = match.group(1)
    assert "chosen&&chosen.other&&!extra" in body, (
        "选择『其他』但没写补充时，不能把占位标签当成真实答案发出去"
    )


def test_choice_answers_are_sent_as_additive_chat_metadata():
    src = _src()
    submit = re.search(r"function submitChoices\(card\)\{(.*?)\n\}", src, re.S).group(1)
    send = re.search(r"async function sendPrompt\(\)\{(.*?)\n\}\n", src, re.S).group(1)
    assert "pendingChoiceAnswers" in submit
    assert "choice_answers" in send


def test_choice_answer_transport_keeps_other_free_text_and_normalizes_pairs():
    src = _src()
    assert "function normalizeChoiceAnswers(" in src
    normalize = re.search(r"function normalizeChoiceAnswers\(.*?\)\{(.*?)\n\}", src, re.S)
    assert normalize and "typeof" in normalize.group(1)
    submit = re.search(r"function submitChoices\(card\)\{(.*?)\n\}", src, re.S).group(1)
    assert "normalizeChoiceAnswers(card.answers)" in submit
    assert '"__other__"+question.id' in submit


def test_choice_metadata_is_consumed_only_when_chat_request_is_built():
    send = re.search(r"async function sendPrompt\(\)\{(.*?)\n\}\n", _src(), re.S).group(1)
    capture = send.index("const choiceAnswers=S.pendingChoiceAnswers")
    payload = send.index("choice_answers:choiceAnswers||{}")
    clear = send.index("S.pendingChoiceAnswers=null")
    chat_fetch = send.index('fetch("/chat"')
    assert capture < payload < clear < chat_fetch


if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll frontend-choices tests passed.")
