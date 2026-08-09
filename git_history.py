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
DEFAULT_BLAME_LINES = 100
MAX_BLAME_LINES = 500
MAX_BLAME_LINE_NUMBER = 10_000_000
MAX_OUTPUT_BYTES = 512_000
DEFAULT_OUTPUT_BYTES = 256_000
MAX_STDERR_BYTES = 16_384
MAX_TIMEOUT_SECONDS = 15.0
DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_GITFILE_BYTES = 4096
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


def _validate_git_entry(root: Path, *, extra_roots="") -> None:
    entry = root / ".git"
    if not entry.exists():
        raise GitHistoryError(
            "exact repository root must contain .git; upward discovery is disabled"
        )
    if file_ops._is_reparse_point(entry):
        raise GitHistoryError("repository .git entry must not be a symlink or junction")
    if entry.is_dir():
        return
    if not entry.is_file() or entry.stat().st_size > MAX_GITFILE_BYTES:
        raise GitHistoryError("repository .git file is malformed or oversized")
    try:
        lines = entry.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise GitHistoryError("repository .git file could not be safely read") from exc
    if len(lines) != 1 or not lines[0].lower().startswith("gitdir:"):
        raise GitHistoryError("repository .git file is malformed")
    raw_target = lines[0][len("gitdir:"):].strip()
    if not raw_target or file_ops._foreign_absolute(raw_target):
        raise GitHistoryError("repository gitfile target uses an unsafe path")
    # Git resolves relative gitdir values against the gitfile's directory and
    # does not perform shell-style tilde expansion; validate the same path Git
    # will actually open.
    requested = Path(raw_target)
    if not requested.is_absolute():
        requested = entry.parent / requested
    if file_ops._is_reparse_point(requested):
        raise GitHistoryError("repository gitfile target must not be a symlink or junction")
    target = file_ops._resolve_best_effort(requested)
    authorized_roots = [
        file_ops._resolve_best_effort(candidate)
        for candidate in file_ops.allowed_roots(extra_roots)
    ]
    if not any(
        target == candidate or file_ops._is_inside(target, candidate)
        for candidate in authorized_roots
    ):
        raise GitHistoryError("repository gitfile target is outside authorized roots")
    if not target.is_dir():
        raise GitHistoryError("repository gitfile target is not a directory")


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
    # Deliberately do not ask Git to discover a parent repository. A linked
    # worktree's gitfile is accepted only when its target is independently
    # contained by an operator-authorized root.
    _validate_git_entry(root, extra_roots=extra_roots)
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


def resolve_show_target(root: Path, value) -> str:
    """Require one safe contained path before exposing commit patch content."""
    relative = resolve_path_filter(root, value)
    if not relative:
        raise GitHistoryError("repo_show requires a contained file_path")
    target = root / Path(relative)
    if not target.exists() or not target.is_file():
        raise GitHistoryError("repo_show file_path must be an existing regular file")
    if file_ops._is_reparse_point(target):
        raise GitHistoryError("repo_show file_path must not be a symlink or junction")
    return relative


def resolve_blame_target(root: Path, value) -> str:
    """Resolve one existing regular worktree file for bounded blame."""
    raw = str(value or "").strip()
    if not raw:
        raise GitHistoryError("blame file_path is required")
    relative = resolve_path_filter(root, raw)
    target = root / Path(relative)
    if not target.exists() or not target.is_file():
        raise GitHistoryError("blame target must be an existing regular file")
    if file_ops._is_reparse_point(target):
        raise GitHistoryError("blame target must not be a symlink or junction")
    return relative


