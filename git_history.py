"""Bounded, read-only Git history inspection with exact repository roots."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time

import file_ops


MAX_LOG_COUNT = 100
DEFAULT_LOG_COUNT = 20
MAX_OUTPUT_BYTES = 512_000
DEFAULT_OUTPUT_BYTES = 256_000
MAX_STDERR_BYTES = 16_384
MAX_TIMEOUT_SECONDS = 15.0
DEFAULT_TIMEOUT_SECONDS = 5.0
_REVISION_RE = re.compile(
    r"(?:HEAD|[0-9a-fA-F]{4,64}|(?:refs/(?:heads|tags|remotes)/)?"
    r"[A-Za-z0-9][A-Za-z0-9._/-]*)(?:[~^][0-9]*)*\Z"
)


class GitHistoryError(RuntimeError):
    """A rejected repository, revision, path, or bounded Git invocation."""


def _bounded_int(value, default, minimum, maximum):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _bounded_timeout(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT_SECONDS
    if not value or value != value:
        value = DEFAULT_TIMEOUT_SECONDS
    return max(0.1, min(value, MAX_TIMEOUT_SECONDS))


def validate_revision(value: str) -> str:
    revision = str(value or "HEAD")
    if (
        len(revision) > 256
        or not _REVISION_RE.fullmatch(revision)
        or revision.startswith("-")
        or ".." in revision
        or "//" in revision
        or any(part in {"", ".", ".."} for part in revision.split("/"))
        or revision.endswith(("/", ".lock"))
    ):
        raise GitHistoryError("revision uses unsupported or unsafe syntax")
    return revision


def resolve_repo_root(path=".", *, extra_roots="") -> Path:
    try:
        root = file_ops.resolve_repository_read_path(
            str(path or "."),
            allow_workspace_root=True,
            reject_sensitive=False,
            extra_roots=extra_roots,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise GitHistoryError("repository root rejected: %s" % exc) from exc
    if not root.is_dir():
        raise GitHistoryError("repository root is not a directory: %s" % root)
    # Deliberately do not ask Git to discover a parent repository.  A normal
    # checkout has a .git directory; a linked worktree has a .git file.
    if not (root / ".git").exists():
        raise GitHistoryError(
            "exact repository root must contain .git; upward discovery is disabled"
        )
    if file_ops._is_reparse_point(root / ".git"):
        raise GitHistoryError("repository .git entry must not be a symlink or junction")
    return root


def resolve_path_filter(root: Path, value="") -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) > 4096:
        raise GitHistoryError("path filter exceeds length ceiling")
    if file_ops._foreign_absolute(raw):
        raise GitHistoryError("path filter uses a non-native absolute form")
    candidate = Path(raw).expanduser()
    if candidate.is_absolute() or candidate.drive or candidate.anchor:
        raise GitHistoryError("path filter must be relative to the repository root")
    try:
        resolved = file_ops.resolve_repository_read_path(
            str(root / candidate),
            allow_workspace_root=False,
            reject_sensitive=True,
            extra_roots=str(root),
        )
        relative = resolved.relative_to(root)
    except (OSError, TypeError, ValueError) as exc:
        raise GitHistoryError("path filter rejected: %s" % exc) from exc
    if relative == Path("."):
        raise GitHistoryError("path filter must name a path below the repository root")
    return relative.as_posix()


def _git_executable() -> str:
    executable = shutil.which("git")
    if not executable:
        raise GitHistoryError("git executable was not found on PATH")
    return str(Path(executable).resolve())


def _reader(
    stream, retained: bytearray, ceiling: int, exceeded: threading.Event,
    errors: list[OSError],
):
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            remaining = ceiling + 1 - len(retained)
            if remaining > 0:
                retained.extend(chunk[:remaining])
            if len(retained) > ceiling or len(chunk) > remaining:
                exceeded.set()
    except OSError as exc:
        errors.append(exc)
        exceeded.set()
    finally:
        stream.close()


def _run_git(root: Path, arguments: list[str], *, timeout, max_bytes) -> dict:
    timeout = _bounded_timeout(timeout)
    max_bytes = _bounded_int(
        max_bytes, DEFAULT_OUTPUT_BYTES, 1024, MAX_OUTPUT_BYTES,
    )
    argv = [
        _git_executable(),
        "-c", "core.pager=cat",
        "-c", "pager.log=false",
        "-c", "pager.show=false",
        "-c", "diff.external=",
        "-c", "diff.trustExitCode=false",
        "-c", "diff.renames=false",
        "-c", "diff.algorithm=myers",
        "-c", "core.attributesFile=%s" % os.devnull,
        *arguments,
    ]
    env = os.environ.copy()
    for name in list(env):
        if name in {
            "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR",
            "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_CONFIG", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS",
            "GIT_NAMESPACE", "GIT_REPLACE_REF_BASE", "GIT_EXEC_PATH",
            "GIT_INDEX_FILE", "GIT_GRAFT_FILE", "GIT_SHALLOW_FILE",
        } or name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            env.pop(name, None)
    env.update({
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "GIT_EXTERNAL_DIFF": "",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "LC_ALL": "C",
    })
    process = subprocess.Popen(
        argv,
        cwd=str(root),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout = bytearray()
    stderr = bytearray()
    exceeded = threading.Event()
    reader_errors = []
    readers = [
        threading.Thread(
            target=_reader,
            args=(process.stdout, stdout, max_bytes, exceeded, reader_errors),
            daemon=True,
        ),
        threading.Thread(
            target=_reader,
            args=(process.stderr, stderr, MAX_STDERR_BYTES, exceeded, reader_errors),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout
    timed_out = False
    while process.poll() is None:
        if exceeded.is_set():
            process.kill()
            break
        if time.monotonic() >= deadline:
            timed_out = True
            process.kill()
            break
        time.sleep(0.01)
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    for reader in readers:
        reader.join(timeout=1)
    if timed_out:
        raise GitHistoryError("git history command exceeded timeout ceiling")
    if reader_errors:
        raise GitHistoryError("git history output stream failed")
    truncated = len(stdout) > max_bytes or len(stderr) > MAX_STDERR_BYTES
    stdout = bytes(stdout[:max_bytes])
    stderr = bytes(stderr[:MAX_STDERR_BYTES])
    if process.returncode and not truncated:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise GitHistoryError(
            "git history command failed%s"
            % (": " + detail if detail else "")
        )
    return {
        "argv": argv,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": truncated,
        "timeout_seconds": timeout,
        "max_bytes": max_bytes,
    }


def repo_log(
    path=".", *, revision="HEAD", file_path="", count=DEFAULT_LOG_COUNT,
    timeout=DEFAULT_TIMEOUT_SECONDS, max_bytes=DEFAULT_OUTPUT_BYTES,
    extra_roots="",
) -> dict:
    root = resolve_repo_root(path, extra_roots=extra_roots)
    revision = validate_revision(revision)
    file_path = resolve_path_filter(root, file_path)
    count = _bounded_int(count, DEFAULT_LOG_COUNT, 1, MAX_LOG_COUNT)
    arguments = [
        "log", "--no-decorate", "--no-color", "--no-show-signature", "--no-notes",
        "--max-count=%d" % (count + 1),
        "--format=%H%x00%P%x00%an%x00%ae%x00%aI%x00%s",
        revision,
    ]
    if file_path:
        arguments.extend(["--", file_path])
    result = _run_git(root, arguments, timeout=timeout, max_bytes=max_bytes)
    records = []
    for raw_line in result["stdout"].splitlines():
        fields = raw_line.split(b"\0", 5)
        if len(fields) != 6:
            result["truncated"] = True
            continue
        decoded = [field.decode("utf-8", errors="replace") for field in fields]
        records.append({
            "commit": decoded[0],
            "parents": decoded[1].split() if decoded[1] else [],
            "author": {"name": decoded[2], "email": decoded[3]},
            "authored_at": decoded[4],
            "subject": decoded[5],
        })
    more = len(records) > count
    records = records[:count]
    return {
        "ok": True,
        "repository": str(root),
        "revision": revision,
        "path_filter": file_path or None,
        "count": len(records),
        "limit": count,
        "truncated": bool(result["truncated"] or more),
        "output_bytes": len(result["stdout"]),
        "commits": records,
    }


def repo_show(
    path=".", *, revision="HEAD", file_path="", timeout=DEFAULT_TIMEOUT_SECONDS,
    max_bytes=DEFAULT_OUTPUT_BYTES, extra_roots="",
) -> dict:
    root = resolve_repo_root(path, extra_roots=extra_roots)
    revision = validate_revision(revision)
    file_path = resolve_path_filter(root, file_path)
    arguments = [
        "show", "--no-color", "--no-show-signature", "--no-notes",
        "--no-ext-diff", "--no-textconv", "--no-renames", "--unified=3",
        "--format=%H%x00%P%x00%an%x00%ae%x00%aI%x00%s%x00%B%x00",
        revision,
    ]
    if file_path:
        arguments.extend(["--", file_path])
    result = _run_git(root, arguments, timeout=timeout, max_bytes=max_bytes)
    fields = result["stdout"].split(b"\0", 7)
    if len(fields) != 8:
        raise GitHistoryError("git show output was incomplete before metadata ended")
    decoded = [field.decode("utf-8", errors="replace") for field in fields[:7]]
    patch = fields[7].decode("utf-8", errors="replace").lstrip("\r\n")
    return {
        "ok": True,
        "repository": str(root),
        "revision": revision,
        "path_filter": file_path or None,
        "truncated": bool(result["truncated"]),
        "output_bytes": len(result["stdout"]),
        "commit": decoded[0],
        "parents": decoded[1].split() if decoded[1] else [],
        "author": {"name": decoded[2], "email": decoded[3]},
        "authored_at": decoded[4],
        "subject": decoded[5],
        "message": decoded[6],
        "patch": patch,
    }
