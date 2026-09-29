"""Static contracts for dashboard manager-session restoration.

Run: python3 tests/test_frontend_manager_session_restore.py
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src() -> str:
    with open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8") as f:
        return f.read()


def _between(start_marker: str, end_marker: str) -> str:
    src = _src()
    start = src.index(start_marker)
    end = src.index(end_marker, start + len(start_marker))
    return src[start:end]


def test_boot_restores_before_live_polling():
    body = _between("async function boot(){", "boot();")
    assert "await restoreManagerSessions()" in body
    assert body.index("await restoreManagerSessions()") < body.index('setMode("live")')


def test_archived_chat_is_keyed_by_run_and_not_put_in_prechat():
    body = _between("async function restoreManagerSessions(){",
                    "/* ---------- restored pending turn ---------- */")
    assert "managerChatsByRun" in body
    assert "run_id" in body
    run_branch = body[body.index("data.run"):]
    assert "S.preChat=" not in run_branch, "archived run chat would mix with active draft"


def test_plan_created_consumes_archived_chat_only_once():
    branch = _between('if(e.type==="plan_created"){',
                      '  }else{\n    const sess=S.sessions[S.sessions.length-1];')
    assert "managerChatsByRun" in branch
    assert "delete S.managerChatsByRun" in branch


def test_active_draft_keeps_the_latest_after_old_run_replay():
    branch = _between('if(e.type==="plan_created"){',
                      '  }else{\n    const sess=S.sessions[S.sessions.length-1];')
    assert re.search(r"S\.chatSession\s*\?\s*S\.sessions\.length", branch), \
        "active draft must stay in the slot after the replayed run"


def test_history_conversion_uses_state_not_html_strings():
    body = _between("function historyToChat(history){", "function applyActiveSnapshot")
    assert "text:" in body and "card:" in body
    assert "innerHTML" not in body


def test_history_conversion_tolerates_malformed_snapshot_containers():
    body = _between("function historyToChat(history){", "function applyActiveSnapshot")
    assert "Array.isArray(history)" in body
    assert "Array.isArray(item.questions)" in body


def test_restore_fetch_failure_is_visible_but_non_fatal():
    body = _between("async function restoreManagerSessions(){",
                    "/* ---------- restored pending turn ---------- */")
    catch_branch = body[body.index("catch("):]
    assert "hint(" in catch_branch
    assert "throw" not in catch_branch


def test_active_draft_is_not_rendered_inside_the_latest_run():
    body = _between("function renderChat(sess){", "/* ---------- drawer ---------- */")
    assert "S.chatSession?S.activeIdx===S.sessions.length" in body


def test_pending_restore_polls_get_and_never_posts_chat():
    body = _between("async function pollRestoredManagerTurn(){",
                    "function startRestoredManagerPolling")
    assert 'fetch("/chat/session/current"' in body
    assert 'fetch("/chat"' not in body
    assert "applyActiveSnapshot" in body


def test_polling_stops_and_clears_when_active_session_disappears():
    body = _between("async function pollRestoredManagerTurn(){",
                    "function startRestoredManagerPolling")
    inactive = body[body.rindex("}else{") :]
    assert "stopRestoredManagerPolling()" in inactive
    assert "stopManagerProgress()" in inactive
    # The four assignments used to be inlined here, duplicating
    # clearRestoredDraftState().  They diverged the moment that helper learned
    # to keep local-only rows, so the poll now calls the helper instead.
    assert "clearRestoredDraftState()" in inactive
    helper = _between("function clearRestoredDraftState(){",
                      "async function restoreManagerSessions")
    assert "S.chatSession=null" in helper and "S.sendPending=false" in helper


def test_stale_poll_cannot_resurrect_a_discarded_draft():
    body = _between("async function pollRestoredManagerTurn(){",
                    "function startRestoredManagerPolling")
    assert "expectedSession" in body
    guard = re.search(r"if\([^\n]*S\.chatSession[^\n]*expectedSession[^\n]*\)return;", body)
    assert guard, "poll response must be ignored after the local active SID changes"
    assert guard.start() < body.index("applyActiveSnapshot")


def test_restore_starts_polling_only_for_a_pending_active_turn():
    body = _between("async function restoreManagerSessions(){",
                    "/* ---------- restored pending turn ---------- */")
    assert re.search(r"data\.active[^\n]*pending[^\n]*startRestoredManagerPolling", body)


def test_new_goal_waits_for_discard_before_clearing_state():
    src = _src()
    handler = re.search(r'\$\("#new-btn"\)\.onclick=async\(\)=>\{(.*?)\n\};', src, re.S)
    assert handler, "new goal handler must be async"
    body = handler.group(1)
    assert "await discardActiveDraft()" in body
    assert body.index("await discardActiveDraft()") < body.index("S.preChat=[]")


def test_discard_failure_keeps_the_draft_visible():
    body = _between("async function discardActiveDraft(){", '$("#new-btn").onclick')
    assert "return false" in body
    assert "S.preChat=[]" not in body


def test_discard_is_sid_scoped_and_cannot_clear_a_newer_local_session():
    body = _between("async function discardActiveDraft(){", '$("#new-btn").onclick')
    assert re.search(r"const\s+sessionId\s*=\s*S\.chatSession", body)
    assert "JSON.stringify({session_id:sessionId})" in body
    assert re.search(r"S\.chatSession\s*!==\s*sessionId", body), \
        "an older discard response must not authorize clearing a newer local SID"


def test_discard_errors_are_visible_and_demo_never_hits_the_endpoint():
    """DEMO must not call the discard endpoint -- but the reason is that DEMO
    has no draft, not that the mode flag says so.

    This originally asserted `S.mode!=="demo"` in the click handler.  That
    encoded a defect: `S.chatSession` survives a mode toggle, so a LIVE draft
    carried into DEMO was dropped locally and orphaned on the server, and the
    next LIVE send hit 409.  The guard that actually matters is inside
    discardActiveDraft, which returns early -- without fetching -- when there
    is no session id.  Pure DEMO never assigns one (sendPrompt returns in its
    demo branch before minting it), so the endpoint is still never called.
    """
    src = _src()
    discard = _between("async function discardActiveDraft(){", '$("#new-btn").onclick')
    assert "response.ok" in discard and "hint(" in discard
    guard = discard[:discard.index("fetch(")]
    assert re.search(r"if\(!sessionId\)return true;", guard), \
        "no draft must short-circuit before any request"
    handler = re.search(r'\$\("#new-btn"\)\.onclick=async\(\)=>\{(.*?)\n\};', src, re.S)
    assert handler and "await discardActiveDraft()" in handler.group(1)
    demo_branch = _between("if(S.mode===\"demo\"){", "// @taskid")
    assert "S.chatSession" not in demo_branch, \
        "if DEMO ever mints a session id, this test's premise breaks"


def test_rejected_chat_rehydrates_before_rendering():
    body = re.search(r"async function sendPrompt\(\)\{(.*?)\n\}\n", _src(), re.S).group(1)
    error_branch = body[body.index('if(!res.ok){hint("经理没接上') :]
    assert "await restoreManagerSessions()" in error_branch
    assert error_branch.index("await restoreManagerSessions()") < error_branch.index("renderAll()")


def test_rejected_chat_cannot_unlock_an_authoritative_pending_restore():
    body = re.search(r"async function sendPrompt\(\)\{(.*?)\n\}\n", _src(), re.S).group(1)
    finally_branch = body[body.rindex("}finally{") :]
    # 判据是「此刻还有没有在轮询服务端」，不是某个句柄变量叫什么。
    guard = re.search(r'Timers\.running\("session"\)', finally_branch)
    assert guard, "解锁前必须先问：服务端那一轮还在飞吗"
    assert finally_branch.index('Timers.running("session")') < \
        finally_branch.index("S.sendPending=false")


def test_nested_choice_metadata_is_normalized_before_rendering():
    body = _between("function historyToChat(history){", "function applyActiveSnapshot")
    assert "Array.isArray(q.options)" in body
    assert re.search(r"options:\s*\(Array\.isArray\(q\.options\)", body)
    assert "String(o.label" in body


def test_active_draft_has_a_sidebar_navigation_entry():
    body = _between("function renderSide(){", "/* ---------- office canvas ---------- */")
    assert "S.chatSession" in body
    assert re.search(r"S\.activeIdx\s*=\s*S\.sessions\.length", body)


def test_boot_disables_mutating_controls_until_restore_settles():
    """开机恢复未落定时，草稿区控件必须是禁用的。

    以前断言 renderAll 里出现 S.restoringSessions —— 那是钉死写法。判据
    2026-09-05 收进了 draftLocked()，renderAll 只负责应用，所以这里改成
    断言真正的契约：判据里读了这个标志，且控件走统一登记表。
    """
    src = _src()
    assert "restoringSessions:true" in src, "初始值必须是锁上的"
    predicate = re.search(r"function draftLocked\(\)\{(.*?)\n\}", src, re.S)
    assert predicate and "S.restoringSessions" in predicate.group(1), \
        "草稿锁判据没有考虑开机恢复未落定的情况"
    body = _between("function renderAll(){", "/* ---------- send ---------- */")
    assert "draftLocked()" in body and "DRAFT_CONTROLS" in body, \
        "renderAll 必须走统一判据与登记表"


def test_bfcache_restore_rearms_the_session_poll():
    """pagehide kills the only timer that can clear a restored sendPending.

    The page has no unload/beforeunload handler, so it is bfcache-eligible and
    "进设置再返回" restores it WITHOUT re-running boot().  Without a pageshow
    listener the UI comes back with every control disabled and nothing polling.
    """
    src = _src()
    assert "beforeunload" not in src and '"unload"' not in src, \
        "if the page stops being bfcache-eligible, revisit this contract"
    match = re.search(r'addEventListener\("pageshow",(.*?)\}\);', src, re.S)
    assert match, "a bfcache restore must re-derive state from the server"
    body = match.group(1)
    assert "persisted" in body, "only a bfcache restore needs re-deriving"
    assert "restoreManagerSessions" in body


def test_rejected_chat_gives_the_user_their_text_back():
    src = _src()
    send = re.search(r"async function sendPrompt\(\)\{(.*?)\n\}\n", src, re.S).group(1)
    rejected = send[send.index("if(!res.ok){hint(\"经理没接上"):]
    rejected = rejected[:rejected.index("return;")]
    assert "ta.value=text" in rejected, \
        "a pre-persistence rejection wipes both the textarea and preChat"


def test_new_goal_never_orphans_a_server_draft_in_demo():
    src = _src()
    handler = re.search(r'\$\("#new-btn"\)\.onclick=async\(\)=>\{(.*?)\n\};', src, re.S)
    body = handler.group(1)
    guard = body[:body.index("await discardActiveDraft()")]
    assert 'S.mode!=="demo"' not in guard, \
        "S.chatSession survives a mode toggle, so mode is the wrong test"


def test_snapshots_keep_locally_generated_system_messages():
    """run_crashed/run_cancelled text lives only in preChat (§6.15).

    applyActiveSnapshot replaces preChat wholesale at 1 Hz, so without a carve
    out the crash explanation is erased and the user is back to silence.
    """
    src = _src()
    crash = _between("if(e.type===\"run_crashed\"||e.type===\"run_cancelled\"){",
                     "return;\n    }")
    assert "local:true" in crash, "local-only rows must be taggable"
    apply_body = _between("function applyActiveSnapshot(active){",
                          "function clearRestoredDraftState")
    assert "m.local" in apply_body
    clear_body = _between("function clearRestoredDraftState(){",
                          "async function restoreManagerSessions")
    assert "m.local" in clear_body


def test_invalid_session_id_stops_instead_of_spamming_hints():
    src = _src()
    apply_body = _between("function applyActiveSnapshot(active){",
                          "function clearRestoredDraftState")
    assert "return false" in apply_body and "return true" in apply_body, \
        "the caller must be able to tell a rejected snapshot from an applied one"
    restore = _between("async function restoreManagerSessions(){",
                       "/* ---------- restored pending turn")
    assert "applyActiveSnapshot(data.active)&&" in restore or \
           "if(applyActiveSnapshot(data.active)" in restore, \
        "polling must not start for a snapshot that was rejected"


def test_repeated_poll_failures_stop_and_tell_the_user():
    """A 500 (corrupt session file) otherwise polls forever, silently, with
    every control disabled -- indistinguishable from a slow manager."""
    body = _between("async function pollRestoredManagerTurn(){",
                    "function startRestoredManagerPolling")
    assert "pollFail" in body
    assert "stopRestoredManagerPolling()" in body
    assert "hint(" in body, "a stuck poll must say something"



if __name__ == "__main__":
    for name in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {name} ==")
        globals()[name]()
    print("\nAll frontend manager-session restore tests passed.")
