"""经理澄清选择题：结构化只发生在展示层。

无框架，纯 assert。零 LLM 调用。

Run: python3 tests/test_manager_choices.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.manager import (MAX_QUESTIONS_PER_ROUND, OTHER_OPTION_LABEL,
                         _normalize_questions, _render_questions_text)


def test_new_shape_passes_through():
    data = {"status": "asking", "questions": [
        {"id": "q1", "question": "面向什么用户？",
         "options": [{"label": "内部同事", "detail": "公司内部用"},
                     {"label": "外部客户", "detail": "对外发布"}]}]}
    out = _normalize_questions(data, "")
    assert len(out) == 1, out
    assert out[0]["id"] == "q1" and out[0]["question"] == "面向什么用户？"
    labels = [o["label"] for o in out[0]["options"]]
    assert labels == ["内部同事", "外部客户", OTHER_OPTION_LABEL], labels
    assert out[0]["options"][-1]["other"] is True


def test_old_single_question_shape_still_works():
    out = _normalize_questions({"status": "asking", "question": "抓哪个网站？"}, "")
    assert out == [{"id": "q1", "question": "抓哪个网站？", "options": []}], out


def test_unparseable_text_becomes_one_open_question():
    out = _normalize_questions(None, "预算大概是多少呢？")
    assert out == [{"id": "q1", "question": "预算大概是多少呢？", "options": []}], out


def test_other_option_only_appended_when_real_options_exist():
    out = _normalize_questions({"question": "随便说说"}, "")
    assert out[0]["options"] == [], out


def test_degradation_rule_returns_bare_text():
    qs = [{"id": "q1", "question": "抓哪个网站？", "options": []}]
    assert _render_questions_text(qs) == "抓哪个网站？"


def test_multi_question_text_is_readable_in_a_terminal():
    qs = _normalize_questions({"questions": [
        {"question": "面向谁？", "options": [{"label": "内部"}, {"label": "外部"}]},
        {"question": "要移动端吗？", "options": [{"label": "要"}, {"label": "不要"}]},
    ]}, "")
    text = _render_questions_text(qs)
    assert "1. 面向谁？" in text and "2. 要移动端吗？" in text, text
    assert "A) 内部" in text and "B) 外部" in text, text
    assert OTHER_OPTION_LABEL in text, text


def test_round_is_capped_at_four_questions():
    data = {"questions": [{"question": f"q{i}"} for i in range(9)]}
    out = _normalize_questions(data, "")
    assert len(out) == MAX_QUESTIONS_PER_ROUND == 4, len(out)


def test_malformed_entries_are_dropped_not_crashed():
    data = {"questions": [
        {"question": "好的一题", "options": ["纯字符串选项", {"label": "对象选项"},
                                            {"detail": "没有 label 要丢掉"}]},
        {"no_question_key": 1},
        "整个不是 dict",
    ]}
    out = _normalize_questions(data, "兜底文本")
    assert len(out) == 1, out
    labels = [o["label"] for o in out[0]["options"]]
    assert labels == ["纯字符串选项", "对象选项", OTHER_OPTION_LABEL], labels


def test_all_questions_dropped_falls_back_to_raw_text():
    out = _normalize_questions({"questions": [{"bad": 1}]}, "兜底文本")
    assert out == [{"id": "q1", "question": "兜底文本", "options": []}], out


if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll manager-choices tests passed.")
