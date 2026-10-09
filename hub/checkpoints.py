"""Checkpoints: Agent Hub's own "shadow git" for one work folder.

docs/decisions/change-safety-net.md §3.1. The only module in the project that
runs git, and it knows nothing about the engine, the blackboard or HTTP.

One store per (project, work folder), used with --git-dir + --work-tree, so a
folder's own .git (if it has one) is never touched. One ref per checkpoint,
refs/agenthub/runs/<run id>/<label>, so git's garbage collection never drops a
checkpoint someone may still roll back to. Every git call is an argv list
(never a shell) run with the work folder as cwd.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
import subprocess
import tempfile

from .executors import _which

MAX_NEW_FILES = 20_000          # new (not yet checkpointed) files per snapshot
MAX_NEW_BYTES = 1 << 30         # ... and their total size
MAX_FILE_BYTES = 100 << 20      # a single file this big never enters a checkpoint
GIT_TIMEOUT = 120               # seconds per git call
DEFAULT_EXCLUDES = ("node_modules/", ".venv/", "venv/", "__pycache__/")
# 关掉文件夹自己的 .gitattributes 可能要求的一切转换（text=auto 的换行归一化、
# LFS filter、working-tree-encoding），否则回滚回来的文件不是原样（spec §2）
ATTRIBUTES = "* -text -filter -ident -working-tree-encoding\n"
ZERO = "0" * 40
GITLINK, SYMLINK, EXECUTABLE = "160000", "120000", "100755"
_IDENTITY = ("-c", "user.name=Agent Hub", "-c", "user.email=agenthub@localhost",
             "-c", "commit.gpgsign=false")
_STORE_CONFIG = (("core.bare", "false"), ("core.autocrlf", "false"),
                 ("core.safecrlf", "false"), ("core.longpaths", "true"),
                 ("core.quotepath", "false"), ("gc.autoDetach", "false"),
                 ("maintenance.auto", "false"))
# 父进程环境里的这些变量会把 git 指到别的仓库、别的 index 上
_SCRUB_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
              "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
              "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM")
_REF_PART = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_QUOTED = re.compile(r"'([^']+)'|\"([^\"]+)\"")
_GLOB_SPECIAL = re.compile(r"([\\*?\[!# ])")


class CheckpointError(Exception):
    """git failed or a checkpoint is gone; str(e) is shown to the user as is."""


def available() -> bool:
    """Is git installed? Same lookup as every other CLI (hub/executors.py)."""
    return bool(_which("git"))


def store_path(runtime_dir: str, workdir: str) -> str:
    key = hashlib.sha1(os.path.realpath(workdir).encode("utf-8")).hexdigest()[:12]
    return os.path.join(runtime_dir, "checkpoints", key + ".git")


def ref(run_id: str, label: str) -> str:
    if not (_REF_PART.match(run_id) and _REF_PART.match(label)):
        raise ValueError(f"bad checkpoint ref part: {run_id!r} / {label!r}")
    return f"refs/agenthub/runs/{run_id}/{label}"


def blob_sha(data: bytes) -> str:
    """git's blob id for these bytes: what a checkpoint stores for a file."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def show(path: str) -> str:
    """A path that is safe to keep as JSON text (unencodable characters -> '?')."""
    return path.encode("utf-8", "replace").decode("utf-8")


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB_ENV}
    env.update(LC_ALL="C", LANGUAGE="C", GIT_TERMINAL_PROMPT="0")
    return env


