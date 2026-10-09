"""A run's change record on its own blackboard
(docs/decisions/change-safety-net.md §3.2-§5.2).

Segments: every main.py start (the run itself, each /rerun) and every
follow-up whose agent can write opens one; its before/after checkpoints bound
what it changed. Write-capable tasks also get their own before/after inside
it, so a change can be pinned to the task that made it. Rollback walks the
segments newest-first and restores a file only when it is still exactly what
that segment left.

Blackboard keys -- written by "engine" (main.py and the engine) or "system"
(serve.py), both in SYSTEM_AUTHORS, so the permission model is unchanged:
  run/checkpoints/seg/<n>       {kind: run|rerun|followup, task_id, workdir,
                                 before, after, started, ended, incomplete,
                                 note, skipped}
  tasks/<id>/checkpoints/<n>    {before, after}; written with task_id=None so
                                 the task's activity log stays free of it
  run/checkpoints/rollback/<m>  {ts, pre, post, workdir, requested, restored,
                                 skipped, undone, error[, undo]}
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass

from . import checkpoints

SEG_PREFIX = "run/checkpoints/seg/"
ROLLBACK_PREFIX = "run/checkpoints/rollback/"
LATE_NOTE = "运行没有正常结束，结束检查点是事后补拍的，可能包含之后的手动修改"
OFF_NOTE = "未启用"
NO_SEGMENTS = "这次运行早于改动安全网，没有检查点"
INCOMPLETE = "这一段的检查点不完整，不能回滚"
GONE = "这次运行的检查点已不存在（可能过了保留期被清理，或检查点库被删了）"
_RUN_ID = re.compile(r"^[0-9a-f]{1,32}$")


class RollbackError(Exception):
    """Rollback / undo refused; str(e) is shown to the user."""


@dataclass
class Segment:
    store: str
    workdir: str
    run_id: str
    n: int
    exclude: tuple = ()


def _join(*notes) -> str:
    return "；".join(n for n in notes if n)


def _merge(old, new) -> list:
    seen = {(s.get("path"), s.get("reason")) for s in old or []}
    return list(old or []) + [s for s in new or []
                              if (s.get("path"), s.get("reason")) not in seen]


def _numbered(board, prefix: str) -> list:
    """[(n, value)] for entries under `prefix` whose key tail is a plain
    ASCII integer, oldest first (board.query()'s own order). str.isdigit()
    alone accepts characters int() rejects (e.g. the superscript '²'), which
    would otherwise crash summary()'s never-raise promise and turn a bad key
    into a raw ValueError instead of a clean RollbackError."""
    out = []
    for e in board.query(prefix):
        tail = e["key"][len(prefix):]
        if tail.isascii() and tail.isdigit():
            out.append((int(tail), e["value"]))
    return out


def _seg_numbers(board) -> list:
    return sorted(n for n, _v in _numbered(board, SEG_PREFIX))


def segments(board) -> list:
    """[(n, record)] oldest first."""
    return [(n, board.get(f"{SEG_PREFIX}{n}") or {}) for n in _seg_numbers(board)]


def _close(board, n: int, store: str, run_id: str, note: str, author: str,
           exclude=()) -> None:
    key = f"{SEG_PREFIX}{n}"
    rec = dict(board.get(key) or {})
    if not rec.get("before") or rec.get("ended"):
        return
    snap = checkpoints.snapshot(store, rec["workdir"],
                                checkpoints.ref(run_id, f"seg{n}-after"), exclude)
    rec.update(after=snap["commit"], ended=round(time.time(), 3),
               skipped=_merge(rec.get("skipped"), snap["skipped"]),
               note=_join(rec.get("note"), note, snap["error"]))
    board.put(key, rec, author=author)
    if rec.get("kind") == "followup" and rec.get("task_id"):
        # 追问这一段整段都是那个任务的改动（spec §3.3）
        board.put(f"tasks/{rec['task_id']}/checkpoints/{n}",
                  {"before": rec["before"], "after": rec["after"]}, author=author)


def begin(board, store, workdir: str, run_id: str, kind: str, author: str,
          task_id=None, exclude=()):
    """Open segment n+1 and take its `before` checkpoint. Board writes can raise.
    Returns (Segment, {"on": True, "reason": ""}) or, when this segment gets no
    checkpoints, (None, {"on": False, "reason": why}) -- the reason is also
    written on the segment record."""
    workdir = os.path.realpath(workdir)
    nums = _seg_numbers(board)
    n = (nums[-1] + 1) if nums else 1
    rec = {"kind": kind, "task_id": task_id, "workdir": workdir, "before": "",
           "after": "", "started": round(time.time(), 3), "ended": 0,
           "incomplete": False, "note": "", "skipped": []}
    if not store or not run_id:
        reason = OFF_NOTE
    elif not _RUN_ID.match(run_id):
        reason = f"运行 id 不合法：{run_id[:40]}"
    elif not checkpoints.available():
        reason = "没装 git"
    else:
        if nums and (board.get(f"{SEG_PREFIX}{nums[-1]}") or {}).get("workdir") == workdir:
            # 上一段没收尾（serve.py 被杀、看门狗也没来得及）：先补拍收掉（spec §3.2）
            _close(board, nums[-1], store, run_id, LATE_NOTE, author, exclude)
        checkpoints.clear_stale_lock(store)
        snap = checkpoints.snapshot(store, workdir,
                                    checkpoints.ref(run_id, f"seg{n}-before"), exclude)
        rec["before"], rec["skipped"], reason = snap["commit"], snap["skipped"], snap["error"]
    rec["note"] = reason
    board.put(f"{SEG_PREFIX}{n}", rec, author=author)
    if reason:
        return None, {"on": False, "reason": reason}
    return Segment(store, workdir, run_id, n, tuple(exclude)), {"on": True, "reason": ""}


def end(board, seg, author: str) -> None:
    """Take the segment's `after` checkpoint (no-op for a segment without one)."""
    if seg is not None:
        _close(board, seg.n, seg.store, seg.run_id, "", author, seg.exclude)


def close_open(board, runtime_dir: str, run_id: str, note: str, author: str,
               exclude=()) -> bool:
    """Close the run's last segment if it is still open: the watchdog after a
    cancel or crash (note=""), or on demand when serve.py never saw the run end
    (note=LATE_NOTE). The caller makes sure no engine is using the store."""
    nums = _seg_numbers(board)
    if not nums or not _RUN_ID.match(run_id or ""):
        return False
    rec = board.get(f"{SEG_PREFIX}{nums[-1]}") or {}
    if not rec.get("before") or rec.get("ended"):
        return False
    store = checkpoints.store_path(runtime_dir, rec["workdir"])
    checkpoints.clear_stale_lock(store)
    _close(board, nums[-1], store, run_id, note, author, exclude)
    return True


def task_label(task_id: str, n: int, when: str) -> str:
    """Planner task ids are unconstrained, so the ref name carries a hash of it."""
    return f"task-{hashlib.sha1(task_id.encode('utf-8')).hexdigest()[:10]}-{n}-{when}"


def task_snapshot(seg: Segment, task_id: str, when: str) -> dict:
    """git only -- safe to run off the event loop (asyncio.to_thread)."""
    return checkpoints.snapshot(seg.store, seg.workdir,
                                checkpoints.ref(seg.run_id, task_label(task_id, seg.n, when)),
                                seg.exclude)


def record_task(board, seg: Segment, task_id: str, when: str, snap: dict) -> dict:
    """The blackboard side of a task checkpoint: call it on the event-loop
    thread. A failed snapshot marks the segment incomplete (spec §3.5).
    Returns the task's {before, after} record."""
    key = f"tasks/{task_id}/checkpoints/{seg.n}"
    rec = dict(board.get(key) or {"before": "", "after": ""})
    rec[when] = snap["commit"]
    board.put(key, rec, author="engine")
    if snap["error"] or snap["skipped"]:
        seg_key = f"{SEG_PREFIX}{seg.n}"
        srec = dict(board.get(seg_key) or {})
        if snap["error"]:
            srec["incomplete"] = True
            srec["note"] = _join(srec.get("note"),
                                 f"任务 {task_id} 的检查点没拍到：{snap['error']}")
        srec["skipped"] = _merge(srec.get("skipped"), snap["skipped"])
        board.put(seg_key, srec, author="engine")
    return rec


def _checked_store(runtime_dir: str, rec: dict) -> str:
    store = checkpoints.store_path(runtime_dir, rec["workdir"])
    if not checkpoints.has(store, rec["before"], rec["after"]):
        raise checkpoints.CheckpointError(GONE)
    return store


def _task_groups(board, store: str, n: int) -> list:
    """Per-task attribution inside segment n: [{id, title, paths, complete}]."""
    suffix = f"/checkpoints/{n}"
    groups = []
    for e in board.query("tasks/"):
        key = e["key"]
        if not key.endswith(suffix):
            continue
        tid = key[len("tasks/"):-len(suffix)]
        v = e["value"] or {}
        complete = bool(v.get("before") and v.get("after"))
        paths = ([f["path"] for f in checkpoints.changes(store, v["before"], v["after"])]
                 if complete else [])
        spec = board.get(f"tasks/{tid}/spec") or {}
        groups.append({"id": tid, "title": spec.get("title") or tid, "paths": paths,
                       "complete": complete})
    return groups


def summary(board, runtime_dir: str) -> dict:
    """Everything GET /run/changes shows. Never raises: problems become notes.
    `changed`/`counts` are net per file across segments -- a file created and
    deleted again within the run does not count."""
    segs = segments(board)
    out, first_status, last_status = [], {}, {}
    for n, rec in segs:
        info = {"n": n, "kind": rec.get("kind") or "run", "task_id": rec.get("task_id"),
                "started": rec.get("started"), "ended": rec.get("ended"),
                "incomplete": bool(rec.get("incomplete")), "note": rec.get("note") or "",
                "skipped": rec.get("skipped") or [],
                "open": bool(rec.get("before")) and not rec.get("ended"),
                "files": [], "tasks": [], "can_rollback": False, "error": ""}
        if rec.get("before") and rec.get("after"):
            try:
                store = _checked_store(runtime_dir, rec)
                info["files"] = checkpoints.changes(store, rec["before"], rec["after"])
                info["tasks"] = _task_groups(board, store, n)
                info["can_rollback"] = not info["incomplete"]
            except checkpoints.CheckpointError as e:
                info["files"], info["tasks"], info["error"] = [], [], str(e)
        elif rec.get("before") and rec.get("ended"):
            # before 有、ended 有、after 没有：收尾快照没拍成（R-T5a），原因只落在
            # note 里的话，审阅页会误以为这段"没有改动"，必须显式冒泡成 error
            info["error"] = "这一段的收尾检查点没拍成：" + (rec.get("note") or "原因不明")
        for f in info["files"]:
            first_status.setdefault(f["path"], f["status"])
            last_status[f["path"]] = f["status"]
        out.append(info)
    net = {}
    for path, last in last_status.items():
        first = first_status[path]
        if first == "A" and last == "D":
            continue
        net[path] = "A" if first == "A" else ("D" if last == "D" else "M")
    if segs:
        ck = {"on": bool(segs[0][1].get("before")), "reason": segs[0][1].get("note") or ""}
    else:
        ck = {"on": False, "reason": NO_SEGMENTS}
    rollbacks = [dict(rec, m=m) for m, rec in _numbered(board, ROLLBACK_PREFIX)]
    return {"checkpoints": ck, "segments": out, "rollbacks": rollbacks,
            "changed": len(net),
            "counts": {"added": sum(s == "A" for s in net.values()),
                       "modified": sum(s == "M" for s in net.values()),
                       "deleted": sum(s == "D" for s in net.values())}}


def segment_diff(board, runtime_dir: str, n: int, path: str) -> str:
    """One changed file's diff inside segment n. LookupError: not a changed
    file of that segment -- the whitelist, same idea as GET /file.
    CheckpointError: the checkpoints are gone or git failed."""
    rec = board.get(f"{SEG_PREFIX}{n}") or {}
    if not (rec.get("before") and rec.get("after")):
        raise LookupError("这一段没有检查点")
    store = _checked_store(runtime_dir, rec)
    if not any(f["path"] == path
               for f in checkpoints.changes(store, rec["before"], rec["after"])):
        raise LookupError("not a changed file of this segment")
    return checkpoints.file_diff(store, rec["before"], rec["after"], path)


def task_file_diff(board, runtime_dir: str, task_id: str, path: str):
    """Diff of `path` from this task's newest checkpoints that changed it, or
    None when no checkpoint of the task covers it (GET /file/diff then falls
    back to git diff against HEAD)."""
    prefix = f"tasks/{task_id}/checkpoints/"
    found = []
    for n, value in _numbered(board, prefix):
        v = value or {}
        if v.get("before") and v.get("after"):
            found.append((n, v))
    for n, v in sorted(found, key=lambda x: x[0], reverse=True):
        workdir = (board.get(f"{SEG_PREFIX}{n}") or {}).get("workdir")
        if not workdir:
            continue
        store = checkpoints.store_path(runtime_dir, workdir)
        try:
            if any(f["path"] == path
                   for f in checkpoints.changes(store, v["before"], v["after"])):
                return checkpoints.file_diff(store, v["before"], v["after"], path)
        except checkpoints.CheckpointError:
            return None
    return None


def _last_rollback(board):
    """(m, record) of the highest-numbered rollback entry of this run, or
    (0, None) when it has none yet."""
    entries = _numbered(board, ROLLBACK_PREFIX)
    return max(entries, key=lambda mr: mr[0]) if entries else (0, None)


def has_unreversed_rollback(board) -> bool:
    """Whether any recorded rollback still has changes that were not undone."""
    return any(rec.get("restored") and
               (not rec.get("undone") or (rec.get("undo") or {}).get("skipped"))
               for _m, rec in _numbered(board, ROLLBACK_PREFIX))


def rollback(board, runtime_dir: str, run_id: str, paths=None, author: str = "system",
             exclude=()) -> dict:
    """Roll the run's segments back newest-first (spec §5.2), between a pre and
    a post checkpoint so the rollback itself can be undone. Incomplete
    segments are listed as skipped, never rolled back. At most one un-undone
    rollback per run: undo it before rolling back anything else -- but only
    when that rollback is actually undoable (has both a pre and a post
    checkpoint) and changed something (a non-empty `restored`); a rollback
    whose post checkpoint failed, or whose paths matched no changed file, can
    never be undone and so must never block the next one (F1). Raises
    RollbackError -- with no file touched -- when there is nothing to roll
    back, the checkpoints are gone, the pre checkpoint cannot be taken, or the
    previous undoable rollback of this run has not been undone yet."""
    if not _RUN_ID.match(run_id or ""):
        raise RollbackError("运行 id 不合法")
    last_m, last_rec = _last_rollback(board)
    if (last_rec is not None and not last_rec.get("undone") and last_rec.get("pre")
            and last_rec.get("post") and last_rec.get("restored")):
        raise RollbackError("上一次回滚还没撤销：先撤销它，再回滚别的文件")
    usable, blocked = [], []
    for n, rec in segments(board):
        if rec.get("before") and rec.get("after"):
            (blocked if rec.get("incomplete") else usable).append((n, rec))
    if not usable:
        raise RollbackError("这次运行没有可以回滚的检查点")
    workdir = usable[-1][1]["workdir"]
    store = checkpoints.store_path(runtime_dir, workdir)
    if not checkpoints.has(store, *[c for _n, r in usable for c in (r["before"], r["after"])]):
        raise RollbackError(GONE)
    m = last_m + 1
    checkpoints.clear_stale_lock(store)
    pre = checkpoints.snapshot(store, workdir, checkpoints.ref(run_id, f"rollback{m}-pre"),
                               exclude)
    if not pre["commit"]:
        raise RollbackError(f"回滚前的检查点拍不了，没有动任何文件：{pre['error']}")
    wanted = None if paths is None else set(paths)
    restored, skipped, error = [], [], ""
    for n, rec in blocked:
        try:
            files = checkpoints.changes(store, rec["before"], rec["after"])
        except checkpoints.CheckpointError:
            files = []
        skipped += [{"path": checkpoints.show(f["path"]), "reason": INCOMPLETE, "seg": n}
                    for f in files if wanted is None or f["path"] in wanted]
    try:
        for n, rec in reversed(usable):
            res = checkpoints.restore(store, workdir, rec["before"], rec["after"], wanted)
            restored += res["restored"]
            skipped += [{"path": checkpoints.show(s["path"]), "reason": s["reason"], "seg": n}
                        for s in res["skipped"]]
    except checkpoints.CheckpointError as e:
        error = str(e)
    post = checkpoints.snapshot(store, workdir, checkpoints.ref(run_id, f"rollback{m}-post"),
                                exclude)
    if error and post["commit"]:
        # restore() can die partway through a segment (a corrupt/missing blob
        # slips the batch-check pre-check): what it already changed on disk
        # before dying is real and must not be lost, so read the true effect
        # back from this rollback's own before/after instead of trusting the
        # partial in-memory `restored` list the loop above may have missed.
        try:
            restored = [f["path"] for f in checkpoints.changes(store, pre["commit"],
                                                                post["commit"])]
        except checkpoints.CheckpointError:
            pass  # keep the partial list already collected above
    record = {"ts": round(time.time(), 3), "pre": pre["commit"], "post": post["commit"],
              "workdir": workdir,
              "requested": None if wanted is None else sorted(checkpoints.show(p) for p in wanted),
              "restored": sorted({checkpoints.show(p) for p in restored}),
              "skipped": skipped, "undone": False, "error": _join(error, post["error"])}
    board.put(f"{ROLLBACK_PREFIX}{m}", record, author=author)
    return record


def undo(board, runtime_dir: str, author: str = "system") -> dict:
    """Undo this run's most recent rollback, same rule: only files still exactly
    as the rollback left them go back (spec §5.2). Partial progress is recorded
    so another undo can retry only the remaining files. Raises RollbackError
    before changing files when the rollback cannot be undone."""
    m, rec = _last_rollback(board)
    if rec is None:
        raise RollbackError("这次运行还没有回滚过")
    rec = dict(rec)
    if rec.get("undone"):
        raise RollbackError("最近一次回滚已经撤销过了")
    if not (rec.get("pre") and rec.get("post")):
        raise RollbackError("这次回滚没有留下完整的检查点，撤销不了")
    store = checkpoints.store_path(runtime_dir, rec["workdir"])
    if not checkpoints.has(store, rec["pre"], rec["post"]):
        raise RollbackError(GONE)
    checkpoints.clear_stale_lock(store)
    prior = rec.get("undo") or {}
    done = set(prior.get("restored") or [])
    error = ""
    try:
        remaining = {f["path"] for f in checkpoints.changes(store, rec["pre"], rec["post"])
                     if checkpoints.show(f["path"]) not in done}
        res = checkpoints.restore(store, rec["workdir"], rec["pre"], rec["post"], remaining)
    except checkpoints.CheckpointError as e:
        error = str(e)
        res = {"restored": getattr(e, "restored", []), "skipped": getattr(e, "skipped", [])}
    restored = [checkpoints.show(p) for p in res["restored"]]
    skipped = [{"path": checkpoints.show(s["path"]), "reason": s["reason"]}
               for s in res["skipped"]]
    undone = not error and not skipped
    result = {"ts": round(time.time(), 3),
              "restored": restored, "skipped": skipped, "error": error,
              "undone": undone}
    rec.update(undone=undone, undo={"ts": result["ts"],
                                   "restored": sorted(done | set(restored)),
                                   "skipped": skipped, "error": error})
    board.put(f"{ROLLBACK_PREFIX}{m}", rec, author=author)
    result["rolled_back"] = has_unreversed_rollback(board)
    return result
