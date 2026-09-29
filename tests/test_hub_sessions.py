"""hub/sessions.py —— 不启服务器、不碰 serve 的任何全局。

这是 2026-09-05 那次抽取的兑现点：会话的三层 schema 校验以前只能透过
serve.py 测（要先 _configure() 铺 31 个模块级路径、存档还原一堆全局）。
Task 1 的评审在这段校验里找出过四个真缺陷，正是这种代码不该被 31 个
全局挡在外面。

本文件**只 import hub.sessions**，一行 serve 都没有。

Run: python3 tests/test_hub_sessions.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub import sessions   # noqa: E402  —— 刻意只导这一个


def _valid(sid: str = "a1b2c3d4e5f6") -> dict:
    return sessions.new_manager_session(sid, now=1000.0)


def test_module_does_not_need_serve():
    """抽取的意义就在这一条：这个模块不该把 serve 拖进来。"""
    assert "serve" not in sys.modules, \
        "hub.sessions 不应该（直接或间接）导入 serve"
    assert sessions.session_path("", "a1b2c3d4e5f6") == "", \
        "没有数据目录时返回空串，而不是去读某个全局"


def test_session_id_is_validated_before_any_path_join():
    for bad in ("../../etc/passwd", "A1B2C3D4E5F6", "a1b2c3d4e5f", "", None, 12):
        try:
            sessions.session_path("/tmp/x", bad)
            raise AssertionError(f"{bad!r} 应当被拒绝")
        except ValueError:
            pass
    assert sessions.session_path("/tmp/x", "a1b2c3d4e5f6") == "/tmp/x/a1b2c3d4e5f6.json"


def test_schema_is_strict_about_unknown_keys():
    """多一个键就拒——否则别的版本写的字段会不经校验混进下一次保存。"""
    data = sessions.disk_dict(_valid())
    data["surprise"] = 1
    try:
        sessions.validate_session_data(data)
        raise AssertionError("未知字段必须被拒绝")
    except sessions.SessionDataError as exc:
        assert "invalid schema" in str(exc)


def test_semantic_consistency_not_just_types():
    base = _valid()
    # 类型全对，但语义自相矛盾的三种
    cases = [
        ({"updated_at": 999.0}, "inconsistent state"),          # 早于 created_at
        ({"awaiting_confirmation": True}, "inconsistent state"), # 等确认却没方案
        ({"status": "started", "run_id": "0a1b2c3d"}, "inconsistent state"),  # 已开工却没方案
    ]
    for patch, expect in cases:
        data = sessions.disk_dict(base)
        data.update(patch)
        try:
            sessions.validate_session_data(data)
            raise AssertionError(f"{patch} 应当被拒绝")
        except sessions.SessionDataError as exc:
            assert expect in str(exc), (patch, str(exc))


def test_active_and_started_disagree_about_run_id():
    data = sessions.disk_dict(_valid())
    data["run_id"] = "0a1b2c3d"          # active 不该带 run_id
    try:
        sessions.validate_session_data(data)
        raise AssertionError("active + run_id 必须被拒绝")
    except sessions.SessionDataError:
        pass


def test_bool_is_not_a_valid_number():
    """True 在 Python 里是 int 的子类，不挡住的话 cost_usd=True 能过。"""
    assert sessions.is_number(0) and sessions.is_number(1.5)
    assert not sessions.is_number(True)
    assert not sessions.is_number(-1)
    assert not sessions.is_number(float("nan"))
    assert not sessions.is_number(float("inf"))


def test_history_ui_fields_are_all_or_nothing():
    base = _valid()
    base["history"] = [{"role": "manager", "text": "问", "ts": 1000.0,
                        "questions": [], "answered": False}]   # 缺 answers
    try:
        sessions.validate_session_data(sessions.disk_dict(base))
        raise AssertionError("半套 UI 字段必须被拒绝")
    except sessions.SessionDataError:
        pass


def test_only_manager_turns_carry_questions():
    base = _valid()
    base["history"] = [{"role": "user", "text": "答", "ts": 1000.0,
                        "questions": [], "answered": True, "answers": {}}]
    try:
        sessions.validate_session_data(sessions.disk_dict(base))
        raise AssertionError("user 轮不该带选择题元数据")
    except sessions.SessionDataError as exc:
        assert "history metadata" in str(exc)


def test_round_trip_through_disk_keeps_every_field():
    with tempfile.TemporaryDirectory() as td:
        sess = _valid()
        sess["history"] = [
            {"role": "user", "text": "做报表", "ts": 1000.0},
            {"role": "manager", "text": "给谁用？", "ts": 1001.0,
             "questions": [{"id": "q1", "question": "给谁用？",
                            "options": [{"label": "财务", "detail": "", "other": False}]}],
             "answered": True, "answers": {"q1": "财务"}},
        ]
        sessions.write_snapshot(td, sess)
        back = sessions.load_disk(td, sess["session_id"])
        assert back == sessions.disk_dict(sess)
        assert os.stat(sessions.session_path(td, sess["session_id"])).st_mode & 0o777 == 0o600


def test_unreadable_file_raises_typed_error_and_is_left_alone():
    with tempfile.TemporaryDirectory() as td:
        sid = "a1b2c3d4e5f6"
        path = sessions.session_path(td, sid)
        os.makedirs(td, exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"{not json")
        try:
            sessions.load_disk(td, sid)
            raise AssertionError("损坏文件必须抛 SessionDataError")
        except sessions.SessionDataError as exc:
            assert "unreadable" in str(exc)
        with open(path, "rb") as f:
            assert f.read() == b"{not json", "读失败不该覆盖证据"


def test_invalid_utf8_is_a_typed_error_not_a_crash():
    with tempfile.TemporaryDirectory() as td:
        sid = "a1b2c3d4e5f6"
        with open(sessions.session_path(td, sid), "wb") as f:
            f.write(b"\xff\xfe not utf8")
        try:
            sessions.load_disk(td, sid)
            raise AssertionError("非法 UTF-8 必须抛 SessionDataError")
        except sessions.SessionDataError:
            pass


def test_filename_and_inner_id_must_agree():
    with tempfile.TemporaryDirectory() as td:
        sess = _valid("a1b2c3d4e5f6")
        sessions.write_snapshot(td, sess)
        other = sessions.session_path(td, "ffffffffffff")
        os.rename(sessions.session_path(td, "a1b2c3d4e5f6"), other)
        try:
            sessions.load_disk(td, "ffffffffffff")
            raise AssertionError("文件名与内部 id 不符必须被拒绝")
        except sessions.SessionDataError as exc:
            assert "does not match its file" in str(exc)


def test_stage_deep_copies_durable_fields_but_keeps_runtime_objects():
    sess = _valid()
    sess["history"] = [{"role": "user", "text": "x", "ts": 1000.0}]
    sentinel = object()
    sess["router"] = sentinel            # 运行时对象，不可序列化
    staged = sessions.stage(sess)
    staged["history"].append({"role": "user", "text": "y", "ts": 1001.0})
    assert len(sess["history"]) == 1, "暂存必须深拷贝，不能改到原件"
    assert staged["router"] is sentinel, "运行时对象按引用带过去"


def test_public_snapshot_never_leaks_the_router():
    sess = _valid()
    sess["router"] = object()
    out = sessions.public(sess, pending=True)
    assert "router" not in out
    assert out["pending"] is True
    json.dumps(out, ensure_ascii=False, allow_nan=False)   # 必须可序列化


def test_write_is_atomic_no_partial_file_left_behind():
    with tempfile.TemporaryDirectory() as td:
        sess = _valid()
        sessions.write_snapshot(td, sess)
        assert [f for f in os.listdir(td) if ".tmp" in f] == [], "不留临时文件"


if __name__ == "__main__":
    for name in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {name} ==")
        globals()[name]()
    print("\nAll hub/sessions tests passed.")
