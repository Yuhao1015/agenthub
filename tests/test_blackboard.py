"""Blackboard.refresh() -- incremental re-read for hot polling loops
(see hub/engine.py's _await_input_loop), as opposed to constructing a
fresh Blackboard(path) every tick (O(file size) per tick). No framework
-- plain assert, exit 1 on failure.

Run: python tests/test_blackboard.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.blackboard import Blackboard


def _fresh_path():
    tmp = tempfile.mkdtemp()
    return os.path.join(tmp, "board.jsonl")


def test_refresh_picks_up_entries_written_by_another_instance():
    path = _fresh_path()
    writer = Blackboard(path, on_write=None)
    writer.put("a", 1, author="system")

    reader = Blackboard(path, on_write=None)  # sees "a" via initial _replay
    assert reader.get("a") == 1

    writer.put("b", 2, author="system")
    assert reader.get("b") is None, "reader must not see it until refresh()"
    reader.refresh()
    assert reader.get("b") == 2
    print("PASS: test_refresh_picks_up_entries_written_by_another_instance")


def test_refresh_is_a_noop_when_nothing_new():
    path = _fresh_path()
    writer = Blackboard(path, on_write=None)
    writer.put("a", 1, author="system")
    reader = Blackboard(path, on_write=None)
    before = dict(reader._kv)
    reader.refresh()
    reader.refresh()
    assert reader._kv == before
    print("PASS: test_refresh_is_a_noop_when_nothing_new")


def test_refresh_before_file_exists_then_after():
    """A Blackboard constructed before the file exists (path never
    touched yet) must not crash on refresh(), and must pick up entries
    once another process creates + writes the file."""
    path = _fresh_path()
    assert not os.path.exists(path)
    reader = Blackboard(path, on_write=None)
    reader.refresh()  # must not raise
    assert reader.get("a") is None

    writer = Blackboard(path, on_write=None)
    writer.put("a", 1, author="system")
    reader.refresh()
    assert reader.get("a") == 1
    print("PASS: test_refresh_before_file_exists_then_after")


def test_refresh_does_not_consume_an_incomplete_trailing_line():
    """A concurrent writer's in-flight last line (no trailing newline yet)
    must be left unconsumed -- re-read once complete, not silently
    dropped forever because the byte offset already skipped past it."""
    path = _fresh_path()
    writer = Blackboard(path, on_write=None)
    writer.put("a", 1, author="system")

    reader = Blackboard(path, on_write=None)
    reader.refresh()
    assert reader.get("a") == 1

    # simulate a partial write: valid JSON line with NO trailing "\n" yet
    partial = '{"key": "b", "value": 2, "author": "system", "task_id": null, "kind": "fact", "confidence": 1.0, "ts": 1.0}'
    with open(path, "a", encoding="utf-8") as f:
        f.write(partial)  # no newline -- write "in progress"

    reader.refresh()
    assert reader.get("b") is None, \
        "an incomplete trailing line must not be consumed as if it were a full entry"

    # the writer "finishes" the line
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n")
    reader.refresh()
    assert reader.get("b") == 2, "must pick it up once the line is actually complete"
    print("PASS: test_refresh_does_not_consume_an_incomplete_trailing_line")


def test_own_writes_are_not_reprocessed_via_refresh():
    """A Blackboard's own put() must advance _replayed_bytes so a
    subsequent refresh() doesn't redundantly re-read what it just wrote
    itself (harmless if it did -- last-write-wins -- but wasteful)."""
    path = _fresh_path()
    board = Blackboard(path, on_write=None)
    board.put("a", 1, author="system")
    size_after_put = os.path.getsize(path)
    assert board._replayed_bytes == size_after_put
    board.refresh()
    assert board._replayed_bytes == size_after_put, \
        "refresh() after a local put() should find nothing new to consume"
    print("PASS: test_own_writes_are_not_reprocessed_via_refresh")


def test_refresh_updates_note_seq_for_notes_written_elsewhere():
    path = _fresh_path()
    writer = Blackboard(path, on_write=None)
    reader = Blackboard(path, on_write=None)
    writer.add_note("finding one", author="agent-x")
    reader.refresh()
    assert reader.get("notes/1") == "finding one"
    # reader's own next add_note must not collide with the seq it just saw
    reader.add_note("finding two", author="agent-y")
    assert reader.get("notes/2") == "finding two"
    print("PASS: test_refresh_updates_note_seq_for_notes_written_elsewhere")


if __name__ == "__main__":
    test_refresh_picks_up_entries_written_by_another_instance()
    test_refresh_is_a_noop_when_nothing_new()
    test_refresh_before_file_exists_then_after()
    test_refresh_does_not_consume_an_incomplete_trailing_line()
    test_own_writes_are_not_reprocessed_via_refresh()
    test_refresh_updates_note_seq_for_notes_written_elsewhere()
    print("\nAll Blackboard.refresh() tests passed.")
