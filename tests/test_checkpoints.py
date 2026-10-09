"""hub/checkpoints.py -- Agent Hub's shadow git (docs/decisions/change-safety-net.md
§3.1, §7).

Real git in temp dirs. Tests that need git print "skipped" and return when it
is missing, like tests/test_git_diff.py. No framework, plain asserts, zero LLM
calls.

Run: python tests/test_checkpoints.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub import checkpoints

RID = "abcd1234"


def needs_git(fn):
    def wrapper():
        if shutil.which("git") is None:
            print("  (skipped: no git on PATH)")
            return
        fn()
    return wrapper


def _setup():
    tmp = tempfile.mkdtemp(prefix="ah_ckpt_")
    wd = os.path.join(tmp, "wd")
    os.makedirs(wd)
    return tmp, wd, checkpoints.store_path(os.path.join(tmp, "rt"), wd)


def _write(root, rel, data: bytes):
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(data)


def _read(root, rel) -> bytes:
    with open(os.path.join(root, rel), "rb") as f:
        return f.read()


def _tree(store, commit):
    out = subprocess.run(["git", "--git-dir", store, "ls-tree", "-r", "-z",
                          "--name-only", commit],
                         capture_output=True, check=True).stdout
    return sorted(os.fsdecode(p) for p in out.split(b"\0") if p)


def _snap(store, wd, label, exclude=()):
    return checkpoints.snapshot(store, wd, checkpoints.ref(RID, label), exclude)


def test_store_path_is_per_workdir():
    tmp = tempfile.mkdtemp(prefix="ah_ckpt_")
    try:
        a, b = os.path.join(tmp, "a"), os.path.join(tmp, "b")
        os.makedirs(a)
        os.makedirs(b)
        rt = os.path.join(tmp, "rt")
        sa = checkpoints.store_path(rt, a)
        assert sa.startswith(os.path.join(rt, "checkpoints") + os.sep), sa
        assert sa.endswith(".git"), sa
        assert sa != checkpoints.store_path(rt, b)
        assert sa == checkpoints.store_path(rt, os.path.join(b, "..", "a")), \
            "the same folder spelled differently must map to the same store"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_ref_names_are_checked():
    assert checkpoints.ref(RID, "seg1-before") == f"refs/agenthub/runs/{RID}/seg1-before"
    for bad in (("../x", "a"), (RID, "a/b"), (RID, ""), ("", "a")):
        try:
            checkpoints.ref(*bad)
        except ValueError:
            continue
        raise AssertionError(f"ref{bad} must be rejected")


def test_no_git_degrades_cleanly():
    tmp, wd, store = _setup()
    orig = checkpoints._which
    checkpoints._which = lambda name: None
    try:
        assert checkpoints.available() is False
        snap = _snap(store, wd, "seg1-before")
        assert snap["commit"] == "" and "没装 git" in snap["error"], snap
        assert checkpoints.has(store, "a" * 40) is False
    finally:
        checkpoints._which = orig
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_blob_sha_matches_git():
    for data in (b"", b"hello\n", b"a\r\nb\r\n", bytes(range(256))):
        want = subprocess.run(["git", "hash-object", "--stdin"], input=data,
                              capture_output=True, check=True).stdout.decode().strip()
        assert checkpoints.blob_sha(data) == want, data


@needs_git
def test_shadow_store_uses_sha1_despite_git_default_hash():
    tmp, wd, store = _setup()
    old = os.environ.get("GIT_DEFAULT_HASH")
    try:
        os.environ["GIT_DEFAULT_HASH"] = "sha256"
        _write(wd, "a.txt", b"old")
        before = _snap(store, wd, "seg1-before")
        assert len(before["commit"]) == 40, before
        _write(wd, "a.txt", b"new")
        after = _snap(store, wd, "seg1-after")
        assert checkpoints.restore(store, wd, before["commit"], after["commit"])[
            "restored"] == ["a.txt"]
        assert _read(wd, "a.txt") == b"old"
    finally:
        if old is None:
            os.environ.pop("GIT_DEFAULT_HASH", None)
        else:
            os.environ["GIT_DEFAULT_HASH"] = old
        shutil.rmtree(tmp, ignore_errors=True)


def test_restore_replacement_preserves_existing_permissions_and_recreates_privately():
    tmp, wd, _store = _setup()
    original_chmod = os.chmod
    modes = []
    def recording_chmod(path, mode):
        modes.append(mode)
        original_chmod(path, mode)
    try:
        _write(wd, "existing.txt", b"new")
        existing_mode = os.stat(os.path.join(wd, "existing.txt")).st_mode & 0o777
        checkpoints.os.chmod = recording_chmod
        for rel in ("existing.txt", "deleted.txt"):
            checkpoints._apply(wd, {"path": rel, "src_sha": "1", "src_mode": "100644"}, b"old")
        checkpoints._apply(wd, {"path": "existing.txt", "src_sha": "1",
                                "src_mode": checkpoints.EXECUTABLE}, b"old")
        assert modes == [existing_mode & ~0o111, 0o600, existing_mode | 0o100], modes
    finally:
        checkpoints.os.chmod = original_chmod
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_snapshot_records_files_and_respects_ignores():
    tmp, wd, store = _setup()
    try:
        _write(wd, "a.txt", b"a")
        _write(wd, "sub/b.txt", b"b")
        _write(wd, ".gitignore", b"*.log\n")
        _write(wd, "debug.log", b"x")
        _write(wd, "node_modules/lib.js", b"x")
        snap = _snap(store, wd, "seg1-before")
        assert snap["error"] == "" and len(snap["commit"]) == 40, snap
        assert _tree(store, f"refs/agenthub/runs/{RID}/seg1-before") == \
            [".gitignore", "a.txt", "sub/b.txt"]
        assert checkpoints.has(store, snap["commit"])
        assert not checkpoints.has(store, snap["commit"], "0" * 40)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_the_folders_own_repo_is_untouched():
    tmp, wd, store = _setup()
    try:
        def g(*a):
            return subprocess.run(["git", "-C", wd, *a], capture_output=True,
                                  check=True).stdout
        g("init", "-q")
        g("config", "user.email", "t@t")
        g("config", "user.name", "t")
        _write(wd, "a.txt", b"a")
        g("add", "a.txt")
        g("commit", "-qm", "init")
        _write(wd, "b.txt", b"untracked")
        head, status = g("rev-parse", "HEAD"), g("status", "--porcelain")
        snap = _snap(store, wd, "seg1-before")
        assert snap["commit"], snap
        assert g("rev-parse", "HEAD") == head and g("status", "--porcelain") == status
        assert g("for-each-ref", "refs/agenthub") == b"", "no ref may leak into the folder's repo"
        assert _tree(store, snap["commit"]) == ["a.txt", "b.txt"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_nested_repo_without_commit_is_not_fatal():
    tmp, wd, store = _setup()
    try:
        _write(wd, "a.txt", b"a")
        subprocess.run(["git", "init", "-q", os.path.join(wd, "nest")], check=True)
        _write(wd, "nest/f.txt", b"x")
        snap = _snap(store, wd, "seg1-before")
        assert snap["commit"], snap
        assert _tree(store, snap["commit"]) == ["a.txt"]
        assert any("nest" in s["path"] for s in snap["skipped"]), snap["skipped"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_too_many_new_files_refuses_with_a_reason():
    tmp, wd, store = _setup()
    orig = checkpoints.MAX_NEW_FILES
    checkpoints.MAX_NEW_FILES = 3
    try:
        for i in range(5):
            _write(wd, f"f{i}.txt", b"x")
        snap = _snap(store, wd, "seg1-before")
        assert snap["commit"] == "" and "文件夹太大（5 个文件" in snap["error"], snap
        assert ".gitignore" in snap["error"], "the reason must say what to do"
    finally:
        checkpoints.MAX_NEW_FILES = orig
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_big_file_never_enters_and_is_reported():
    tmp, wd, store = _setup()
    orig = checkpoints.MAX_FILE_BYTES
    checkpoints.MAX_FILE_BYTES = 10
    try:
        _write(wd, "small.txt", b"x")
        _write(wd, "#大 文件 [1].bin", b"y" * 20)     # the exclude line must be literal
        snap = _snap(store, wd, "seg1-before")
        assert snap["commit"] and _tree(store, snap["commit"]) == ["small.txt"], snap
        assert [s["path"] for s in snap["skipped"]] == ["#大 文件 [1].bin"], snap["skipped"]
        again = _snap(store, wd, "seg1-after")
        assert _tree(store, again["commit"]) == ["small.txt"]
        assert again["skipped"] == [], "excluded for good -- not reported a second time"
    finally:
        checkpoints.MAX_FILE_BYTES = orig
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_store_and_excluded_paths_never_enter():
    """Review Focus 3: the work folder contains Agent Hub's own data dir."""
    tmp = tempfile.mkdtemp(prefix="ah_ckpt_")
    try:
        wd = os.path.join(tmp, "wd")
        data = os.path.join(wd, ".agenthub")
        store = checkpoints.store_path(os.path.join(data, "default"), wd)
        _write(wd, "a.txt", b"a")
        _write(data, "credentials.yaml", b"secret")
        _write(data, f"default/runs/{RID}/events.jsonl", b"{}")
        snap = _snap(store, wd, "seg1-before", exclude=(data,))
        assert _tree(store, snap["commit"]) == ["a.txt"], _tree(store, snap["commit"])
        _write(data, f"default/runs/{RID}/events.jsonl", b"{}\n{}")   # the engine keeps writing
        after = _snap(store, wd, "seg1-after")          # no exclude passed this time
        assert _tree(store, after["commit"]) == ["a.txt"], "exclusions persist in the store"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_changes_list_added_modified_deleted_with_line_counts():
    tmp, wd, store = _setup()
    try:
        _write(wd, "a.txt", b"one\ntwo\n")
        _write(wd, "gone.txt", b"bye\n")
        before = _snap(store, wd, "seg1-before")["commit"]
        _write(wd, "a.txt", b"one\nTWO\nthree\n")
        os.remove(os.path.join(wd, "gone.txt"))
        _write(wd, "new.bin", bytes(range(256)))
        after = _snap(store, wd, "seg1-after")["commit"]
        files = checkpoints.changes(store, before, after)
        assert files == [
            {"path": "a.txt", "status": "M", "added": 2, "deleted": 1,
             "binary": False, "nested": False},
            {"path": "gone.txt", "status": "D", "added": 0, "deleted": 1,
             "binary": False, "nested": False},
            {"path": "new.bin", "status": "A", "added": 0, "deleted": 0,
             "binary": True, "nested": False},
        ], files
        assert checkpoints.counts(files) == {"added": 1, "modified": 1, "deleted": 1}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_changes_handle_unusual_names():
    """Review Focus 2."""
    tmp, wd, store = _setup()
    names = ["中文 名.md", "#hash.txt", "!bang.txt", "sq [1].txt", "dir with space/x.txt"]
    try:
        before = _snap(store, wd, "seg1-before")["commit"]
        for n in names:
            _write(wd, n, b"x\n")
        after = _snap(store, wd, "seg1-after")["commit"]
        got = [f["path"] for f in checkpoints.changes(store, before, after)]
        assert got == sorted(names), got
        assert "+x" in checkpoints.file_diff(store, before, after, "中文 名.md")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_nested_repo_with_a_commit_is_flagged():
    tmp, wd, store = _setup()
    try:
        nest = os.path.join(wd, "nest")

        def g(*a):
            subprocess.run(["git", "-C", nest, *a], capture_output=True, check=True)
        subprocess.run(["git", "init", "-q", nest], check=True)
        g("config", "user.email", "t@t")
        g("config", "user.name", "t")
        _write(nest, "f.txt", b"1")
        g("add", "f.txt")
        g("commit", "-qm", "one")
        before = _snap(store, wd, "seg1-before")["commit"]
        _write(nest, "f.txt", b"2")
        g("commit", "-qam", "two")
        after = _snap(store, wd, "seg1-after")["commit"]
        files = checkpoints.changes(store, before, after)
        assert [(f["path"], f["nested"]) for f in files] == [("nest", True)], files
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_file_diff_is_one_literal_path():
    tmp, wd, store = _setup()
    try:
        _write(wd, "a.txt", b"a1\n")
        _write(wd, "b.txt", b"b1\n")
        before = _snap(store, wd, "seg1-before")["commit"]
        _write(wd, "a.txt", b"a2\n")
        _write(wd, "b.txt", b"b2\n")
        after = _snap(store, wd, "seg1-after")["commit"]
        d = checkpoints.file_diff(store, before, after, "a.txt")
        assert "-a1" in d and "+a2" in d and "b.txt" not in d, d
        # without --literal-pathspecs "*.txt" would glob-match both files
        assert checkpoints.file_diff(store, before, after, "*.txt") == ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _restore_setup():
    """before: crlf.txt, bin.dat, keep.txt, 中文 名.md -- the segment then
    modifies crlf.txt and 中文 名.md, deletes bin.dat, creates new/deep/x.txt."""
    tmp, wd, store = _setup()
    _write(wd, "crlf.txt", b"a\r\nb\r\n")
    _write(wd, "bin.dat", bytes(range(256)))
    _write(wd, "keep.txt", b"keep")
    _write(wd, "中文 名.md", b"v1\n")
    before = _snap(store, wd, "seg1-before")["commit"]
    _write(wd, "crlf.txt", b"changed\n")
    _write(wd, "中文 名.md", b"v2\n")
    os.remove(os.path.join(wd, "bin.dat"))
    _write(wd, "new/deep/x.txt", b"x")
    after = _snap(store, wd, "seg1-after")["commit"]
    return tmp, wd, store, before, after


