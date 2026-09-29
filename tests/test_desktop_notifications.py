"""Desktop notifications (CLAUDE.md §6.7 P0 item 2), structural guard.

This feature is 100% inside dashboard.html's JS. The repo has no JS/browser
test infrastructure (no package.json, no headless browser), and adding one
for a single feature contradicts the project's zero-build discipline -- so
these tests do NOT verify that a notification actually pops up. They read
dashboard.html's source text and assert the STRUCTURE of the four new
functions and their call sites hasn't silently regressed (guards, call
order, three call sites, four titles). Real-browser behavior is covered by
the manual checklist in the plan doc.

No framework, plain asserts. Zero LLM calls.

Run: python3 tests/test_desktop_notifications.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SRC_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "dashboard.html")


def _src() -> str:
    with open(SRC_PATH, encoding="utf-8") as f:
        return f.read()


def _extract_func(src: str, name: str) -> str:
    """Brace-matched body of `function name(` / `async function name(`.
    String-literal aware so braces inside "..." / '...' / `...` don't
    break the match. Prefers the declaration spelling so a same-named
    CALL appearing earlier in the file doesn't get picked up."""
    for decl in (f"function {name}(", f"async function {name}(", f"const {name}="):
        if decl in src:
            i = src.index(decl)
            j = src.index("{", i)
            break
    else:
        i = src.index(name + "(")
        j = src.index("{", i)
    depth = 0
    k = j
    quote = None
    while k < len(src):
        c = src[k]
        if quote:
            if c == "\\":
                k += 2
                continue
            if c == quote:
                quote = None
            k += 1
            continue
        if src.startswith("//", k):
            newline = src.find("\n", k + 2)
            k = len(src) if newline < 0 else newline + 1
            continue
        if src.startswith("/*", k):
            end = src.find("*/", k + 2)
            if end < 0:
                raise AssertionError(f"unterminated comment in {name}")
            k = end + 2
            continue
        if c in "\"'`":
            quote = c
            k += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[j:k + 1]
        k += 1
    raise AssertionError(f"unbalanced braces in {name}")


def _assert_in(body: str, needle: str, label: str):
    assert needle in body, f"{label}: missing {needle!r}"


def test_state_has_notify_asked():
    src = _src()
    state_start = src.index("const S={sessions:[]")
    state_end = src.index("};", state_start) + 2
    assert "notifyAsked:false" in src[state_start:state_end], \
        "S state object must carry notifyAsked:false"


def test_ensure_notify_permission_guards():
    body = _extract_func(_src(), "ensureNotifyPermission")
    for needle in ("notifySupported()", "S.notifyAsked=true",
                   "Notification.permission", "Notification.requestPermission()"):
        _assert_in(body, needle, "ensureNotifyPermission")
    # the `typeof Notification` existence guard lives in notifySupported()
    supported = _extract_func(_src(), "notifySupported")
    _assert_in(supported, "typeof Notification", "notifySupported")
    # notifyAsked must be set BEFORE the permission check (max once per
    # page lifetime, decided before the result comes back)
    assert body.index("S.notifyAsked=true") < body.index("Notification.permission"), \
        "notifyAsked must be set before the permission check"


def test_notify_run_guards():
    body = _extract_func(_src(), "notifyRun")
    for needle in ("S.mode!==\"live\"", "Notification.permission!==\"granted\"",
                   "document.hidden", "new Notification(",
                   "window.focus()", "n.close()"):
        _assert_in(body, needle, "notifyRun")


def test_trunc_impl():
    body = _extract_func(_src(), "trunc")
    _assert_in(body, "slice(0,n)", "trunc")
    assert "…" in body, "trunc must append an ellipsis"


def test_send_prompt_asks_permission():
    src = _src()
    body = _extract_func(src, "sendPrompt")
    _assert_in(body, "ensureNotifyPermission()", "sendPrompt")
    gate = "if(!text||(S.busy&&S.mode===\"live\")||S.sendPending)return;"
    assert gate in body, "sendPrompt early-return gate must still exist"
    assert body.index("ensureNotifyPermission()") > body.index(gate), \
        "permission request must come AFTER the early-return gate " \
        "(user gesture, not pollLive's setInterval callback)"


def test_ingest_notifies_on_every_terminal_outcome_and_no_more():
    """终态通知点：finished / crashed / cancelled（有会话），外加一处
    无会话兜底（引擎在 plan_created 之前就崩了/被取消，此刻还没有会话
    对象 —— 见 CLAUDE.md §6.15，那个洞让用户看到「收到，马上开工」
    之后永远的沉默）。

    仍然锁死确切数量：多出来的调用点通常意味着同一件事被通知两次，
    而重复的系统通知比没有通知更烦人。
    """
    src = _src()
    body = _extract_func(src, "ingest")
    assert body.count("notifyRun(") == 4, \
        f"expected exactly 4 notifyRun calls (finished/crashed/cancelled " \
        f"+ the no-session fallback), found {body.count('notifyRun(')}"
    for title in ("Agent Hub · 运行完成", "Agent Hub · 有任务失败",
                  "Agent Hub · 引擎异常退出", "Agent Hub · 已取消运行"):
        _assert_in(body, title, "ingest")
    # chat copy must be shared via a `msg` local, not duplicated inline
    for token in ("const msg=", "trunc(sess.goal,50)"):
        _assert_in(body, token, "ingest")


if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll desktop-notifications tests passed.")