def _run(argv, what: str, cwd=None, data=None, check=True) -> subprocess.CompletedProcess:
    exe = _which("git")
    if not exe:
        raise CheckpointError("没装 git")
    try:
        r = subprocess.run([exe, *argv], cwd=cwd, input=data, capture_output=True,
                           env=_env(), timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise CheckpointError(f"git {what} 超过 {GIT_TIMEOUT} 秒没结束") from None
    except OSError as e:
        raise CheckpointError(f"git 启动失败：{e}") from None
    if check and r.returncode != 0:
        msg = r.stderr.decode("utf-8", "replace").strip()
        raise CheckpointError(f"git {what} 失败：{msg[:300]}")
    return r


def _git(store: str, workdir, cmd: str, *args, pre=(), data=None,
         check=True) -> subprocess.CompletedProcess:
    """One git command against a store; workdir=None for commands that only read
    the store. `pre` holds global options (-c ..., --literal-pathspecs)."""
    argv = ["--git-dir", store]
    if workdir is not None:
        argv += ["--work-tree", workdir]
    return _run([*argv, *pre, cmd, *args], cmd, cwd=workdir, data=data, check=check)


def _rel_inside(path: str, workdir: str) -> str:
    """path relative to workdir when it lies strictly inside it, else ""."""
    root, full = os.path.realpath(workdir), os.path.realpath(path)
    return os.path.relpath(full, root) if full.startswith(root + os.sep) else ""


def _add_excludes(store: str, rels, directory: bool = False) -> None:
    """Append one anchored, literal pattern per workdir-relative path to the
    store's info/exclude, skipping lines that are already there."""
    path = os.path.join(store, "info", "exclude")
    try:
        with open(path, encoding="utf-8") as f:
            have = set(f.read().splitlines())
    except OSError:
        have = set()
    new = []
    for rel in rels:
        line = "/" + _GLOB_SPECIAL.sub(r"\\\1", rel.replace(os.sep, "/"))
        line += "/" if directory else ""
        if line not in have:
            have.add(line)
            new.append(line)
    if new:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(new) + "\n")


def init_store(store: str, workdir: str, exclude=()) -> None:
    """Create the store if needed, then make sure it never checkpoints itself or
    any path in `exclude` (serve.py passes its data dir). Idempotent. The
    attributes file is written last and doubles as the "fully set up" marker,
    so a half-finished init is simply redone. Raises CheckpointError/OSError."""
    if not os.path.isfile(os.path.join(store, "info", "attributes")):
        os.makedirs(os.path.dirname(store), exist_ok=True)
        _run(["init", "-q", "--bare", "--object-format=sha1", store], "init")
        for key, value in _STORE_CONFIG:
            _git(store, None, "config", key, value)
        info = os.path.join(store, "info")
        os.makedirs(info, exist_ok=True)
        with open(os.path.join(info, "exclude"), "w", encoding="utf-8") as f:
            f.write("\n".join(DEFAULT_EXCLUDES) + "\n")
        with open(os.path.join(info, "attributes"), "w", encoding="utf-8") as f:
            f.write(ATTRIBUTES)
    with open(os.path.join(store, "info", "attributes"), "r+", encoding="utf-8") as f:
        if f.read() != ATTRIBUTES:
            f.seek(0)
            f.write(ATTRIBUTES)
            f.truncate()
    inside = [_rel_inside(p, workdir) for p in (store, *exclude) if p]
    _add_excludes(store, [r for r in inside if r], directory=True)


def _size_guard(store: str, workdir: str):
    """Only NEW files are looked at: names from ls-files, sizes from lstat, no
    content read. Files over MAX_FILE_BYTES are excluded for good (spec §3.5);
    the rest must stay under MAX_NEW_FILES / MAX_NEW_BYTES or the snapshot is
    refused. Returns (skipped, problem)."""
    out = _git(store, workdir, "ls-files", "-z", "--others", "--exclude-standard").stdout
    big, shown, count, total = [], [], 0, 0
    for raw in out.split(b"\0"):
        if not raw or raw.endswith(b"/"):     # "sub/" = an untracked nested repository
            continue
        rel = os.fsdecode(raw)
        try:
            size = os.lstat(os.path.join(workdir, rel)).st_size
        except OSError:
            continue
        if size > MAX_FILE_BYTES:
            big.append(rel)
            shown.append({"path": show(rel),
                          "reason": f"超过 {MAX_FILE_BYTES >> 20} MB，不进检查点"})
        else:
            count += 1
            total += size
    if big:
        _add_excludes(store, big)
    if count > MAX_NEW_FILES or total > MAX_NEW_BYTES:
        return shown, (f"文件夹太大（{count} 个文件 / {total / (1 << 30):.1f} GB），"
                       f"把依赖和产物目录加进 .gitignore 后再试")
    return shown, ""


def _add_errors(stderr: bytes) -> list:
    """What `git add --ignore-errors` skipped (locked or unreadable files, nested
    repos without a commit). Best effort: git names the path in quotes."""
    out = []
    for line in stderr.decode("utf-8", "replace").splitlines():
        if line.startswith("error:") or "could not open" in line:
            m = _QUOTED.search(line)
            out.append({"path": (m.group(1) or m.group(2)) if m else "",
                        "reason": line[:200]})
    return out


