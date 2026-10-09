"""hub/run_changes.py -- a run's change record on its own blackboard
(docs/decisions/change-safety-net.md §3.2-§5.2).

Real git in temp dirs (skipped honestly without git) and a real Blackboard.
No framework, plain asserts, zero LLM calls.

Run: python tests/test_run_changes.py
"""
from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub import checkpoints, run_changes
from hub.blackboard import Blackboard

RID = "abcd1234"


def needs_git(fn):
    def wrapper():
        if shutil.which("git") is None:
            print("  (skipped: no git on PATH)")
            return
        fn()
    return wrapper


def _rmtree(path):
    """git marks its object files read-only, and Windows refuses to delete those."""
    for root, _dirs, files in os.walk(path):
        for name in files:
            os.chmod(os.path.join(root, name), stat.S_IREAD | stat.S_IWRITE)
    shutil.rmtree(path)


def _setup():
    tmp = tempfile.mkdtemp(prefix="ah_rc_")
    wd = os.path.join(tmp, "wd")
    os.makedirs(wd)
    rt = os.path.join(tmp, "rt")
    board = Blackboard(os.path.join(tmp, "board.jsonl"), on_write=None)
    return tmp, wd, rt, board, checkpoints.store_path(rt, wd)


def _write(root, rel, data: bytes):
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(data)


def _read(root, rel) -> bytes:
    with open(os.path.join(root, rel), "rb") as f:
        return f.read()


