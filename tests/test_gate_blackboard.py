"""黑板写权限：user 可写 run/gate_reply/*（spec §3.2，受保护接口改动 #1）。

放宽必须是**窄**的：只开这一个前缀，user 写 run/ 下的任何别的键仍然
被拒，写别人任务的命名空间仍然被拒。

无框架，纯 assert。零 LLM 调用。

Run: python3 tests/test_gate_blackboard.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.blackboard import Blackboard, PermissionError_


def _board(tmp):
    return Blackboard(os.path.join(tmp, "blackboard.jsonl"), on_write=lambda e: None)


def test_user_can_write_gate_reply():
    tmp = tempfile.mkdtemp(prefix="ah_gate_bb_ok_")
    try:
        b = _board(tmp)
        b.put("run/gate_reply/plan", {"decision": "approve"}, author="user")
        assert b.get("run/gate_reply/plan") == {"decision": "approve"}
        b.put("run/gate_reply/task:t3", {"decision": "skip"}, author="user")
        assert b.get("run/gate_reply/task:t3") == {"decision": "skip"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_widening_is_narrow_user_still_cannot_write_other_run_keys():
    """只开 run/gate_reply/ 一个前缀，不是把整个 run/ 交给 user。"""
    tmp = tempfile.mkdtemp(prefix="ah_gate_bb_no_")
    try:
        b = _board(tmp)
        for key in ("run/workdir", "run/gate/plan", "goal/statement",
                    "run/gate_replyX/plan"):
            try:
                b.put(key, "x", author="user")
                raise AssertionError(f"user 不该能写 {key}")
            except PermissionError_:
                pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_agent_authors_still_cannot_write_gate_reply():
    """门的回复只有 user 能写；agent 自批自放是绝对不行的。"""
    tmp = tempfile.mkdtemp(prefix="ah_gate_bb_agent_")
    try:
        b = _board(tmp)
        try:
            b.put("run/gate_reply/plan", {"decision": "approve"},
                  author="cc-sonnet", task_id="t1")
            raise AssertionError("agent 不该能写 gate reply —— 那等于自己给自己放行")
        except PermissionError_:
            pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_existing_rules_unchanged():
    """回归：既有的三条规则一条都不能被这次放宽影响。"""
    tmp = tempfile.mkdtemp(prefix="ah_gate_bb_reg_")
    try:
        b = _board(tmp)
        b.put("notes/0", "x", author="cc-sonnet", task_id="t1")
        b.put("tasks/t1/output", "x", author="cc-sonnet", task_id="t1")
        b.put("review/t1/verdict", "ok", author="cc-sonnet", task_id="t1")
        b.put("goal/statement", "g", author="system")
        try:
            b.put("tasks/t2/output", "x", author="cc-sonnet", task_id="t1")
            raise AssertionError("越权写别人任务应该被拒")
        except PermissionError_:
            pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll gate-blackboard tests passed.")