@needs_git
def test_restore_roundtrip_is_byte_exact():
    tmp, wd, store, before, after = _restore_setup()
    try:
        res = checkpoints.restore(store, wd, before, after)
        assert res["skipped"] == [], res
        assert res["restored"] == sorted(["bin.dat", "crlf.txt", "new/deep/x.txt", "中文 名.md"])
        assert _read(wd, "crlf.txt") == b"a\r\nb\r\n"
        assert _read(wd, "bin.dat") == bytes(range(256))
        assert _read(wd, "中文 名.md") == b"v1\n"
        assert not os.path.exists(os.path.join(wd, "new")), "emptied dirs are removed"
        assert os.path.isdir(wd), "never the work folder itself"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_restore_skips_files_changed_after_the_segment():
    tmp, wd, store, before, after = _restore_setup()
    try:
        _write(wd, "crlf.txt", b"edited by hand later")
        _write(wd, "bin.dat", b"came back")
        res = checkpoints.restore(store, wd, before, after)
        reasons = {s["path"]: s["reason"] for s in res["skipped"]}
        assert "之后又被改过" in reasons["crlf.txt"], reasons
        assert "之后又被创建了" in reasons["bin.dat"], reasons
        assert _read(wd, "crlf.txt") == b"edited by hand later", "never overwrite later work"
        assert _read(wd, "中文 名.md") == b"v1\n", "the other files still go back"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_restore_only_requested_paths():
    tmp, wd, store, before, after = _restore_setup()
    try:
        res = checkpoints.restore(store, wd, before, after, paths=["crlf.txt"])
        assert res == {"restored": ["crlf.txt"], "skipped": []}, res
        assert os.path.exists(os.path.join(wd, "new", "deep", "x.txt"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_restore_ignores_the_folders_gitattributes():
    """Review Focus 1: text=auto / eol=crlf / an LFS filter in the folder's own
    .gitattributes must not change a single byte on the way back."""
    tmp, wd, store = _setup()
    try:
        _write(wd, ".gitattributes", b"* text=auto eol=crlf\n*.dat filter=lfs diff=lfs merge=lfs -text\n")
        _write(wd, "lf.txt", b"unix\nlines\n")
        _write(wd, "crlf.txt", b"dos\r\nlines\r\n")
        _write(wd, "x.dat", b"\x00raw\r\n\x01")
        before = _snap(store, wd, "seg1-before")["commit"]
        for rel in ("lf.txt", "crlf.txt", "x.dat"):
            _write(wd, rel, b"agent rewrote this")
        after = _snap(store, wd, "seg1-after")["commit"]
        checkpoints.restore(store, wd, before, after)
        assert _read(wd, "lf.txt") == b"unix\nlines\n"
        assert _read(wd, "crlf.txt") == b"dos\r\nlines\r\n"
        assert _read(wd, "x.dat") == b"\x00raw\r\n\x01"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_restore_ignores_working_tree_encoding_attribute():
    tmp, wd, store = _setup()
    try:
        _write(wd, ".gitattributes", b"*.txt working-tree-encoding=UTF-16LE\n")
        original = "before\n".encode("utf-16-le")
        _write(wd, "encoded.txt", original)
        first = _snap(store, wd, "seg1-before")
        assert first["commit"], first
        with open(os.path.join(store, "info", "attributes"), "w", encoding="utf-8") as f:
            f.write("* -text -filter -ident\n")  # an older shadow store
        _write(wd, "encoded.txt", "after\n".encode("utf-16-le"))
        second = _snap(store, wd, "seg1-after")
        assert second["commit"], second
        result = checkpoints.restore(store, wd, first["commit"], second["commit"])
        assert result["restored"] == ["encoded.txt"] and _read(wd, "encoded.txt") == original
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_restore_skips_nested_repo():
    tmp, wd, store = _setup()
    try:
        nest = os.path.join(wd, "nest")

        def g(*a):
            subprocess.run(["git", "-C", nest, *a], capture_output=True, check=True)
        subprocess.run(["git", "init", "-q", nest], check=True)
        g("config", "user.email", "t@t")
        g("config", "user.name", "t")
        _write(nest, "f.txt", b"1")
        g("add", "f.txt")
        g("commit", "-qm", "one")
        before = _snap(store, wd, "seg1-before")["commit"]
        _write(nest, "f.txt", b"2")
        g("commit", "-qam", "two")
        after = _snap(store, wd, "seg1-after")["commit"]
        res = checkpoints.restore(store, wd, before, after)
        assert res["restored"] == [] and "嵌套" in res["skipped"][0]["reason"], res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_restore_refuses_paths_that_now_lead_outside():
    tmp, wd, store = _setup()
    outside = os.path.join(tmp, "outside")
    try:
        _write(wd, "sub/x.txt", b"v1")
        before = _snap(store, wd, "seg1-before")["commit"]
        _write(wd, "sub/x.txt", b"v2")
        after = _snap(store, wd, "seg1-after")["commit"]
        # after the run, sub/ became a link to a folder outside the work folder
        _write(outside, "x.txt", b"v2")
        shutil.rmtree(os.path.join(wd, "sub"))
        try:
            os.symlink(outside, os.path.join(wd, "sub"), target_is_directory=True)
        except (OSError, NotImplementedError):
            print("  (skipped: this machine cannot create symlinks)")
            return
        res = checkpoints.restore(store, wd, before, after)
        assert res["restored"] == [] and "工作目录之外" in res["skipped"][0]["reason"], res
        assert _read(outside, "x.txt") == b"v2", "nothing outside may be written"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_restore_skips_unwritable_file_and_continues():
    """Review Focus 5: a locked (Windows) or read-only (POSIX) file is skipped
    with its reason; every other file still goes back."""
    if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
        print("  (skipped: root ignores permissions)")
        return
    tmp, wd, store = _setup()
    locked_dir = os.path.join(wd, "locked")
    holder = None
    try:
        _write(wd, "locked/a.txt", b"a1")
        _write(wd, "z.txt", b"z1")
        before = _snap(store, wd, "seg1-before")["commit"]
        _write(wd, "locked/a.txt", b"a2")
        _write(wd, "z.txt", b"z2")
        after = _snap(store, wd, "seg1-after")["commit"]
        if os.name == "nt":
            holder = open(os.path.join(locked_dir, "a.txt"), "rb")   # another program has it open
        else:
            os.chmod(locked_dir, 0o500)
        res = checkpoints.restore(store, wd, before, after)
        assert res["restored"] == ["z.txt"], res
        assert [s["path"] for s in res["skipped"]] == ["locked/a.txt"], res
        assert "写不进去" in res["skipped"][0]["reason"], res
        assert _read(wd, "z.txt") == b"z1"
        # Verify no temp file left behind
        temp_files = [f for f in os.listdir(locked_dir) if f.startswith(".agenthub-restore-")]
        assert temp_files == [], f"temp files left behind: {temp_files}"
    finally:
        if holder:
            holder.close()
        if os.name != "nt":
            os.chmod(locked_dir, 0o700)
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_restore_raises_when_blob_is_missing():
    """Blob was deleted from the store; restore raises CheckpointError and changes nothing."""
    tmp, wd, store = _setup()
    try:
        _write(wd, "a.txt", b"a1")
        before = _snap(store, wd, "seg1-before")["commit"]
        _write(wd, "a.txt", b"a2")
        _write(wd, "created.txt", b"new file")  # segment also creates a file
        after = _snap(store, wd, "seg1-after")["commit"]
        diffs = checkpoints._diff(store, before, after)
        a_sha = next(d["src_sha"] for d in diffs if d["path"] == "a.txt")

        # Actually delete the blob object from the store
        obj_path = os.path.join(store, "objects", a_sha[:2], a_sha[2:])
        try:
            os.chmod(obj_path, 0o666)
        except OSError:
            pass
        os.remove(obj_path)

        try:
            res = checkpoints.restore(store, wd, before, after)
            raise AssertionError(f"restore should have raised CheckpointError, got {res}")
        except checkpoints.CheckpointError:
            pass

        # Verify a.txt was not changed
        assert _read(wd, "a.txt") == b"a2"
        # Verify created.txt still exists (wasn't deleted in phase 1 before error)
        assert _read(wd, "created.txt") == b"new file"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_restore_raises_on_wrong_content_blob():
    """If blob data doesn't match its promised hash, raise CheckpointError."""
    tmp, wd, store = _setup()
    try:
        # Create two files
        _write(wd, "file.txt", b"hello world")
        _write(wd, "other.txt", b"different content here")
        before = _snap(store, wd, "seg1-before")["commit"]
        # Modify both in the segment
        _write(wd, "file.txt", b"changed")
        _write(wd, "other.txt", b"also changed")
        after = _snap(store, wd, "seg1-after")["commit"]
        diffs = checkpoints._diff(store, before, after)
        file_sha = next(d["src_sha"] for d in diffs if d["path"] == "file.txt")
        other_sha = next(d["src_sha"] for d in diffs if d["path"] == "other.txt")

        # Overwrite the file.txt blob's object with other.txt's blob object
        file_obj_path = os.path.join(store, "objects", file_sha[:2], file_sha[2:])
        other_obj_path = os.path.join(store, "objects", other_sha[:2], other_sha[2:])

        # Clear read-only flag if any
        try:
            os.chmod(file_obj_path, 0o666)
        except OSError:
            pass

        # Overwrite with other blob
        with open(other_obj_path, "rb") as f:
            other_data = f.read()
        with open(file_obj_path, "wb") as f:
            f.write(other_data)

        try:
            res = checkpoints.restore(store, wd, before, after)
            raise AssertionError(f"restore should have raised CheckpointError, got {res}")
        except checkpoints.CheckpointError:
            pass

        # Verify file was not changed
        assert _read(wd, "file.txt") == b"changed"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_restore_raises_when_workdir_is_missing():
    """If workdir doesn't exist, raise CheckpointError before touching anything."""
    tmp, wd, store = _setup()
    try:
        _write(wd, "a.txt", b"a1")
        before = _snap(store, wd, "seg1-before")["commit"]
        _write(wd, "a.txt", b"a2")
        after = _snap(store, wd, "seg1-after")["commit"]
        # Delete the workdir
        shutil.rmtree(wd)
        try:
            res = checkpoints.restore(store, wd, before, after)
            raise AssertionError(f"restore should have raised CheckpointError, got {res}")
        except checkpoints.CheckpointError:
            pass
        # Verify workdir was NOT recreated
        assert not os.path.exists(wd), "workdir should not be recreated"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_restore_handles_file_to_dir_and_dir_to_file_swaps():
    """In one segment, a file becomes a directory or vice versa; restore puts back the before state."""
    tmp, wd, store = _setup()
    try:
        # Scenario 1: file -> directory
        _write(wd, "path1", b"was a file")
        before = _snap(store, wd, "seg1-before")["commit"]
        os.remove(os.path.join(wd, "path1"))
        _write(wd, "path1/file.txt", b"now in a dir")
        after = _snap(store, wd, "seg1-after")["commit"]
        res = checkpoints.restore(store, wd, before, after)
        assert "path1" in res["restored"], f"path1 should be restored: {res}"
        assert _read(wd, "path1") == b"was a file"
        assert not os.path.isdir(os.path.join(wd, "path1"))

        # Scenario 2: directory -> file
        os.remove(os.path.join(wd, "path1"))
        os.makedirs(os.path.join(wd, "path2", "subdir"))
        _write(wd, "path2/subdir/file.txt", b"was a dir")
        before = _snap(store, wd, "seg2-before")["commit"]
        shutil.rmtree(os.path.join(wd, "path2"))
        _write(wd, "path2", b"now a file")
        after = _snap(store, wd, "seg2-after")["commit"]
        res = checkpoints.restore(store, wd, before, after)
        assert "path2" in res["restored"], f"path2 should be restored: {res}"
        assert os.path.isdir(os.path.join(wd, "path2"))
        assert _read(wd, "path2/subdir/file.txt") == b"was a dir"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_blobreader_raises_on_wrong_trailing_byte():
    """_BlobReader raises CheckpointError when trailing byte is not \\n."""
    class FakeStdin:
        def write(self, data):
            pass
        def flush(self):
            pass

    class FakeProcess:
        def __init__(self, wrong_byte):
            self.stdin = FakeStdin()
            self.wrong_byte = wrong_byte
            self.call_count = 0

        class FakeStdout:
            def __init__(self, data, wrong_byte):
                self.data = data
                self.wrong_byte = wrong_byte
                self.pos = 0

            def readline(self):
                # Return header line: "sha1_hash blob 5\n"
                return b"abc123 blob 5\n"

            def read(self, n):
                if self.pos == 0:
                    # First call: return the data
                    self.pos = 1
                    return self.data
                else:
                    # Second call: return wrong trailing byte
                    return self.wrong_byte

        @property
        def stdout(self):
            return self.FakeStdout(b"hello", self.wrong_byte)

    # Test wrong trailing byte (not b"\n")
    fake_p = FakeProcess(b"X")
    reader = checkpoints._BlobReader.__new__(checkpoints._BlobReader)
    reader._p = fake_p

    try:
        reader.read("abc123def456def456def456def456def45")
        raise AssertionError("Should have raised CheckpointError for wrong trailing byte")
    except checkpoints.CheckpointError as e:
        assert "尾字节错误" in str(e)


def test_blobreader_raises_on_stdout_error():
    """_BlobReader raises CheckpointError when stdout.read() raises OSError."""
    class FakeStdout:
        def __init__(self, error_on_second=False):
            self.error_on_second = error_on_second
            self.call_count = 0

        def readline(self):
            return b"abc123 blob 5\n"

        def read(self, n):
            self.call_count += 1
            if self.call_count == 1:
                return b"hello"
            else:
                raise OSError("stdout closed")

    class FakeStdin:
        def write(self, data):
            pass
        def flush(self):
            pass

    class FakeProcess:
        def __init__(self):
            self.stdout = FakeStdout()
            self.stdin = FakeStdin()

    fake_p = FakeProcess()
    reader = checkpoints._BlobReader.__new__(checkpoints._BlobReader)
    reader._p = fake_p

    try:
        reader.read("abc123def456def456def456def456def45")
        raise AssertionError("Should have raised CheckpointError for stdout OSError")
    except checkpoints.CheckpointError as e:
        assert "通信失败" in str(e)


@needs_git
def test_restore_refuses_when_subdir_becomes_link_outside():
    """F1: When a subdirectory is replaced by a link to outside workdir, restore must refuse."""
    tmp, wd, store = _setup()
    outside = os.path.join(tmp, "outside")
    try:
        # Create files inside sub/
        _write(wd, "sub/x.txt", b"x1")
        _write(wd, "sub/deep/y.txt", b"y1")
        before = _snap(store, wd, "seg1-before")["commit"]

        # Segment deletes both files and modifies them in other files
        os.remove(os.path.join(wd, "sub/x.txt"))
        os.remove(os.path.join(wd, "sub/deep/y.txt"))
        after = _snap(store, wd, "seg1-after")["commit"]

        # After the run, sub/ is replaced by a link to outside
        os.makedirs(outside, exist_ok=True)
        shutil.rmtree(os.path.join(wd, "sub"))

        # Try to create a junction on Windows or symlink on Unix
        link_path = os.path.join(wd, "sub")
        try:
            if os.name == "nt":
                import _winapi
                _winapi.CreateJunction(outside, link_path)
            else:
                os.symlink(outside, link_path, target_is_directory=True)
        except (OSError, NotImplementedError, ImportError):
            print("  (skipped: this machine cannot create directory links)")
            return

        # Restore should skip the files and not write outside
        res = checkpoints.restore(store, wd, before, after)

        # Both paths should be skipped with the outside-workdir reason
        assert res["restored"] == [], res
        skip_paths = {s["path"]: s["reason"] for s in res["skipped"]}
        assert "sub/x.txt" in skip_paths, f"sub/x.txt should be skipped: {res}"
        assert "sub/deep/y.txt" in skip_paths, f"sub/deep/y.txt should be skipped: {res}"
        assert "工作目录之外" in skip_paths["sub/x.txt"], f"wrong reason: {skip_paths['sub/x.txt']}"

        # Nothing should be written in outside directory
        outside_files = []
        for root_dir, dirs, files in os.walk(outside):
            for f in files:
                outside_files.append(os.path.join(root_dir, f))
        assert outside_files == [], f"files written outside workdir: {outside_files}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_prune_keeps_only_listed_runs():
    tmp, wd, store = _setup()
    try:
        _write(wd, "a.txt", b"a")
        checkpoints.snapshot(store, wd, checkpoints.ref("aaaa0001", "seg1-before"))
        _write(wd, "a.txt", b"b")
        new = checkpoints.snapshot(store, wd, checkpoints.ref("aaaa0002", "seg1-before"))
        assert checkpoints.prune(store, ["aaaa0002"]) == 1
        refs = subprocess.run(["git", "--git-dir", store, "for-each-ref", "--format=%(refname)"],
                              capture_output=True, check=True).stdout.decode().split()
        assert refs == ["refs/agenthub/runs/aaaa0002/seg1-before"], refs
        assert checkpoints.has(store, new["commit"])
        assert checkpoints.prune(store, ["aaaa0002"]) == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_prune_without_a_store_is_a_no_op():
    tmp = tempfile.mkdtemp(prefix="ah_ckpt_")
    try:
        assert checkpoints.prune(os.path.join(tmp, "missing.git"), []) == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_git
def test_stale_index_lock_blocks_until_cleared():
    tmp, wd, store = _setup()
    try:
        _write(wd, "a.txt", b"a")
        assert _snap(store, wd, "seg1-before")["commit"]
        with open(os.path.join(store, "index.lock"), "w"):
            pass                                   # a killed engine left this behind
        _write(wd, "a.txt", b"b")
        blocked = _snap(store, wd, "seg1-after")
        assert blocked["commit"] == "" and "index.lock" in blocked["error"], blocked
        assert checkpoints.clear_stale_lock(store) is True
        assert _snap(store, wd, "seg1-after")["commit"]
        assert checkpoints.clear_stale_lock(store) is False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll checkpoint tests passed.")