def test_begin_without_a_store_records_why():
    tmp, wd, rt, board, _store = _setup()
    try:
        seg, status = run_changes.begin(board, "", wd, RID, "run", "engine")
        assert seg is None and status == {"on": False, "reason": "未启用"}, status
        rec = board.get("run/checkpoints/seg/1")
        assert rec["kind"] == "run" and rec["before"] == "" and rec["note"] == "未启用", rec
        s = run_changes.summary(board, rt)
        assert s["checkpoints"] == {"on": False, "reason": "未启用"} and s["changed"] == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_run_that_predates_the_feature():
    tmp, wd, rt, board, _store = _setup()
    try:
        s = run_changes.summary(board, rt)
        assert s["checkpoints"]["on"] is False and "早于" in s["checkpoints"]["reason"], s
        assert s["segments"] == [] and s["changed"] == 0 and s["rollbacks"] == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_segment_records_before_and_after():
    tmp, wd, rt, board, store = _setup()
    try:
        _write(wd, "a.txt", b"one\n")
        seg, status = run_changes.begin(board, store, wd, RID, "run", "engine")
        assert status == {"on": True, "reason": ""} and seg.n == 1, status
        _write(wd, "a.txt", b"two\n")
        _write(wd, "b.txt", b"new\n")
        run_changes.end(board, seg, author="engine")
        rec = board.get("run/checkpoints/seg/1")
        assert rec["before"] and rec["after"] and rec["note"] == "", rec
        assert rec["ended"] >= rec["started"] > 0 and rec["workdir"] == os.path.realpath(wd)
        s = run_changes.summary(board, rt)
        assert s["checkpoints"] == {"on": True, "reason": ""}
        seg1 = s["segments"][0]
        assert [(f["path"], f["status"]) for f in seg1["files"]] == [("a.txt", "M"), ("b.txt", "A")]
        assert seg1["can_rollback"] is True and seg1["open"] is False
        assert s["changed"] == 2 and s["counts"] == {"added": 1, "modified": 1, "deleted": 0}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_task_checkpoints_pin_changes_to_the_task():
    tmp, wd, rt, board, store = _setup()
    try:
        seg, _ = run_changes.begin(board, store, wd, RID, "run", "engine")
        _write(wd, "plan.md", b"written while planning")          # before any task
        run_changes.record_task(board, seg, "t1", "before",
                                run_changes.task_snapshot(seg, "t1", "before"))
        _write(wd, "t1.txt", b"by t1")
        rec = run_changes.record_task(board, seg, "t1", "after",
                                      run_changes.task_snapshot(seg, "t1", "after"))
        assert rec["before"] and rec["after"], rec
        board.put("tasks/t1/spec", {"title": "写代码"}, author="system", kind="spec")
        run_changes.end(board, seg, author="engine")
        seg1 = run_changes.summary(board, rt)["segments"][0]
        assert [f["path"] for f in seg1["files"]] == ["plan.md", "t1.txt"]
        assert seg1["tasks"] == [{"id": "t1", "title": "写代码", "paths": ["t1.txt"],
                                  "complete": True}], seg1["tasks"]
        assert board.entry("tasks/t1/checkpoints/1")["task_id"] is None, \
            "bookkeeping stays out of the task's activity log"
        label = run_changes.task_label("a/b c", 1, "before")
        assert label.startswith("task-") and label.endswith("-1-before") and "/" not in label
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_failed_task_snapshot_marks_the_segment_incomplete():
    tmp, wd, rt, board, _store = _setup()
    try:
        seg = run_changes.Segment(store="x.git", workdir=wd, run_id=RID, n=1)
        board.put("run/checkpoints/seg/1", {"kind": "run", "workdir": wd, "before": "b" * 40,
                                            "after": "", "note": "", "skipped": [],
                                            "incomplete": False}, author="engine")
        run_changes.record_task(board, seg, "t1", "after",
                                {"commit": "", "skipped": [], "error": "git add 失败：boom"})
        rec = board.get("run/checkpoints/seg/1")
        assert rec["incomplete"] is True and "t1" in rec["note"] and "boom" in rec["note"], rec
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_next_segment_closes_a_dangling_one_late():
    tmp, wd, rt, board, store = _setup()
    try:
        run_changes.begin(board, store, wd, RID, "run", "engine")
        _write(wd, "a.txt", b"x")                  # the engine was killed: seg 1 never ended
        seg2, _ = run_changes.begin(board, store, wd, RID, "rerun", "engine")
        rec1 = board.get("run/checkpoints/seg/1")
        assert rec1["after"] and rec1["ended"] and run_changes.LATE_NOTE in rec1["note"], rec1
        assert seg2.n == 2 and board.get("run/checkpoints/seg/2")["kind"] == "rerun"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_close_open_only_touches_an_open_segment():
    tmp, wd, rt, board, store = _setup()
    try:
        run_changes.begin(board, store, wd, RID, "run", "engine")
        _write(wd, "a.txt", b"x")
        assert run_changes.close_open(board, rt, RID, note="", author="system") is True
        rec = board.get("run/checkpoints/seg/1")
        assert rec["after"] and rec["note"] == "", rec
        assert run_changes.close_open(board, rt, RID, note="", author="system") is False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_followup_segment_is_the_tasks_own_change():
    tmp, wd, rt, board, store = _setup()
    try:
        seg, _ = run_changes.begin(board, store, wd, RID, "followup", "system", task_id="t2")
        _write(wd, "f.txt", b"x\n")
        run_changes.end(board, seg, author="system")
        rec = board.get("run/checkpoints/seg/1")
        assert rec["kind"] == "followup" and rec["task_id"] == "t2"
        assert board.get("tasks/t2/checkpoints/1") == {"before": rec["before"],
                                                        "after": rec["after"]}
        assert "+x" in run_changes.task_file_diff(board, rt, "t2", "f.txt")
        assert run_changes.task_file_diff(board, rt, "t2", "other.txt") is None
        assert run_changes.task_file_diff(board, rt, "t9", "f.txt") is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_segment_diff_is_whitelisted():
    tmp, wd, rt, board, store = _setup()
    try:
        _write(wd, "a.txt", b"one\n")
        seg, _ = run_changes.begin(board, store, wd, RID, "run", "engine")
        _write(wd, "a.txt", b"two\n")
        run_changes.end(board, seg, author="engine")
        assert "+two" in run_changes.segment_diff(board, rt, 1, "a.txt")
        for n, path in ((1, "b.txt"), (1, "../a.txt"), (2, "a.txt")):
            try:
                run_changes.segment_diff(board, rt, n, path)
            except LookupError:
                continue
            raise AssertionError(f"segment {n} / {path} must not be diffable")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_summary_reports_gone_checkpoints():
    """Review Focus 4."""
    tmp, wd, rt, board, store = _setup()
    try:
        seg, _ = run_changes.begin(board, store, wd, RID, "run", "engine")
        _write(wd, "a.txt", b"x")
        run_changes.end(board, seg, author="engine")
        _rmtree(store)
        seg1 = run_changes.summary(board, rt)["segments"][0]
        assert seg1["files"] == [] and seg1["can_rollback"] is False, seg1
        assert "已不存在" in seg1["error"], seg1
        try:
            run_changes.segment_diff(board, rt, 1, "a.txt")
        except checkpoints.CheckpointError as e:
            assert "已不存在" in str(e)
        else:
            raise AssertionError("a gone checkpoint cannot be diffed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_counts_are_net_across_segments():
    tmp, wd, rt, board, store = _setup()
    try:
        _write(wd, "a.txt", b"1")
        s1, _ = run_changes.begin(board, store, wd, RID, "run", "engine")
        _write(wd, "a.txt", b"2")
        _write(wd, "tmp.txt", b"scratch")
        run_changes.end(board, s1, author="engine")
        s2, _ = run_changes.begin(board, store, wd, RID, "rerun", "engine")
        os.remove(os.path.join(wd, "tmp.txt"))
        _write(wd, "a.txt", b"3")
        run_changes.end(board, s2, author="engine")
        s = run_changes.summary(board, rt)
        assert [len(x["files"]) for x in s["segments"]] == [2, 2]
        assert s["changed"] == 1 and s["counts"] == {"added": 0, "modified": 1, "deleted": 0}, s
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_rollback_restores_and_undo_puts_it_back():
    tmp, wd, rt, board, store = _setup()
    try:
        _write(wd, "a.txt", b"one")
        _write(wd, "gone.txt", b"bye")
        seg, _ = run_changes.begin(board, store, wd, RID, "run", "engine")
        _write(wd, "a.txt", b"two")
        os.remove(os.path.join(wd, "gone.txt"))
        _write(wd, "new.txt", b"new")
        run_changes.end(board, seg, author="engine")

        rec = run_changes.rollback(board, rt, RID)
        assert rec["restored"] == ["a.txt", "gone.txt", "new.txt"] and rec["skipped"] == [], rec
        assert rec["pre"] and rec["post"] and rec["undone"] is False and rec["error"] == ""
        assert _read(wd, "a.txt") == b"one" and _read(wd, "gone.txt") == b"bye"
        assert not os.path.exists(os.path.join(wd, "new.txt"))
        assert board.get("run/checkpoints/rollback/1")["pre"] == rec["pre"]

        undo = run_changes.undo(board, rt)
        assert undo["restored"] == ["a.txt", "gone.txt", "new.txt"], undo
        assert _read(wd, "a.txt") == b"two" and _read(wd, "new.txt") == b"new"
        assert not os.path.exists(os.path.join(wd, "gone.txt"))
        assert board.get("run/checkpoints/rollback/1")["undone"] is True
        try:
            run_changes.undo(board, rt)
        except run_changes.RollbackError as e:
            assert "已经撤销过" in str(e)
        else:
            raise AssertionError("the same rollback cannot be undone twice")
        rb = run_changes.summary(board, rt)["rollbacks"]
        assert [(r["m"], r["undone"]) for r in rb] == [(1, True)], rb
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_interleaved_runs_keep_later_work():
    """Spec §5.2 example: run A changes F, run B changes F, then a follow-up of
    A changes F. Rolling back A undoes the follow-up -- F goes back to B's
    version -- and leaves B's work alone."""
    tmp, wd, rt, board_a, store = _setup()
    board_b = Blackboard(os.path.join(tmp, "board_b.jsonl"), on_write=None)
    try:
        _write(wd, "F.txt", b"original")
        a1, _ = run_changes.begin(board_a, store, wd, "aaaa0001", "run", "engine")
        _write(wd, "F.txt", b"by A")
        run_changes.end(board_a, a1, author="engine")
        b1, _ = run_changes.begin(board_b, store, wd, "bbbb0002", "run", "engine")
        _write(wd, "F.txt", b"by B")
        run_changes.end(board_b, b1, author="engine")
        a2, _ = run_changes.begin(board_a, store, wd, "aaaa0001", "followup", "system",
                                  task_id="t1")
        _write(wd, "F.txt", b"by A's follow-up")
        run_changes.end(board_a, a2, author="system")

        rec = run_changes.rollback(board_a, rt, "aaaa0001")
        assert _read(wd, "F.txt") == b"by B", _read(wd, "F.txt")
        assert rec["restored"] == ["F.txt"]
        assert [(s["path"], s["seg"]) for s in rec["skipped"]] == [("F.txt", 1)], rec["skipped"]
        assert "之后又被改过" in rec["skipped"][0]["reason"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_rollback_only_requested_paths():
    tmp, wd, rt, board, store = _setup()
    try:
        _write(wd, "a.txt", b"a1")
        _write(wd, "b.txt", b"b1")
        seg, _ = run_changes.begin(board, store, wd, RID, "run", "engine")
        _write(wd, "a.txt", b"a2")
        _write(wd, "b.txt", b"b2")
        run_changes.end(board, seg, author="engine")
        rec = run_changes.rollback(board, rt, RID, paths=["a.txt"])
        assert rec["restored"] == ["a.txt"] and rec["requested"] == ["a.txt"], rec
        assert _read(wd, "a.txt") == b"a1" and _read(wd, "b.txt") == b"b2"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_incomplete_segment_is_listed_not_rolled_back():
    tmp, wd, rt, board, store = _setup()
    try:
        s1, _ = run_changes.begin(board, store, wd, RID, "run", "engine")
        _write(wd, "a.txt", b"a")
        run_changes.record_task(board, s1, "t1", "after",
                                {"commit": "", "skipped": [], "error": "boom"})
        run_changes.end(board, s1, author="engine")
        try:
            run_changes.rollback(board, rt, RID)
        except run_changes.RollbackError as e:
            assert "没有可以回滚" in str(e)
        else:
            raise AssertionError("an incomplete segment alone cannot be rolled back")
        s2, _ = run_changes.begin(board, store, wd, RID, "rerun", "engine")
        _write(wd, "b.txt", b"b")
        run_changes.end(board, s2, author="engine")
        rec = run_changes.rollback(board, rt, RID)
        assert rec["restored"] == ["b.txt"] and os.path.exists(os.path.join(wd, "a.txt"))
        assert [(s["path"], s["seg"]) for s in rec["skipped"]] == [("a.txt", 1)], rec
        assert rec["skipped"][0]["reason"] == run_changes.INCOMPLETE
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_rollback_refuses_when_checkpoints_are_gone():
    """Review Focus 4: refused before touching anything, and nothing recorded."""
    tmp, wd, rt, board, store = _setup()
    try:
        seg, _ = run_changes.begin(board, store, wd, RID, "run", "engine")
        _write(wd, "a.txt", b"x")
        run_changes.end(board, seg, author="engine")
        _rmtree(store)
        try:
            run_changes.rollback(board, rt, RID)
        except run_changes.RollbackError as e:
            assert "已不存在" in str(e)
        else:
            raise AssertionError("must refuse")
        assert board.query("run/checkpoints/rollback/") == []
        assert _read(wd, "a.txt") == b"x"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_rollback_refuses_a_bad_run_id():
    tmp, wd, rt, board, _store = _setup()
    try:
        try:
            run_changes.rollback(board, rt, "../x")
        except run_changes.RollbackError:
            pass
        else:
            raise AssertionError("a run id is never a path")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_second_rollback_waits_for_undo():
    """R-P10: at most one un-undone rollback per run -- otherwise a second,
    even accidental, rollback would bury the first one's undo for good."""
    tmp, wd, rt, board, store = _setup()
    try:
        _write(wd, "a.txt", b"one")
        seg, _ = run_changes.begin(board, store, wd, RID, "run", "engine")
        _write(wd, "a.txt", b"two")
        run_changes.end(board, seg, author="engine")

        rec = run_changes.rollback(board, rt, RID)
        assert rec["restored"] == ["a.txt"] and _read(wd, "a.txt") == b"one", rec

        original_snapshot, calls = checkpoints.snapshot, []
        def counted(*a, **k):
            calls.append(1)
            return original_snapshot(*a, **k)
        checkpoints.snapshot = counted
        try:
            try:
                run_changes.rollback(board, rt, RID)
            except run_changes.RollbackError as e:
                assert "先撤销" in str(e)
            else:
                raise AssertionError("a second rollback must wait for the first to be undone")
        finally:
            checkpoints.snapshot = original_snapshot
        assert calls == [], "a refused rollback must not touch a checkpoint at all"
        assert [e["key"] for e in board.query(run_changes.ROLLBACK_PREFIX)] == \
            ["run/checkpoints/rollback/1"]

        run_changes.undo(board, rt)
        assert _read(wd, "a.txt") == b"two"

        rec2 = run_changes.rollback(board, rt, RID)
        assert rec2["restored"] == ["a.txt"] and _read(wd, "a.txt") == b"one", rec2
        assert board.get("run/checkpoints/rollback/2") is not None
        assert [e["key"] for e in board.query(run_changes.ROLLBACK_PREFIX)] == \
            ["run/checkpoints/rollback/1", "run/checkpoints/rollback/2"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_rollback_with_failed_post_checkpoint_does_not_block_future_rollbacks():
    """F1 (narrows R-P10): a rollback whose post checkpoint failed can never
    be undone (undo() needs both pre and post), so it must not lock every
    later rollback out forever."""
    tmp, wd, rt, board, store = _setup()
    try:
        _write(wd, "a.txt", b"a1")
        _write(wd, "b.txt", b"b1")
        seg, _ = run_changes.begin(board, store, wd, RID, "run", "engine")
        _write(wd, "a.txt", b"a2")
        _write(wd, "b.txt", b"b2")
        run_changes.end(board, seg, author="engine")

        original_snapshot = checkpoints.snapshot
        def flaky_post(store_, workdir_, ref_name, exclude=()):
            if ref_name.endswith("rollback1-post"):
                return {"commit": "", "skipped": [], "error": "模拟：拍不了"}
            return original_snapshot(store_, workdir_, ref_name, exclude)
        checkpoints.snapshot = flaky_post
        try:
            rec = run_changes.rollback(board, rt, RID, paths=["a.txt"])
        finally:
            checkpoints.snapshot = original_snapshot
        assert rec["restored"] == ["a.txt"] and rec["post"] == "", rec
        assert _read(wd, "a.txt") == b"a1"

        try:
            run_changes.undo(board, rt)
        except run_changes.RollbackError:
            pass
        else:
            raise AssertionError("a rollback with no post checkpoint cannot be undone")

        rec2 = run_changes.rollback(board, rt, RID, paths=["b.txt"])
        assert rec2["restored"] == ["b.txt"], rec2
        assert _read(wd, "b.txt") == b"b1"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_rollback_matching_nothing_does_not_block_the_next_one():
    """F1: a rollback whose paths matched no changed file restored nothing,
    so it never counted as "waiting to be undone" in the first place."""
    tmp, wd, rt, board, store = _setup()
    try:
        _write(wd, "a.txt", b"a1")
        seg, _ = run_changes.begin(board, store, wd, RID, "run", "engine")
        _write(wd, "a.txt", b"a2")
        run_changes.end(board, seg, author="engine")

        rec = run_changes.rollback(board, rt, RID, paths=["nope.txt"])
        assert rec["restored"] == [] and rec["pre"] and rec["post"], rec

        rec2 = run_changes.rollback(board, rt, RID, paths=["a.txt"])
        assert rec2["restored"] == ["a.txt"], rec2
        assert _read(wd, "a.txt") == b"a1"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_midway_restore_failure_still_records_what_it_changed():
    """Plan-mandated (F2): restore() can die partway through a segment (a
    corrupt/missing blob slips the batch-check pre-check) after phase 1
    already deleted the files this segment created. That on-disk change is
    real and must show up in the record, not be silently lost."""
    tmp, wd, rt, board, store = _setup()
    try:
        _write(wd, "a.txt", b"one")
        seg, _ = run_changes.begin(board, store, wd, RID, "run", "engine")
        _write(wd, "a.txt", b"two")
        _write(wd, "created.txt", b"new")
        run_changes.end(board, seg, author="engine")

        original_read = checkpoints._BlobReader.read
        def boom(self, sha):
            raise checkpoints.CheckpointError("模拟：blob 读取失败")
        checkpoints._BlobReader.read = boom
        try:
            rec = run_changes.rollback(board, rt, RID)
        finally:
            checkpoints._BlobReader.read = original_read

        assert rec["restored"] == ["created.txt"], rec
        assert rec["error"], rec
        assert not os.path.exists(os.path.join(wd, "created.txt"))
        assert _read(wd, "a.txt") == b"two"          # phase 2 never got to it

        undo = run_changes.undo(board, rt)
        assert "created.txt" in undo["restored"], undo
        assert _read(wd, "created.txt") == b"new"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_undo_midway_failure_records_progress_and_retry_only_touches_remaining_files():
    tmp, wd, rt, board, store = _setup()
    try:
        _write(wd, "a.txt", b"one")
        _write(wd, "deleted.txt", b"old")
        seg, _ = run_changes.begin(board, store, wd, RID, "run", "engine")
        _write(wd, "a.txt", b"two")
        os.remove(os.path.join(wd, "deleted.txt"))
        run_changes.end(board, seg, author="engine")
        run_changes.rollback(board, rt, RID)

        original_read = checkpoints._BlobReader.read
        def boom(self, sha):
            raise checkpoints.CheckpointError("模拟：撤销读 blob 失败")
        checkpoints._BlobReader.read = boom
        try:
            first = run_changes.undo(board, rt)
        finally:
            checkpoints._BlobReader.read = original_read
        assert first["error"] and first["restored"] == ["deleted.txt"], first
        rec = board.get(f"{run_changes.ROLLBACK_PREFIX}1")
        assert rec["undone"] is False and rec["undo"]["restored"] == ["deleted.txt"], rec
        assert not os.path.exists(os.path.join(wd, "deleted.txt"))
        assert _read(wd, "a.txt") == b"one"

        second = run_changes.undo(board, rt)
        assert second["restored"] == ["a.txt"] and not second["skipped"], second
        rec = board.get(f"{run_changes.ROLLBACK_PREFIX}1")
        assert rec["undone"] is True
        assert rec["undo"]["restored"] == ["a.txt", "deleted.txt"], rec
        assert _read(wd, "a.txt") == b"two"
    finally:
        _rmtree(tmp)


@needs_git
def test_undo_all_skipped_stays_pending_until_a_later_retry():
    tmp, wd, rt, board, store = _setup()
    try:
        _write(wd, "a.txt", b"one")
        seg, _ = run_changes.begin(board, store, wd, RID, "run", "engine")
        _write(wd, "a.txt", b"two")
        _write(wd, "new.txt", b"new")
        run_changes.end(board, seg, author="engine")
        run_changes.rollback(board, rt, RID)
        _write(wd, "a.txt", b"later")
        _write(wd, "new.txt", b"later")

        first = run_changes.undo(board, rt)
        assert first["restored"] == [] and len(first["skipped"]) == 2, first
        assert first["undone"] is False, first
        assert board.get(f"{run_changes.ROLLBACK_PREFIX}1")["undone"] is False

        _write(wd, "a.txt", b"one")
        os.remove(os.path.join(wd, "new.txt"))
        second = run_changes.undo(board, rt)
        assert second["undone"] is True and second["restored"] == ["a.txt", "new.txt"], second
        assert _read(wd, "a.txt") == b"two" and _read(wd, "new.txt") == b"new"
    finally:
        _rmtree(tmp)


def test_non_ascii_digit_rollback_key_is_ignored():
    """R-T5b / F4: str.isdigit() accepts characters int() rejects (e.g. the
    superscript '²'); such a key must not crash summary(), and
    rollback()/undo() must not mistake it for a real rollback record."""
    tmp, wd, rt, board, _store = _setup()
    try:
        board.put("run/checkpoints/rollback/²",
                 {"pre": "a" * 40, "post": "b" * 40, "undone": False, "restored": ["z"]},
                 author="engine")
        s = run_changes.summary(board, rt)
        assert s["rollbacks"] == [], s["rollbacks"]
        try:
            run_changes.undo(board, rt)
        except run_changes.RollbackError as e:
            assert "还没有回滚过" in str(e)
        else:
            raise AssertionError("a non-ASCII-digit key is not a real rollback")
        try:
            run_changes.rollback(board, rt, RID)
        except run_changes.RollbackError as e:
            assert "没有可以回滚" in str(e)
        else:
            raise AssertionError("no real segments exist here either")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_failed_closing_snapshot_shows_as_segment_error():
    """R-T5a: a segment whose closing snapshot failed must show why, not
    silently look like "no files changed"."""
    tmp, wd, rt, board, store = _setup()
    try:
        _write(wd, "a.txt", b"one")
        seg, _ = run_changes.begin(board, store, wd, RID, "run", "engine")
        _write(wd, "a.txt", b"two")
        original_snapshot = checkpoints.snapshot
        checkpoints.snapshot = lambda *a, **k: {"commit": "", "skipped": [],
                                                "error": "模拟：拍不了"}
        try:
            run_changes.end(board, seg, author="engine")
        finally:
            checkpoints.snapshot = original_snapshot
        seg1 = run_changes.summary(board, rt)["segments"][0]
        assert "收尾检查点没拍成" in seg1["error"] and "模拟：拍不了" in seg1["error"], seg1
        assert seg1["can_rollback"] is False and seg1["files"] == [], seg1
        assert run_changes.summary(board, rt)["changed"] == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_task_file_diff_ignores_non_ascii_numbered_key():
    tmp, _wd, rt, board, _store = _setup()
    try:
        board.put("tasks/t1/checkpoints/²", {"before": "a", "after": "b"},
                  author="engine")
        assert run_changes.task_file_diff(board, rt, "t1", "a.txt") is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll run-changes tests passed.")