def snapshot(store: str, workdir: str, ref_name: str, exclude=()) -> dict:
    """Take one checkpoint of workdir under ref_name. Never raises:
    {"commit": sha or "", "skipped": [{"path", "reason"}], "error": "" or why}."""
    try:
        init_store(store, workdir, exclude)
        skipped, problem = _size_guard(store, workdir)
        if problem:
            return {"commit": "", "skipped": skipped, "error": problem}
        added = _git(store, workdir, "add", "-A", "--ignore-errors", check=False)
        if added.returncode not in (0, 1):     # 1 = some files skipped, the rest staged
            msg = added.stderr.decode("utf-8", "replace").strip()
            raise CheckpointError(f"git add 失败：{msg[:300]}")
        skipped += _add_errors(added.stderr)
        tree = _git(store, workdir, "write-tree").stdout.decode().strip()
        label = ref_name.rsplit("/", 1)[-1]
        commit = _git(store, None, "commit-tree", tree, "-m", label,
                      pre=_IDENTITY).stdout.decode().strip()
        _git(store, None, "update-ref", ref_name, commit)
        return {"commit": commit, "skipped": skipped, "error": ""}
    except CheckpointError as e:
        return {"commit": "", "skipped": [], "error": str(e)}
    except OSError as e:
        return {"commit": "", "skipped": [], "error": f"检查点库读写失败：{e}"}


def has(store: str, *commits: str) -> bool:
    """Do all these checkpoint commits still exist? A pruned run or a deleted
    store says no."""
    if not commits or not all(commits) or not os.path.isdir(store):
        return False
    try:
        out = _git(store, None, "cat-file", "--batch-check",
                   data="".join(c + "\n" for c in commits).encode()).stdout.decode()
    except CheckpointError:
        return False
    lines = out.splitlines()
    return len(lines) == len(commits) and all(l.split()[1:2] == ["commit"] for l in lines)


def _diff(store: str, before: str, after: str) -> list:
    """Raw per-file differences between two checkpoints: path, status (A/M/D/T),
    modes and blob ids on both sides (ZERO for a side that has no file)."""
    raw = _git(store, None, "diff-tree", "-r", "-z", "--no-renames", before, after).stdout
    parts, out, i = raw.split(b"\0"), [], 0
    while i + 1 < len(parts):
        meta = parts[i].decode("ascii", "replace")
        if not meta.startswith(":"):
            i += 1
            continue
        src_mode, dst_mode, src_sha, dst_sha, status = meta[1:].split(" ")
        out.append({"path": os.fsdecode(parts[i + 1]), "status": status[:1],
                    "src_mode": src_mode, "dst_mode": dst_mode,
                    "src_sha": src_sha, "dst_sha": dst_sha})
        i += 2
    return out


def _numstat(store: str, before: str, after: str) -> dict:
    raw = _git(store, None, "diff-tree", "-r", "-z", "--no-renames", "--numstat",
               before, after).stdout
    stats = {}
    for rec in raw.split(b"\0"):
        fields = rec.split(b"\t", 2)
        if len(fields) == 3:
            stats[os.fsdecode(fields[2])] = (fields[0], fields[1])
    return stats


def changes(store: str, before: str, after: str) -> list:
    """Files that differ between two checkpoints, sorted by path. A type change
    (file <-> symlink) counts as modified. Raises CheckpointError."""
    stats = _numstat(store, before, after)
    out = []
    for d in _diff(store, before, after):
        added, deleted = stats.get(d["path"], (b"0", b"0"))
        binary = added == b"-"
        out.append({"path": d["path"], "status": "M" if d["status"] == "T" else d["status"],
                    "added": 0 if binary else int(added),
                    "deleted": 0 if binary else int(deleted),
                    "binary": binary,
                    "nested": GITLINK in (d["src_mode"], d["dst_mode"])})
    return sorted(out, key=lambda f: f["path"])


def counts(files) -> dict:
    return {"added": sum(f["status"] == "A" for f in files),
            "modified": sum(f["status"] == "M" for f in files),
            "deleted": sum(f["status"] == "D" for f in files)}


def file_diff(store: str, before: str, after: str, path: str) -> str:
    """Unified diff of one path between two checkpoints. Plumbing only, so the
    user's diff settings (external tools, textconv, colour) never apply."""
    r = _git(store, None, "diff-tree", "-p", "--no-renames", "--no-ext-diff",
             "--no-textconv", before, after, "--", path, pre=("--literal-pathspecs",))
    return r.stdout.decode("utf-8", "replace")


