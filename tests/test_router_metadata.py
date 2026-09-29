"""Router 元数据访问器（spec §4.3/§4.4）：计划门要给前端一份「本机现在
真能用的 agent」清单，这些数据 Router 手上都有，只是没有对外读法。

blurb 是**可选**字段（BYO 原则：用户自己写自己机器上的情况），没写就
返回空串，由调用方拼保底文案。

无框架，纯 assert。零 LLM 调用。

Run: python3 tests/test_router_metadata.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.router import Router

MATRIX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "hub", "capability_matrix.yaml")


def _router():
    return Router(MATRIX, workdir=".", mock_all=True)


def test_all_agents_lists_every_matrix_agent():
    r = _router()
    names = r.all_agents()
    for expected in ("cc-sonnet", "cc-opus", "codex-local", "cc-kimi", "or-deepseek",
                     "dsh-pro", "mock"):
        assert expected in names, f"{expected} 不在 all_agents() 里"


def test_kind_reports_real_kind_not_mock():
    """mock_all=True 时执行器换成 mock，但 kind() 必须报矩阵里声明的
    真实 kind —— 前端的额度徽章靠它，报成 mock 会让徽章全错。"""
    r = _router()
    assert r.kind("cc-sonnet") == "claude_code", r.kind("cc-sonnet")
    assert r.kind("cc-kimi") == "codebuddy"
    assert r.kind("codex-local") == "codex"
    assert r.kind("or-deepseek") == "openrouter"
    assert r.kind("dsh-pro") == "dsh"
    assert r.kind("不存在的") == ""


def test_tier_of_returns_none_for_unwired_agents():
    """不是每个 agent 都在某个 tier 里（or-nemotron、dsh-flash 注册但
    未接线）。没有就是 None，不要编一个。"""
    r = _router()
    assert r.tier_of("cc-sonnet") == "mid"
    assert r.tier_of("cc-hy3") == "economy"
    assert r.tier_of("cc-opus") == "strong"
    assert r.tier_of("or-nemotron") is None
    assert r.tier_of("codex-local") is None


def test_specialty_passthrough():
    r = _router()
    assert r.specialty("cc-kimi") == "frontend"
    assert r.specialty("cc-sonnet") is None


def test_blurb_is_optional_and_defaults_to_empty():
    r = _router()
    assert isinstance(r.blurb("cc-sonnet"), str)
    assert r.blurb("不存在的") == ""


def test_circuit_open_reflects_open_circuit():
    r = _router()
    assert r.circuit_open() == set()
    r.open_circuit("cc-opus")
    assert r.circuit_open() == {"cc-opus"}
    # 返回副本，外部改动不能污染内部状态
    r.circuit_open().add("cc-sonnet")
    assert r.circuit_open() == {"cc-opus"}


if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll router-metadata tests passed.")