def normalize_blame_range(start_line=1, end_line=0) -> tuple[int, int]:
    """Return an explicit, strictly bounded inclusive line range."""
    if isinstance(start_line, bool) or not isinstance(start_line, int):
        raise GitHistoryError("blame start_line must be an integer")
    if start_line < 1 or start_line > MAX_BLAME_LINE_NUMBER:
        raise GitHistoryError("blame start_line is outside the supported range")
    if end_line in (None, 0):
        end_line = start_line + DEFAULT_BLAME_LINES - 1
    elif isinstance(end_line, bool) or not isinstance(end_line, int):
        raise GitHistoryError("blame end_line must be an integer")
    if end_line < start_line or end_line > MAX_BLAME_LINE_NUMBER:
        raise GitHistoryError("blame end_line is outside the supported range")
    if end_line - start_line + 1 > MAX_BLAME_LINES:
        raise GitHistoryError(
            "blame range exceeds the %d-line ceiling" % MAX_BLAME_LINES
        )
    return start_line, end_line


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
    file_path = resolve_show_target(root, file_path)
    timeout_budget = _bounded_timeout(timeout)
    probe_timeout = max(0.1, timeout_budget / 3.0)
    patch_timeout = max(0.1, timeout_budget - probe_timeout)
    object_result = _run_git(
        root, ["cat-file", "-t", revision + "^0:" + file_path],
        timeout=probe_timeout, max_bytes=1024,
    )
    if object_result["truncated"] or object_result["stdout"].strip() != b"blob":
        raise GitHistoryError("repo_show path is not a file at the requested revision")
    arguments = [
        "show", "--no-color", "--no-show-signature", "--no-notes",
        "--no-ext-diff", "--no-textconv", "--no-renames", "--unified=3",
        "--format=%H%x00%P%x00%an%x00%ae%x00%aI%x00%s%x00%B%x00",
        revision + "^0",
    ]
    arguments.extend(["--", file_path])
    result = _run_git(
        root, arguments, timeout=patch_timeout, max_bytes=max_bytes,
    )
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


_BLAME_HEADER_RE = re.compile(
    rb"(?P<commit>[0-9a-f]{40}|[0-9a-f]{64}) "
    rb"(?P<original>[0-9]+) (?P<final>[0-9]+)(?: [0-9]+)?\Z"
)


def _parse_blame_porcelain(payload: bytes, *, output_truncated=False):
    records = []
    header = None
    metadata = {}
    incomplete = False
    for raw_line in payload.splitlines():
        if header is None:
            match = _BLAME_HEADER_RE.fullmatch(raw_line)
            if not match:
                if output_truncated:
                    incomplete = True
                    break
                raise GitHistoryError("git blame output contained a malformed header")
            header = match.groupdict()
            metadata = {}
            continue
        if raw_line.startswith(b"\t"):
            previous = metadata.get("previous", "")
            previous_record = None
            if previous:
                prior_commit, separator, prior_path = previous.partition(" ")
                previous_record = {
                    "commit": prior_commit,
                    "filename": prior_path if separator else "",
                }
            author_email = metadata.get("author-mail", "")
            if author_email.startswith("<") and author_email.endswith(">"):
                author_email = author_email[1:-1]
            records.append({
                "commit": header["commit"].decode("ascii"),
                "original_line": int(header["original"]),
                "final_line": int(header["final"]),
                "author": {
                    "name": metadata.get("author", ""),
                    "email": author_email,
                },
                "author_time": int(metadata.get("author-time", "0")),
                "author_tz": metadata.get("author-tz", ""),
                "summary": metadata.get("summary", ""),
                "filename": metadata.get("filename", ""),
                "previous": previous_record,
                "boundary": "boundary" in metadata,
                "text": raw_line[1:].decode("utf-8", errors="replace"),
            })
            header = None
            metadata = {}
            continue
        key, separator, value = raw_line.partition(b" ")
        decoded_key = key.decode("ascii", errors="replace")
        metadata[decoded_key] = (
            value.decode("utf-8", errors="replace") if separator else ""
        )
    if header is not None:
        if not output_truncated:
            raise GitHistoryError("git blame output ended before a source line")
        incomplete = True
    return records, incomplete


def repo_blame(
    path=".", *, file_path, revision="HEAD", start_line=1, end_line=0,
    timeout=DEFAULT_TIMEOUT_SECONDS, max_bytes=DEFAULT_OUTPUT_BYTES,
    extra_roots="",
) -> dict:
    """Read structured blame records for one explicit bounded file range."""
    root = resolve_repo_root(path, extra_roots=extra_roots)
    revision = validate_revision(revision)
    file_path = resolve_blame_target(root, file_path)
    start_line, end_line = normalize_blame_range(start_line, end_line)
    result = _run_git(
        root,
        [
            "blame", "--line-porcelain", "--no-progress", "--no-textconv",
            "-L", "%d,%d" % (start_line, end_line), revision, "--", file_path,
        ],
        timeout=timeout,
        max_bytes=max_bytes,
    )
    records, incomplete = _parse_blame_porcelain(
        result["stdout"], output_truncated=result["truncated"],
    )
    return {
        "ok": True,
        "repository": str(root),
        "revision": revision,
        "path": file_path,
        "start_line": start_line,
        "end_line": end_line,
        "count": len(records),
        "limit": end_line - start_line + 1,
        "truncated": bool(result["truncated"] or incomplete),
        "output_bytes": len(result["stdout"]),
        "lines": records,
    }
