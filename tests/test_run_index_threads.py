"""B34 (2026-09-25): the run index is written from two threads with no lock.
The watchdog thread records a finished run (_runs_update) while a request
thread adds the next one (_runs_add, from /run or /rerun), and
hub/atomicio.atomic_write names its temp file after the process id only --
so both threads open the same temp file. On Windows that is PermissionError
(or WinError 32 on the rename), crashing the request or the watchdog;
tests/test_forced_rerun.py failed 2 runs in 5 this way. On POSIX the writes
interleave, and a save that serialized before the other thread's update can
land last: the index then says "running" for a run that finished.

Run: python tests/test_run_index_threads.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve


def test_run_index_writers_on_two_threads():
    orig = (serve.INDEX_PATH, serve._runs)
    serve.INDEX_PATH = os.path.join(tempfile.mkdtemp(), "index.jsonl")
    serve._runs = [{"run_id": "r0", "status": "running"}]
    errors = []

    def adder():                       # the request thread: new runs
        for i in range(150):
            try:
                serve._runs_add(f"r{i + 1}", "goal")
            except Exception as e:     # noqa: BLE001 -- collecting, not handling
                errors.append(e)

    def updater():                     # the watchdog thread: finished runs
        for i in range(150):
            try:
                serve._runs_update("r0", status="done", n=i)
            except Exception as e:     # noqa: BLE001
                errors.append(e)

    try:
        threads = [threading.Thread(target=adder), threading.Thread(target=updater)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, f"{len(errors)} failed writes, e.g. {errors[0]!r}"
        with open(serve.INDEX_PATH, encoding="utf-8") as f:
            on_disk = [json.loads(line) for line in f if line.strip()]
        assert on_disk == serve._runs, "the index on disk lost an update"
    finally:
        serve.INDEX_PATH, serve._runs = orig
    print("PASS: test_run_index_writers_on_two_threads")


if __name__ == "__main__":
    test_run_index_writers_on_two_threads()
    print("\nAll run-index thread tests passed.")