class _BlobReader:
    """One `git cat-file --batch` process that hands out one blob at a time --
    file content is streamed, never put on a command line (spec §5.2)."""

    def __init__(self, store: str):
        exe = _which("git")
        if not exe:
            raise CheckpointError("没装 git")
        try:
            self._p = subprocess.Popen([exe, "--git-dir", store, "cat-file", "--batch"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, env=_env())
        except OSError as e:
            raise CheckpointError(f"git 启动失败：{e}") from None

    def read(self, sha: str) -> bytes:
        try:
            self._p.stdin.write(sha.encode("ascii") + b"\n")
            self._p.stdin.flush()
            header = self._p.stdout.readline().split()
            if len(header) != 3 or header[1] != b"blob":
                raise CheckpointError(f"检查点里找不到内容 {sha[:10]}，检查点可能被删了")
            expected_size = int(header[2])
            data = self._p.stdout.read(expected_size)
            if len(data) != expected_size:
                raise CheckpointError(f"blob {sha[:10]} 读取不完整：期望 {expected_size} 字节，实际 {len(data)} 字节")
            trailing_byte = self._p.stdout.read(1)
            if trailing_byte != b"\n":
                raise CheckpointError(f"blob {sha[:10]} 尾字节错误：期望 b'\\n'，实际 {trailing_byte!r}")
        except CheckpointError:
            raise
        except OSError as e:
            raise CheckpointError(f"git 通信失败：{e}") from None
        # Verify the data's sha matches the requested sha
        actual_sha = blob_sha(data)
        if actual_sha != sha:
            raise CheckpointError(f"blob {sha[:10]} 内容不匹配：期望 {sha}，实际 {actual_sha}")
        return data

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._p.stdin.close()
        try:
            self._p.wait(timeout=GIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            self._p.kill()


def _blocker(root: str, d: dict, is_creation: bool = False) -> str:
    """Why this file must not be restored; "" means go ahead.
    is_creation=True for phase 1 (deleting created files), False for phase 2 (restoring modified/deleted files)."""
    if GITLINK in (d["src_mode"], d["dst_mode"]):
        return "嵌套的 git 仓库，不回滚"
    if SYMLINK in (d["src_mode"], d["dst_mode"]):
        return "符号链接，不回滚"
    full = os.path.join(root, d["path"])
    real = os.path.realpath(full)
    if not real.startswith(root + os.sep):
        return "路径在工作目录之外，拒绝写入"

    if is_creation:  # phase 1: files the segment created (src_sha=ZERO, need to delete them)
        # Check if file matches dst_sha (what it was after the segment)
        if not os.path.isfile(full) or os.path.islink(full):
            return "之后被删除或移动了，跳过"
        try:
            with open(full, "rb") as f:
                current = blob_sha(f.read())
        except OSError as e:
            return f"读不了：{e.strerror or e}"
        return "" if current == d["dst_sha"] else "之后又被改过，跳过"
    else:  # phase 2: files the segment modified/deleted (src_sha!=ZERO)
        if d["dst_sha"] == ZERO:  # segment deleted it, so to restore we need to recreate
            return "之后又被创建了，跳过" if os.path.lexists(full) else ""
        else:  # segment modified it, so to restore we change it back
            if not os.path.isfile(full) or os.path.islink(full):
                return "之后被删除或移动了，跳过"
            try:
                with open(full, "rb") as f:
                    current = blob_sha(f.read())
            except OSError as e:
                return f"读不了：{e.strerror or e}"
            return "" if current == d["dst_sha"] else "之后又被改过，跳过"


def _prune_empty_dirs(path: str, root: str) -> None:
    """Remove directories a rollback emptied -- never the work folder itself."""
    while path.startswith(root + os.sep):
        try:
            os.rmdir(path)                     # fails, and stops, on the first non-empty one
        except OSError:
            return
        path = os.path.dirname(path)


def _apply(root: str, d: dict, data: bytes) -> None:
    full = os.path.join(root, d["path"])
    if d["src_sha"] == ZERO:                   # this segment created it
        os.remove(full)
        _prune_empty_dirs(os.path.dirname(full), root)
        return
    parent = os.path.dirname(full)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".agenthub-restore-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        current_mode = (os.stat(full).st_mode & 0o777) \
            if os.path.isfile(full) and not os.path.islink(full) else 0o600
        mode = (current_mode | 0o100) if d["src_mode"] == EXECUTABLE \
            else (current_mode & ~0o111)
        os.chmod(tmp, mode)
        os.replace(tmp, full)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def restore(store: str, workdir: str, before: str, after: str, paths=None) -> dict:
    """Put files changed between two checkpoints back the way `before` had them
    -- but only files that are still exactly what `after` left (spec §5.2).
    Anything changed since is skipped with a reason, never overwritten; a file
    that cannot be written is skipped too and the rest still go through."""
    # Check workdir exists first; never recreate it
    if not os.path.isdir(workdir):
        raise CheckpointError(f"工作目录不存在：{workdir}")
    root = os.path.realpath(workdir)

    wanted = None if paths is None else set(paths)
    all_diffs = _diff(store, before, after)

    # Separate into files created (to delete) and files modified (to restore)
    creations = []  # segment created these, so delete them to restore
    modifications = []  # segment modified/deleted these, so restore them

    for d in all_diffs:
        if wanted is not None and d["path"] not in wanted:
            continue
        if d["src_sha"] == ZERO:  # segment created it
            creations.append(d)
        else:  # segment modified or deleted it
            modifications.append(d)

    # Pre-check that all needed blobs exist before modifying anything (skip gitlinks)
    needed_shas = set()
    for d in modifications:
        if d["src_sha"] != ZERO and GITLINK not in (d["src_mode"], d["dst_mode"]):
            needed_shas.add(d["src_sha"])

    if needed_shas:
        try:
            # Use git cat-file --batch-check to verify all blobs exist
            shas_str = "\n".join(sorted(needed_shas))
            r = _git(store, None, "cat-file", "--batch-check",
                    data=shas_str.encode(), check=False)
            if r.returncode != 0:
                raise CheckpointError("检查点库访问失败")
            # Check that all shas are present
            lines = r.stdout.decode("utf-8", "replace").splitlines()
            for line in lines:
                parts = line.split()
                if len(parts) < 2 or parts[1] != "commit" and parts[1] != "blob":
                    raise CheckpointError(f"检查点里找不到需要的内容，检查点可能被删了")
        except CheckpointError:
            raise
        except Exception as e:
            raise CheckpointError(f"检查点库验证失败：{e}") from None

    restored, skipped = [], []
    reader = None

    try:
        # Phase 1: Delete files that the segment created (to restore them)
        for d in creations:
            reason = _blocker(root, d, is_creation=True)
            if reason:
                skipped.append({"path": d["path"], "reason": reason})
            else:
                try:
                    _apply(root, d, b"")  # empty data for deletion
                    restored.append(d["path"])
                except OSError as e:
                    skipped.append({"path": d["path"], "reason": f"写不进去：{e.strerror or e}"})

        # Phase 2: Restore files that the segment modified or deleted
        for d in modifications:
            reason = _blocker(root, d, is_creation=False)
            if reason:
                skipped.append({"path": d["path"], "reason": reason})
            else:
                data = b""
                if d["src_sha"] != ZERO:
                    reader = reader or _BlobReader(store)
                    data = reader.read(d["src_sha"])
                try:
                    _apply(root, d, data)
                    restored.append(d["path"])
                except OSError as e:
                    skipped.append({"path": d["path"], "reason": f"写不进去：{e.strerror or e}"})
    except CheckpointError as e:
        # A later blob may fail after earlier files have already changed.
        # Keep the completed work so the caller can record and retry it safely.
        e.restored = sorted(restored)
        e.skipped = skipped
        raise
    finally:
        if reader is not None:
            reader.close()

    return {"restored": sorted(restored), "skipped": skipped}


def prune(store: str, keep_run_ids) -> int:
    """Retention (spec §5.3): drop the refs of every run not in keep_run_ids and
    let `git gc --auto` reclaim the space. Returns how many refs went."""
    if not os.path.isdir(store):
        return 0
    keep = set(keep_run_ids)
    out = _git(store, None, "for-each-ref", "--format=%(refname)",
               "refs/agenthub/runs/").stdout.decode()
    doomed = [r for r in out.split() if r.split("/")[3] not in keep]
    if doomed:
        _git(store, None, "update-ref", "--stdin",
             data="".join(f"delete {r}\n" for r in doomed).encode())
        _git(store, None, "gc", "--auto", "--quiet")
    return len(doomed)


def clear_stale_lock(store: str) -> bool:
    """Remove the index.lock a killed process left behind. The caller makes sure
    nobody else is using this store right now."""
    try:
        os.remove(os.path.join(store, "index.lock"))
        return True
    except OSError:
        return False
