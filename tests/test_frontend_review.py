"""改动安全网的前端（docs/decisions/change-safety-net.md §5.4）。

源码锚点测试，跟 tests/test_frontend_crash_before_planning.py 一个路数；再用
node 做一次语法检查（没装 node 就诚实跳过，跟 tests/test_frontend_escaping.py
一样）。真正的交互在 Task 16 的浏览器实测里验。

Run: python tests/test_frontend_review.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(name="dashboard.html") -> str:
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def _fn(src: str, head: str) -> str:
    """A top-level function: from its head to the first line that is exactly "}"."""
    i = src.index(head)
    return src[i:src.index("\n}\n", i)]


def test_changes_are_loaded_when_a_run_ends_or_is_rolled_back():
    src = _src()
    body = _fn(src, "async function loadChanges(runId){")
    assert "/run/changes?run=" in body, body
    assert 'S.mode!=="live"' in body
    assert "r.ok&&d&&d.checkpoints?d:{error:" in body
    assert "S.changesVer++;renderAll();" in body
    ingest = _fn(src, "function ingest(e){")
    assert '["run_finished","run_cancelled","run_crashed","run_rolled_back"].includes(e.type)' in ingest
    assert "loadChanges(sess.runId)" in ingest


def test_a_run_without_checkpoints_says_so_up_front():
    src = _src()
    body = _fn(src, "function checkpointNotice(sess,e){")
    assert "e.checkpoints.on===false" in body and "不能一键回滚" in body, body
    assert _fn(src, "function ingest(e){").count("checkpointNotice(") == 2, \
        "fresh runs and reruns both"


def test_task_log_line_carries_its_changes():
    body = _fn(_src(), "function computeView(sess){")
    assert "changes:e.changes||null" in body and "changeText(e.changes)" in body


def test_end_cards_carry_a_changes_row():
    src = _src()
    card = _fn(src, "function cardEl(c,ts){")
    assert 'c.kind==="changes"' in card and card.count("changesRow(c.runId)") == 2, card
    ingest = _fn(src, "function ingest(e){")
    assert ingest.count('card:{kind:"changes",runId:sess.runId}') == 2, "cancel + crash"
    assert "skipped:skipped,runId:sess.runId" in ingest
    row = _fn(src, "function changesRow(runId){")
    assert 'S.mode!=="live"' in row, \
        "DEMO events are fake: there are no changes to show"
    assert "s.error" in row
    assert "innerHTML" not in row


def test_sidebar_tags_changes_and_rollbacks():
    body = _fn(_src(), "function renderSide(){")
    assert "改动 " in body and "已回滚" in body
    assert 'S.mode==="live"?S.changes[' in body
    assert "!r.undone" in body


def test_scripts_still_parse():
    node = shutil.which("node")
    if not node:
        print("  (skipped: no node)")
        return
    for name in ("dashboard.html", "settings.html"):
        scripts = re.findall(r"<script>(.*?)</script>", _src(name), re.S)
        assert scripts, name
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as f:
            f.write("\n;\n".join(scripts))
            path = f.name
        try:
            r = subprocess.run([node, "--check", path], capture_output=True, text=True,
                               timeout=60)
            assert r.returncode == 0, f"{name}: {r.stderr[-800:]}"
        finally:
            os.remove(path)


def test_changes_row_offers_the_review():
    body = _fn(_src(), "function changesRow(runId){")
    assert "审阅改动" in body and "openReview(runId)" in body, body


def test_review_panel_uses_the_three_endpoints():
    src = _src()
    assert "/run/changes/diff?run=" in _fn(src, "async function renderReviewDiff(body,runId,q){")
    assert '"/run/rollback"' in _fn(src, "async function rollbackSelected(runId,paths){")
    assert '"/run/rollback/undo"' in _fn(src, "async function undoRollback(runId){")


def test_rollback_confirmation_says_what_will_be_skipped():
    body = _fn(_src(), "async function rollbackSelected(runId,paths){")
    assert "window.confirm(" in body and "之后又被改过的文件会跳过" in body, body


def test_review_says_your_own_edits_show_up_too():
    body = _fn(_src(), "function renderReviewList(body,runId,d){")
    assert "运行期间你自己改的文件也会出现在这里" in body
    assert "先撤销上一次回滚" in body and "last.pre&&last.post" in body


def test_agent_named_files_never_reach_innerHTML():
    """Agent-influenced paths, reasons and notes only reach textContent."""
    src = _src()
    for head in ("function rvGroup(box,label,files,seg){", "function rvResult(r){",
                 "async function renderReviewDiff(body,runId,q){",
                 "function renderReviewList(body,runId,d){", "function rvNotes(seg){"):
        for line in _fn(src, head).splitlines():
            if "innerHTML" in line:
                assert not re.search(r"\b(path|reason|note|error|title|goal)\b", line), \
                    f"{head}: {line.strip()}"


def test_closing_the_panel_really_closes_it():
    src = _src()
    body = _fn(src, "function renderPreview(){")
    assert 'classList.contains("on")' in body, body
    assert "if(S.review){renderReview();return;}" in body
    close = re.search(r'\$\("#pv-close"\)\.onclick=\(\)=>\{(.*?)\};', src).group(1)
    assert "closeReview()" in close, close
    assert any('e.key==="Escape"' in line and "S.review" in line for line in src.splitlines())
    assert src.count("S.drawer=null;S.preview=null;S.pvDiff=false;pvShown=null;closeReview();") == 3, \
        "switching sessions (twice in renderSide) and the new-goal button close the review"


def test_old_runs_can_be_reviewed_from_the_settings_history():
    src = _src()
    body = _fn(src, "function reviewFromHash(){")
    assert "#review=" in body and "openReview(" in body, body
    assert "reviewFromHash();" in _fn(src, "async function boot(){")
    runs = _fn(_src("settings.html"), "function renderRuns(runs){")
    assert "/dashboard.html#review='+encodeURIComponent(r.run_id" in runs, runs
    assert "改动 " in runs and "已回滚" in runs


def test_a_completed_followup_displays_a_checkpoint_warning():
    body = _fn(_src(), "async function sendPrompt(){")
    assert re.search(r"if\(data\.warning\).*text:data\.warning", body, re.S), body


if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll review frontend tests passed.")
