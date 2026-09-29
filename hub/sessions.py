"""Manager clarification session storage: validation, serialization, one-file IO.

This is the data-plane half of the manager conversation (see CLAUDE.md §6.16).
It was extracted from serve.py on 2026-09-05.

**Why it lives here and not in serve.py.** Session snapshots are data, not
transport.  When §6.16 was built the whole thing landed in serve.py — not
because anyone was lazy, but because there was no obvious place to put it.
That is how a layer boundary erodes.  hub/ is where the data plane lives
(hub/blackboard.py is the precedent), so this is where the next piece of
session storage goes too.

**What is deliberately NOT here.** The in-memory session cache, the chat
lock, the active-session pointer, run archiving and reconciliation all stay
in serve.py: they are entangled with the manager conversation state machine
and with `_run_meta`, and serve.py's tests treat that cache as serve's own
(one of them rebinds `serve._sessions` outright).  Moving them would mean
first deciding who owns that cache, which is a separate refactor.

**Every function here takes what it needs as an argument.** Nothing reads a
module-level path or a global cache, so all of it is unit-testable without
standing up a server.  That matters most for the three-layer schema check:
an earlier review found four real defects in it (strict-schema, invalid
UTF-8, active-pointer race, semantic consistency), and that is exactly the
kind of code that should be reachable without 31 module globals in the way.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import time

from .atomicio import atomic_write

SESSION_ID_RE = re.compile(r"^[a-f0-9]{12}$")
SESSION_FILE_RE = re.compile(r"^([a-f0-9]{12})\.json$")
MANAGER_RUN_ID_RE = re.compile(r"^[a-f0-9]{8}$")

# The exact field set persisted to disk. `set(data) != set(DISK_FIELDS)` is a
# strict check on purpose: an unknown key means the file was written by a
# different version, and silently keeping it would let unvalidated data ride
# along into the next save.
DISK_FIELDS = (
    "schema_version", "session_id", "created_at", "updated_at", "status",
    "history", "questions", "awaiting_confirmation", "proposal", "cost_usd",
    "run_id",
)


class SessionDataError(RuntimeError):
    """Persisted manager-session data is missing or structurally unsafe."""


class SessionConflict(RuntimeError):
    """The requested mutation conflicts with the one active session."""


def validate_session_id(sid: str) -> str:
    if not isinstance(sid, str) or not SESSION_ID_RE.fullmatch(sid):
        raise ValueError("invalid session id")
    return sid


def session_path(sessions_dir: str, sid: str) -> str:
    """Path for one session file, or "" in legacy mode (no data directory).

    The id is validated BEFORE it is joined onto anything, so a hostile id
    never gets to participate in path construction.
    """
    sid = validate_session_id(sid)
    if not sessions_dir:
        return ""
    return os.path.join(sessions_dir, f"{sid}.json")


def new_manager_session(sid: str, now: float | None = None) -> dict:
    sid = validate_session_id(sid)
    ts = round(time.time() if now is None else now, 3)
    return {"schema_version": 1, "session_id": sid, "created_at": ts,
            "updated_at": ts, "status": "active", "history": [],
            "questions": 0, "awaiting_confirmation": False,
            "proposal": None, "cost_usd": 0.0, "run_id": ""}


def disk_dict(sess: dict) -> dict:
    return {key: sess.get(key) for key in DISK_FIELDS}


def is_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    return isinstance(value, float) and math.isfinite(value) and value >= 0


def validate_questions(value: object) -> None:
    if not isinstance(value, list):
        raise SessionDataError("manager session has invalid question metadata")
    for question in value:
        if not isinstance(question, dict) or set(question) != {
                "id", "question", "options"} or \
                not isinstance(question["id"], str) or \
                not isinstance(question["question"], str) or \
                not isinstance(question["options"], list):
            raise SessionDataError("manager session has invalid question metadata")
        for option in question["options"]:
            if not isinstance(option, dict) or set(option) != {
                    "label", "detail", "other"} or \
                    not isinstance(option["label"], str) or \
                    not isinstance(option["detail"], str) or \
                    not isinstance(option["other"], bool):
                raise SessionDataError("manager session has invalid question metadata")


def validate_history(value: object) -> None:
    if not isinstance(value, list):
        raise SessionDataError("manager session has invalid history")
    base_fields = {"role", "text", "ts"}
    ui_fields = {"questions", "answered", "answers"}
    for entry in value:
        if not isinstance(entry, dict) or set(entry) not in (
                base_fields, base_fields | ui_fields) or \
                entry.get("role") not in ("user", "manager") or \
                not isinstance(entry.get("text"), str) or \
                not is_number(entry.get("ts")):
            raise SessionDataError("manager session has invalid history")
        if set(entry) == base_fields:
            continue
        answers = entry["answers"]
        if entry["role"] != "manager" or \
                not isinstance(entry["answered"], bool) or \
                not isinstance(answers, dict) or \
                any(not isinstance(key, str) or not isinstance(answer, str)
                    for key, answer in answers.items()):
            raise SessionDataError("manager session has invalid history metadata")
        validate_questions(entry["questions"])


def validate_session_data(data: object, expected_sid: str = "") -> dict:
    if not isinstance(data, dict) or set(data) != set(DISK_FIELDS) or \
            type(data.get("schema_version")) is not int or \
            data.get("schema_version") != 1:
        raise SessionDataError("manager session has an invalid schema")
    try:
        sid = validate_session_id(data["session_id"])
        if expected_sid:
            validate_session_id(expected_sid)
    except ValueError as exc:
        raise SessionDataError("manager session has an invalid session id") from exc
    if expected_sid and sid != expected_sid:
        raise SessionDataError("manager session id does not match its file")
    status = data["status"]
    run_id = data["run_id"]
    proposal = data["proposal"]
    if status not in ("active", "started") or \
            not is_number(data["created_at"]) or \
            not is_number(data["updated_at"]) or \
            type(data["questions"]) is not int or data["questions"] < 0 or \
            not isinstance(data["awaiting_confirmation"], bool) or \
            not is_number(data["cost_usd"]) or \
            not isinstance(run_id, str) or \
            (status == "active" and run_id != "") or \
            (status == "started" and re.fullmatch(r"[a-f0-9]{8}", run_id) is None):
        raise SessionDataError("manager session has an invalid schema")
    if proposal is not None and (not isinstance(proposal, dict) or
                                 set(proposal) != {"goal", "constraints"} or
                                 not isinstance(proposal["goal"], str) or
                                 not isinstance(proposal["constraints"], str)):
        raise SessionDataError("manager session has an invalid proposal")
    if data["updated_at"] < data["created_at"] or \
            (data["awaiting_confirmation"] and proposal is None) or \
            (status == "started" and proposal is None):
        raise SessionDataError("manager session has inconsistent state")
    validate_history(data["history"])
    return {key: data[key] for key in DISK_FIELDS}


def save(sessions_dir: str, sess: dict) -> None:
    sess["session_id"] = validate_session_id(sess.get("session_id", ""))
    sess["updated_at"] = round(time.time(), 3)
    write_snapshot(sessions_dir, sess)


def write_snapshot(sessions_dir: str, sess: dict) -> None:
    """Persist durable fields exactly, without advancing updated_at."""
    sess["session_id"] = validate_session_id(sess.get("session_id", ""))
    payload = validate_session_data(disk_dict(sess),
                                    expected_sid=sess["session_id"])
    if not sessions_dir:
        return
    atomic_write(session_path(sessions_dir, sess["session_id"]),
                 json.dumps(payload, ensure_ascii=False, indent=2,
                            allow_nan=False), mode=0o600)


def load_disk(sessions_dir: str, sid: str) -> dict | None:
    path = session_path(sessions_dir, sid)
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SessionDataError(f"manager session is unreadable: {exc}") from exc
    return validate_session_data(data, expected_sid=sid)


def stage(sess: dict) -> dict:
    """Copy durable fields for mutation; retain runtime-only objects by identity."""
    staged = copy.deepcopy(disk_dict(sess))
    for key, value in sess.items():
        if key not in DISK_FIELDS:
            staged[key] = value
    return staged


def public(sess: dict, pending: bool = False) -> dict:
    """给前端的快照。走 disk_dict 是有意的 —— 那里已经把
    不可序列化的 Router 剔掉了，另写一份迟早会漏。"""
    out = disk_dict(sess)
    out["pending"] = bool(pending)
    return out
