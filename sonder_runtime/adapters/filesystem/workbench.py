"""Guarded local workbench discovery, inspection, and bounded execution.

The module is stdlib-only and deliberately keeps policy in ``file_ops``. It
never accepts a shell command string: execution is argv-based, workspace paths
must resolve through guarded roots, traversal is bounded, process trees are
timed out, and output pipes are continuously drained with capped retention.
"""
from __future__ import annotations

from sonder_runtime.platform.runtime_threads import Thread as owned_runtime_thread

import fnmatch
import hashlib
import heapq
import json
import os
import posixpath
import queue
import re
import shlex
import signal
import shutil
import stat
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import sonder_runtime.adapters.filesystem.file_ops as file_ops
import sonder_runtime.adapters.git_discovery as git_discovery
import sonder_runtime.adapters.process_tree as process_tree
from sonder_runtime.domain.agents import interpreter_argv as _interpreter_argv
from sonder_runtime.platform import logging as runtime_logging
from sonder_runtime.platform import paths as runtime_paths


MAX_TREE_ENTRIES = 500
MAX_SEARCH_RESULTS = 500
MAX_SEARCH_FILE_BYTES = 2_000_000
MAX_SEARCH_PATTERN_CHARS = 2_000
MAX_RANGE_LINES = 2_000
MAX_EXEC_OUTPUT = 128_000
MAX_EXEC_TIMEOUT = 120
MAX_PROGRAM_CANDIDATES = 5_000
MAX_IMAGE_BYTES = 64_000_000
MAX_WALK_ENTRIES = 100_000
MAX_WALK_SECONDS = 30.0
DEFAULT_WALK_ENTRIES = 20_000
DEFAULT_WALK_SECONDS = 10.0
SKIP_DIRS = {
    ".dart_tool", ".git", ".idea", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tooling", ".tox", ".venv", ".vs", "__pycache__",
    "build", "coverage", "dist", "ephemeral", "node_modules", "venv",
}
SKIP_DIRS_CASEFOLD = {item.casefold() for item in SKIP_DIRS}
TEXT_SUFFIXES = {
    "", ".bat", ".c", ".cc", ".cfg", ".cmd", ".cpp", ".cs", ".css",
    ".csv", ".dart", ".go", ".h", ".hpp", ".html", ".ini", ".java",
    ".js", ".json", ".jsx", ".log", ".md", ".ps1", ".py", ".rs",
    ".sh", ".sql", ".toml", ".ts", ".tsx", ".txt", ".xml", ".yaml",
    ".yml",
}
SCRIPT_EXTENSIONS = {
    ".bat": "cmd", ".cmd": "cmd", ".dart": "dart", ".js": "node",
    ".ps1": "powershell", ".py": "python", ".sh": "bash",
}
PROJECT_MANIFESTS = {
    "build.gradle", "build.gradle.kts", "cargo.toml", "cmakelists.txt",
    "composer.json", "deno.json", "deno.jsonc", "dockerfile", "gemfile",
    "go.mod", "gradlew", "makefile", "mix.exs", "package.json",
    "package.swift", "pom.xml", "pubspec.yaml", "pyproject.toml",
    "requirements.txt", "setup.cfg", "setup.py", "workspace",
}


def _bounded_int(value, default, minimum, maximum):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _bounded_float(value, default, minimum, maximum):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _resolve(path=".", *, extra_roots="", bypass=False) -> Path:
    return file_ops.resolve_path(
        str(path or "."), extra_roots=extra_roots, bypass=bool(bypass),
    )


def _hidden(name: str) -> bool:
    return name.startswith(".") and name not in (".", "..")


def _skip_dir(name: str, include_ignored=False) -> bool:
    return not include_ignored and name.casefold() in SKIP_DIRS_CASEFOLD


# Windows reparse tags with this bit set are name surrogates: the entry
# stands for another name (IO_REPARSE_TAG_MOUNT_POINT, IO_REPARSE_TAG_SYMLINK
# and similar). IsReparseTagNameSurrogate in the Windows SDK tests it. The
# stdlib ``stat`` module exposes no constant for it.
_REPARSE_TAG_NAME_SURROGATE = 0x20000000


def _reparse_entry(entry) -> bool:
    """Whether a scandir entry is a link the walk must not follow.

    On Windows ``DirEntry.is_symlink()`` is False for a junction, and
    ``is_dir(follow_symlinks=False)`` is True for one, so a walk that only
    checks ``is_symlink`` enters junctions and can leave every allowed root.
    ``is_junction`` (3.12+) catches it, and the reparse tag catches any other
    name surrogate. Non-surrogate reparse points (OneDrive / cloud-files
    placeholders, dedup files, AppExecLinks) are ordinary local content and
    are walked. A reparse point whose tag cannot be read is treated as a
    link (fail closed). On POSIX no extra stat is done: ``is_symlink`` is
    the whole answer there.
    """
    if entry.is_symlink():
        return True
    is_junction = getattr(entry, "is_junction", None)
    if is_junction is not None and is_junction():
        return True
    if os.name != "nt":
        return False
    metadata = entry.stat(follow_symlinks=False)
    attributes = getattr(metadata, "st_file_attributes", 0)
    if not attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        return False
    tag = getattr(metadata, "st_reparse_tag", 0)
    return not tag or bool(tag & _REPARSE_TAG_NAME_SURROGATE)


def _noncanonical_name(name: str) -> bool:
    """A Windows name Win32 does not open as spelled (trailing dot or space).

    Such names exist only when created through ``\\\\?\\`` paths. Opening
    ``id_rsa.`` or listing ``.aws.`` through a normal path reaches ``id_rsa``
    or ``.aws`` instead, so the walk's name is not the file that is read.
    """
    return os.name == "nt" and name.endswith((".", " "))


def _sensitive_directory(path: Path) -> str:
    """Skip reason for a directory a content search must not enter ("" if none).

    ``file_ops.SENSITIVE_READ_DIRECTORIES`` (``.git``, ``.ssh``, ``.aws`` ...)
    are secret or control state. A credential-store directory an operator
    root names is the one exception, decided by the same
    ``credential_read_component`` that ``file_read`` uses. ``.git`` is pruned
    even with ``include_ignored``, on purpose: repository metadata (config,
    packed refs, logs, hooks) is control state, not workspace text.
    """
    name = path.name.casefold()
    if name not in file_ops.SENSITIVE_READ_DIRECTORIES:
        return ""
    if name in file_ops.CREDENTIAL_READ_DIRECTORIES and not file_ops.credential_read_component(path):
        return ""
    return "sensitive_directory"


def _credential_directory(path: Path) -> str:
    """Skip reason for a credential-store directory a listing walk must not enter."""
    return "credential_store" if file_ops.is_credential_store(path) else ""


def _secret_file(path: Path) -> bool:
    """Whether a content search must withhold *path*.

    True for every file an unauthorized ``file_read`` refuses for being a
    secret or credential store: ``credential_read_component`` (root-aware, so
    a store an operator root names stays readable) plus ``_is_secret_path``
    and the personal corpus. The lists live in ``file_ops`` only.
    ``credential_read_component`` resolves every allowed root per call, so it
    runs only when the name-only ``_credential_part`` finds a store component
    anywhere in the absolute path: it judges a suffix of those same parts and
    cannot find one the whole path lacks.
    """
    if file_ops._is_secret_path(path) or file_ops._is_personal_corpus(path):
        return True
    return bool(
        file_ops._credential_part(path.parts)
        and file_ops.credential_read_component(path)
    )


def _new_walk_state(root: Path, max_entries: int, timeout_seconds: float):
    return {
        "root": str(root),
        "entries_scanned": 0,
        "files_seen": 0,
        "directories_seen": 0,
        "skipped_entries": 0,
        "skipped_by_reason": {},
        "skipped_examples": [],
        "truncated": False,
        "truncation_reason": "",
        "max_entries": max_entries,
        "timeout_seconds": timeout_seconds,
        "elapsed_ms": 0,
    }


def _note_skip(state, path, reason, *, expose_path=True):
    state["skipped_entries"] += 1
    counts = state["skipped_by_reason"]
    counts[reason] = counts.get(reason, 0) + 1
    if expose_path and len(state["skipped_examples"]) < 20:
        state["skipped_examples"].append({"path": str(path), "reason": reason})


def _bounded_walk(
    root: Path, *, include_hidden=False, include_ignored=False,
    max_entries=DEFAULT_WALK_ENTRIES, timeout_seconds=DEFAULT_WALK_SECONDS,
    prune_dir=None,
):
    """Return a deterministic guarded-tree iterator and mutable scan state.

    The caller already resolved ``root`` through ``file_ops``. Entries are
    yielded depth-first in case-insensitive path order. Symlinks, junctions
    and other name-surrogate reparse points are never followed (see
    ``_reparse_entry``), and on Windows a name with a trailing dot or space
    (which opens as a different file) is skipped. Both inspected directory
    entries and wall time are bounded, so a small output limit cannot hide an unbounded filesystem walk. ``prune_dir``
    maps a directory path to a skip reason ("" to enter it); a pruned
    directory is neither yielded nor entered.
    """
    max_entries = _bounded_int(
        max_entries, DEFAULT_WALK_ENTRIES, 1, MAX_WALK_ENTRIES,
    )
    timeout_seconds = _bounded_float(
        timeout_seconds, DEFAULT_WALK_SECONDS, 0.01, MAX_WALK_SECONDS,
    )
    state = _new_walk_state(root, max_entries, timeout_seconds)
    state["enforce_git_visibility"] = not include_ignored
    visible = None
    if state["enforce_git_visibility"]:
        visible = git_discovery.visible_paths(
            root,
            timeout_seconds=min(
                timeout_seconds, git_discovery.DEFAULT_TIMEOUT_SECONDS,
            ),
        )
    state["git_visible"] = visible
    started = time.monotonic()
    deadline = started + timeout_seconds

    def stop(reason):
        state["truncated"] = True
        state["truncation_reason"] = reason

    def iterator():
        stack = [root]
        try:
            while stack:
                if time.monotonic() >= deadline:
                    stop("timeout")
                    break
                if state["entries_scanned"] >= max_entries:
                    stop("max_entries")
                    break
                base = stack.pop()
                children = []
                halt_reason = ""
                try:
                    with os.scandir(base) as scan:
                        remaining = max_entries - state["entries_scanned"]
                        while len(children) < remaining:
                            if time.monotonic() >= deadline:
                                halt_reason = "timeout"
                                break
                            try:
                                children.append(next(scan))
                            except StopIteration:
                                break
                except (OSError, PermissionError):
                    _note_skip(state, base, "unreadable_directory")
                    continue
                if len(children) >= max_entries - state["entries_scanned"]:
                    halt_reason = halt_reason or "max_entries"
                children.sort(key=lambda item: item.name.casefold())
                state["entries_scanned"] += len(children)
                descend = []
                for child in children:
                    if time.monotonic() >= deadline:
                        stop("timeout")
                        return
                    path = Path(child.path)
                    relative = str(path.relative_to(root))
                    if visible is not None and os.path.normcase(relative) not in visible:
                        _note_skip(
                            state, path, "git_ignored", expose_path=False,
                        )
                        continue
                    try:
                        if child.is_symlink():
                            _note_skip(state, path, "symlink")
                            continue
                        if _reparse_entry(child):
                            _note_skip(state, path, "reparse_point")
                            continue
                        if _noncanonical_name(child.name):
                            _note_skip(state, path, "noncanonical_name")
                            continue
                        if not include_hidden and _hidden(child.name):
                            _note_skip(state, path, "hidden")
                            continue
                        is_dir = child.is_dir(follow_symlinks=False)
                        if is_dir and _skip_dir(child.name, include_ignored):
                            _note_skip(state, path, "ignored_directory")
                            continue
                        pruned = is_dir and prune_dir is not None and prune_dir(path)
                        if pruned:
                            _note_skip(state, path, pruned)
                            continue
                        size = 0 if is_dir else child.stat(follow_symlinks=False).st_size
                    except (OSError, PermissionError):
                        _note_skip(state, path, "unreadable_entry")
                        continue
                    item = {
                        "path": str(path),
                        "relative": relative,
                        "name": child.name,
                        "type": "dir" if is_dir else "file",
                        "bytes": int(size),
                    }
                    if is_dir:
                        state["directories_seen"] += 1
                        descend.append(path)
                    else:
                        state["files_seen"] += 1
                    yield item
                if halt_reason:
                    stop(halt_reason)
                    return
                stack.extend(reversed(descend))
        finally:
            state["elapsed_ms"] = int((time.monotonic() - started) * 1000)

    return iterator(), state


def _finish_walk(iterator, state):
    """Close an early-stopped walk and return stable public scan metadata."""
    close = getattr(iterator, "close", None)
    if close:
        close()
    if state["enforce_git_visibility"]:
        git_discovery.require_unchanged(
            Path(state["root"]), state["git_visible"],
            timeout_seconds=min(
                state["timeout_seconds"], git_discovery.DEFAULT_TIMEOUT_SECONDS,
            ),
        )
    return {
        "entries_scanned": state["entries_scanned"],
        "files_seen": state["files_seen"],
        "directories_seen": state["directories_seen"],
        "skipped_entries": state["skipped_entries"],
        "skipped_by_reason": dict(sorted(state["skipped_by_reason"].items())),
        "skipped_examples": list(state["skipped_examples"]),
        "elapsed_ms": state["elapsed_ms"],
        "truncated": state["truncated"],
        "truncation_reason": state["truncation_reason"],
    }


def workspace_inventory(
    path=".", *, max_entries=DEFAULT_WALK_ENTRIES,
    timeout_seconds=DEFAULT_WALK_SECONDS, top_n=15, include_hidden=False,
    include_ignored=False, extra_roots="", bypass=False,
):
    """Summarize a guarded workspace without reading file contents."""
    root = _resolve(path, extra_roots=extra_roots, bypass=bypass)
    if not root.is_dir():
        raise ValueError("inventory root is not a directory: %s" % root)
    top_n = _bounded_int(top_n, 15, 1, 50)
    iterator, state = _bounded_walk(
        root,
        include_hidden=include_hidden,
        include_ignored=include_ignored,
        max_entries=max_entries,
        timeout_seconds=timeout_seconds,
    )
    total_bytes = 0
    extension_stats = {}
    area_stats = {}
    manifests = []
    largest = []
    for item in iterator:
        if item["type"] != "file":
            continue
        size = item["bytes"]
        total_bytes += size
        suffix = Path(item["name"]).suffix.lower() or "(none)"
        ext = extension_stats.setdefault(suffix, {"extension": suffix, "files": 0, "bytes": 0})
        ext["files"] += 1
        ext["bytes"] += size
        parts = Path(item["relative"]).parts
        area_name = parts[0] if len(parts) > 1 else "."
        area = area_stats.setdefault(area_name, {"path": area_name, "files": 0, "bytes": 0})
        area["files"] += 1
        area["bytes"] += size
        rank = (size, item["relative"].casefold(), item["relative"])
        if len(largest) < top_n:
            heapq.heappush(largest, rank)
        elif rank > largest[0]:
            heapq.heapreplace(largest, rank)
        lowered = item["name"].casefold()
        if (
            lowered in PROJECT_MANIFESTS
            or lowered.endswith((".sln", ".xcodeproj"))
        ) and len(manifests) < 100:
            manifests.append(item["relative"])
    scan = _finish_walk(iterator, state)
    return {
        "root": str(root),
        "files": state["files_seen"],
        "directories": state["directories_seen"],
        "bytes": total_bytes,
        "extensions": sorted(
            extension_stats.values(),
            key=lambda row: (-row["bytes"], -row["files"], row["extension"]),
        )[:top_n],
        "largest_files": [
            {"relative": relative, "bytes": size}
            for size, _folded, relative in sorted(largest, reverse=True)
        ],
        "top_areas": sorted(
            area_stats.values(),
            key=lambda row: (-row["bytes"], -row["files"], row["path"].casefold()),
        )[:top_n],
        "manifests": sorted(manifests, key=str.casefold),
        **scan,
    }


def directory_tree(
    path=".", *, depth=2, max_entries=200, include_hidden=False,
    include_ignored=False,
    extra_roots="", bypass=False,
):
    root = _resolve(path, extra_roots=extra_roots, bypass=bypass)
    if not root.exists():
        raise FileNotFoundError(str(root))
    if not root.is_dir():
        raise ValueError("tree root is not a directory: %s" % root)
    depth = _bounded_int(depth, 2, 0, 8)
    limit = _bounded_int(max_entries, 200, 1, MAX_TREE_ENTRIES)
    entries = []
    skipped = 0
    visible = None if include_ignored else git_discovery.visible_paths(root)

    def visit(base: Path, level: int):
        nonlocal skipped
        if len(entries) >= limit or level > depth:
            return
        try:
            children = sorted(os.scandir(base), key=lambda item: (not item.is_dir(follow_symlinks=False), item.name.lower()))
        except (OSError, PermissionError):
            skipped += 1
            return
        for child in children:
            if len(entries) >= limit:
                break
            candidate = Path(child.path)
            relative = str(candidate.relative_to(root))
            if visible is not None and os.path.normcase(relative) not in visible:
                skipped += 1
                continue
            try:
                unsafe = _reparse_entry(child) or _noncanonical_name(child.name)
            except OSError:
                unsafe = True
            if unsafe:
                skipped += 1
                continue
            if not include_hidden and _hidden(child.name):
                skipped += 1
                continue
            is_dir = child.is_dir(follow_symlinks=False)
            if is_dir and _skip_dir(child.name, include_ignored):
                skipped += 1
                continue
            if file_ops.is_credential_store(candidate):
                # A credential store is neither listed nor entered, whatever
                # include_hidden says; its names and sizes are its metadata.
                skipped += 1
                continue
            try:
                size = child.stat(follow_symlinks=False).st_size if not is_dir else 0
            except OSError:
                size = 0
            entries.append({
                "path": str(candidate),
                "relative": relative or ".",
                "name": child.name,
                "type": "dir" if is_dir else "file",
                "depth": level,
                "bytes": size,
            })
            if is_dir and level < depth:
                visit(candidate, level + 1)

    visit(root, 1)
    if not include_ignored:
        git_discovery.require_unchanged(root, visible)
    return {
        "root": str(root),
        "depth": depth,
        "entries": entries,
        "truncated": len(entries) >= limit,
        "skipped": skipped,
    }


def read_line_range(
    path, *, start_line=1, end_line=200, extra_roots="", bypass=False,
):
    target = _resolve(path, extra_roots=extra_roots, bypass=bypass)
    if not target.is_file():
        raise FileNotFoundError("file not found: %s" % target)
    start = _bounded_int(start_line, 1, 1, 10_000_000)
    end = _bounded_int(end_line, start + 199, 1, 10_000_000)
    if end < start:
        # An inverted range was previously clamped to a single line, silently
        # returning misleading output; reject it explicitly instead.
        raise ValueError("end_line (%d) is before start_line (%d)" % (end, start))
    end = min(end, start + MAX_RANGE_LINES - 1)
    lines = []
    total_seen = 0
    with target.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        for number, line in enumerate(handle, 1):
            total_seen = number
            if number < start:
                continue
            if number > end:
                break
            lines.append({"line": number, "text": line.rstrip("\r\n")})
    return {
        "path": str(target),
        "start_line": start,
        "end_line": end,
        "lines": lines,
        "eof": total_seen < end,
    }


# Child-side loop for _RegexSearchWorker. It reads one JSON header line
# (pattern, flags), then per file a "<limit> <bytes>\n" line followed by that
# many bytes of UTF-8 text, and answers each file with one JSON line of
# [line, column, text] rows. Lines are split exactly as text_search's
# in-process loop splits them (the parent already applied universal newlines).
_REGEX_WORKER_SOURCE = r"""
import io, json, re, sys
source = sys.stdin.buffer
sink = sys.stdout.buffer
header = json.loads(source.readline())
pattern = re.compile(header["pattern"], header["flags"])
while True:
    request = source.readline()
    if not request:
        break
    limit, size = (int(part) for part in request.split())
    text = source.read(size).decode("utf-8", "surrogatepass")
    found = []
    for number, line in enumerate(io.StringIO(text), 1):
        match = pattern.search(line)
        if match:
            found.append([number, match.start() + 1, line.rstrip("\r\n")[:500]])
            if len(found) >= limit:
                break
    sink.write(json.dumps(found).encode("ascii") + b"\n")
    sink.flush()
"""


class _RegexSearchWorker:
    """Run a caller-supplied regex in a child process under a hard deadline.

    Python's ``re`` engine backtracks and cannot be interrupted from another
    thread, so a pattern such as ``(a+)+$`` against a long line would pin the
    request thread for hours; the walk deadline is only checked between
    entries. The parent keeps every policy decision and opens every file
    itself, then ships the decoded text to the child and waits for its answer
    no longer than the search deadline, killing the child when it passes.

    Ownership: one instance per text_search call, used from that call's thread
    only; ``close()`` must run (it kills and reaps the child and joins the
    reply reader). Starts one Python child process and one reader thread.
    """

    def __init__(self, pattern_text, flags):
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._replies = queue.Queue()
        self._reader = None
        self._proc = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", _REGEX_WORKER_SOURCE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            creationflags=creationflags,
        )
        try:
            self._reader = owned_runtime_thread(
                target=self._read_replies, daemon=True,
            )
            self._reader.start()
            header = json.dumps({"pattern": pattern_text, "flags": int(flags)})
            self._proc.stdin.write(header.encode("utf-8", "surrogatepass") + b"\n")
            self._proc.stdin.flush()
        except BaseException:
            self.close()
            raise

    def _read_replies(self):
        try:
            for line in self._proc.stdout:
                self._replies.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self._replies.put(None)

    def search(self, text, limit, deadline):
        """Return ``[line, column, text]`` rows, or None once ``deadline`` passes."""
        if time.monotonic() >= deadline:
            return None
        payload = text.encode("utf-8", "surrogatepass")
        try:
            self._proc.stdin.write(b"%d %d\n" % (int(limit), len(payload)))
            self._proc.stdin.write(payload)
            self._proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise RuntimeError("regex search worker stopped unexpectedly") from exc
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            reply = self._replies.get(timeout=remaining)
        except queue.Empty:
            return None
        if reply is None:
            raise RuntimeError("regex search worker stopped unexpectedly")
        return json.loads(reply)

    def close(self):
        proc = self._proc
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        # The dead child's stdout reaches EOF, which ends the reader; close the
        # pipe only after that so close() never waits on a blocked read.
        if self._reader is not None:
            self._reader.join(timeout=2)
        if self._reader is None or not self._reader.is_alive():
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except (OSError, ValueError):
                pass


def text_search(
    query, *, root=".", glob="*", regex=False, case_sensitive=False,
    max_results=100, max_file_bytes=MAX_SEARCH_FILE_BYTES,
    max_entries=DEFAULT_WALK_ENTRIES, timeout_seconds=DEFAULT_WALK_SECONDS,
    include_hidden=False, include_ignored=False,
    extra_roots="", bypass=False,
):
    if not str(query or ""):
        raise ValueError("search query is required")
    root_path = _resolve(root, extra_roots=extra_roots, bypass=bypass)
    if not root_path.is_dir():
        raise ValueError("search root is not a directory: %s" % root_path)
    limit = _bounded_int(max_results, 100, 1, MAX_SEARCH_RESULTS)
    size_limit = _bounded_int(
        max_file_bytes, MAX_SEARCH_FILE_BYTES, 1, MAX_SEARCH_FILE_BYTES,
    )
    flags = 0 if case_sensitive else re.IGNORECASE
    if regex and len(str(query)) > MAX_SEARCH_PATTERN_CHARS:
        raise ValueError(
            "regex query exceeds %d characters" % MAX_SEARCH_PATTERN_CHARS
        )
    pattern = re.compile(str(query) if regex else re.escape(str(query)), flags)
    # The extension allowlist (TEXT_SUFFIXES) applies only to a broad glob. An
    # EXPLICIT glob ("*.tmp", a filename) is an intentional request to search
    # exactly those files, so honor it rather than silently skipping and
    # reporting a misleading "0 files scanned". The null-byte binary check below
    # still guards against binary content.
    explicit_glob = str(glob or "*") not in ("", "*")
    matches = []
    files_scanned = 0
    files_skipped = 0
    secret_files_skipped = 0
    # Secret and credential files are withheld whatever include_hidden, the
    # glob or bypass say: a search is an unaddressed sweep, so it never
    # returns a line file_read would refuse to an unauthorized caller.
    iterator, state = _bounded_walk(
        root_path,
        include_hidden=include_hidden,
        include_ignored=include_ignored,
        max_entries=max_entries,
        timeout_seconds=timeout_seconds,
        prune_dir=_sensitive_directory,
    )
    result_limited = False
    # A caller regex runs in a child process held to the walk's own wall
    # budget (see _RegexSearchWorker); a literal query stays in-process.
    deadline = time.monotonic() + state["timeout_seconds"]
    regex_timed_out = False
    worker = None
    try:
        for item in iterator:
            if item["type"] != "file":
                continue
            path = Path(item["path"])
            name = item["name"]
            relative = item["relative"]
            if not (fnmatch.fnmatch(name, glob) or fnmatch.fnmatch(relative, glob)):
                continue
            # Judge and open the file the OS actually resolves to, as file_read
            # does, not the walk's spelling of it: a junction, a trailing-dot name
            # or any other alias the walk did not catch must not turn a refused
            # file into a searchable one. The walk stays under root_path, so a
            # resolved path outside it is an alias and is never opened.
            resolved = file_ops._resolve_best_effort(path)
            if not file_ops._is_inside(resolved, root_path):
                _note_skip(state, path, "resolves_outside_root")
                continue
            if _secret_file(resolved):
                secret_files_skipped += 1
                _note_skip(state, path, "secret_file")
                continue
            if item["bytes"] > size_limit:
                files_skipped += 1
                continue
            if not explicit_glob and resolved.suffix.lower() not in TEXT_SUFFIXES:
                files_skipped += 1
                continue
            try:
                with resolved.open("rb") as raw:
                    if b"\0" in raw.read(4096):
                        files_skipped += 1
                        continue
                handle = resolved.open("r", encoding="utf-8", errors="replace")
            except (OSError, PermissionError):
                files_skipped += 1
                continue
            files_scanned += 1
            if regex:
                with handle:
                    text = handle.read()
                if worker is None:
                    worker = _RegexSearchWorker(str(query), flags)
                rows = worker.search(text, limit - len(matches), deadline)
                if rows is None:
                    regex_timed_out = True
                    break
                for number, column, line_text in rows:
                    matches.append({
                        "path": str(path),
                        "relative": relative,
                        "line": number,
                        "column": column,
                        "text": line_text,
                    })
                if len(matches) >= limit:
                    result_limited = True
                    break
                continue
            with handle:
                for number, line in enumerate(handle, 1):
                    found = pattern.search(line)
                    if not found:
                        continue
                    matches.append({
                        "path": str(path),
                        "relative": relative,
                        "line": number,
                        "column": found.start() + 1,
                        "text": line.rstrip("\r\n")[:500],
                    })
                    if len(matches) >= limit:
                        result_limited = True
                        break
            if result_limited:
                break
    finally:
        if worker is not None:
            worker.close()
    if regex_timed_out:
        state["truncated"] = True
        state["truncation_reason"] = "timeout"
    scan = _finish_walk(iterator, state)
    if result_limited:
        scan["truncated"] = True
        scan["truncation_reason"] = "max_results"
    directories_withheld = scan["skipped_by_reason"].get("sensitive_directory", 0)
    withheld = ""
    if secret_files_skipped or directories_withheld:
        withheld = (
            "; skipped %d secret/credential files and %d sensitive directories"
            " (contents not searched)" % (secret_files_skipped, directories_withheld)
        )
    return {
        "root": str(root_path), "query": str(query), "glob": glob,
        "regex": bool(regex), "case_sensitive": bool(case_sensitive),
        "matches": matches, "files_scanned": files_scanned,
        "files_skipped": files_skipped,
        "secret_files_skipped": secret_files_skipped,
        "sensitive_directories_skipped": directories_withheld,
        "withheld_note": withheld, **scan,
    }


def script_search(
    query="*", *, root=".", max_results=100,
    max_entries=DEFAULT_WALK_ENTRIES, timeout_seconds=DEFAULT_WALK_SECONDS,
    include_hidden=False, include_ignored=False, extra_roots="", bypass=False,
):
    limit = _bounded_int(max_results, 100, 1, MAX_SEARCH_RESULTS)
    root_path = _resolve(root, extra_roots=extra_roots, bypass=bypass)
    if not root_path.is_dir():
        raise ValueError("script search root is not a directory: %s" % root_path)
    iterator, state = _bounded_walk(
        root_path,
        include_hidden=include_hidden,
        include_ignored=include_ignored,
        max_entries=max_entries,
        timeout_seconds=timeout_seconds,
        prune_dir=_credential_directory,
    )
    needle = str(query or "*").lower()
    results = []
    result_limited = False
    for item in iterator:
        suffix = Path(item["name"]).suffix.lower()
        if item["type"] != "file" or suffix not in SCRIPT_EXTENSIONS:
            continue
        if needle not in ("", "*") and needle not in item["relative"].lower():
            continue
        if file_ops.is_credential_store(Path(item["path"])):
            _note_skip(state, item["path"], "credential_store", expose_path=False)
            continue
        results.append({**item, "runner": SCRIPT_EXTENSIONS[suffix]})
        if len(results) >= limit:
            result_limited = True
            break
    scan = _finish_walk(iterator, state)
    if result_limited:
        scan["truncated"] = True
        scan["truncation_reason"] = "max_results"
    return {
        "root": str(root_path), "query": query, "results": results, **scan,
    }


def _program_match(query: str, name: str, path: str) -> bool:
    query = (query or "*").strip().lower()
    if query in ("", "*"):
        return True
    return query in name.lower() or query in path.lower()


def _windows_app_paths():
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []
    rows = []
    locations = (
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\App Paths"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\App Paths"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths"),
    )
    for hive, key_name in locations:
        try:
            key = winreg.OpenKey(hive, key_name)
        except OSError:
            continue
        with key:
            for index in range(1000):
                try:
                    sub_name = winreg.EnumKey(key, index)
                    sub = winreg.OpenKey(key, sub_name)
                except OSError:
                    break
                with sub:
                    try:
                        path, _ = winreg.QueryValueEx(sub, None)
                    except OSError:
                        path = ""
                if path:
                    rows.append((sub_name, os.path.expandvars(str(path)), "app-paths"))
    return rows


def program_search(query="*", *, max_results=100):
    limit = _bounded_int(max_results, 100, 1, 500)
    candidates = []
    candidates_truncated = False
    exact = shutil.which(str(query or "")) if query not in ("", "*") else None
    if exact:
        candidates.append((Path(exact).name, exact, "which"))
    path_exts = [""]
    windows_path_semantics = os.name == "nt" or bool(
        os.environ.get("PATHEXT", "").strip()
    )
    if windows_path_semantics:
        path_exts = [ext.lower() for ext in os.environ.get("PATHEXT", ".EXE;.CMD;.BAT;.COM").split(";") if ext]
    for raw_dir in os.environ.get("PATH", "").split(os.pathsep):
        if len(candidates) >= MAX_PROGRAM_CANDIDATES:
            candidates_truncated = True
            break
        directory = Path(raw_dir.strip('" '))
        if not directory.is_dir():
            continue
        try:
            entries = os.scandir(directory)
        except OSError:
            continue
        with entries:
            for entry in entries:
                if not entry.is_file(follow_symlinks=False):
                    continue
                suffix = Path(entry.name).suffix.lower()
                if windows_path_semantics and suffix not in path_exts:
                    continue
                if not windows_path_semantics and not os.access(entry.path, os.X_OK):
                    continue
                if not _program_match(str(query), entry.name, entry.path):
                    continue
                candidates.append((entry.name, entry.path, "PATH"))
                if len(candidates) >= MAX_PROGRAM_CANDIDATES:
                    candidates_truncated = True
                    break
    remaining = max(0, MAX_PROGRAM_CANDIDATES - len(candidates))
    app_paths = _windows_app_paths()
    candidates.extend(app_paths[:remaining])
    candidates_truncated = candidates_truncated or len(app_paths) > remaining
    seen = set()
    results = []
    for name, path, source in candidates:
        key = os.path.normcase(os.path.abspath(path))
        if key in seen or not _program_match(str(query), name, path):
            continue
        seen.add(key)
        results.append({"name": name, "path": path, "source": source})
        if len(results) >= limit:
            break
    results.sort(key=lambda row: (row["name"].lower(), row["path"].lower()))
    return {
        "query": str(query or "*"), "results": results,
        "truncated": candidates_truncated or len(results) >= limit,
    }


def _json_args(args_json):
    if args_json in (None, ""):
        return []
    data = json.loads(args_json) if isinstance(args_json, str) else args_json
    if not isinstance(data, list):
        raise ValueError("args must be a JSON list")
    if len(data) > 64:
        raise ValueError("too many arguments")
    out = []
    for value in data:
        text = str(value)
        if "\0" in text or len(text) > 8192:
            raise ValueError("invalid program argument")
        out.append(text)
    return out


def _resolve_program(program, *, extra_roots="", bypass=False):
    value = str(program or "").strip()
    if not value:
        raise ValueError("program is required")
    # Refuse UNC/device/NT-namespace spellings before any os.path.realpath
    # call can open an SMB share (NTLM credential leak).
    value = file_ops._reject_network_path(value)
    if Path(value).is_absolute() or any(mark in value for mark in ("/", "\\")):
        if Path(value).is_absolute():
            if os.path.normcase(os.path.realpath(value)) == os.path.normcase(os.path.realpath(sys.executable)):
                return value
            path_program = shutil.which(Path(value).name)
            if path_program and os.path.normcase(os.path.realpath(path_program)) == os.path.normcase(os.path.realpath(value)):
                return path_program
        path = _resolve(value, extra_roots=extra_roots, bypass=bypass)
        if not path.is_file():
            raise FileNotFoundError(str(path))
        return str(path)
    resolved = shutil.which(value)
    if not resolved:
        raise FileNotFoundError("program not found on PATH: %s" % value)
    return resolved


def _run_program_environment():
    """Child environment for the run_program lane: no secrets, utf-8 output.

    Two halves that must agree. The secret stripping is shared with the
    code_runner lane through sonder_logging. The encoding is not shared,
    deliberately: this lane reads the child's pipes as raw bytes and decodes
    them as utf-8 below, so the child has to be told to *emit* utf-8 or the two
    ends disagree.

    On Windows a redirected Python child otherwise defaults to the active ANSI
    code page (commonly cp1252), which raises UnicodeEncodeError the moment a
    program prints a character outside it -- reporting a valid snippet such as
    ``print("🎲")`` as a failed run. code_runner fixed exactly this for its
    own lane; the same class of model-authored program reaches this one through
    script_run and workspace_run.

    Setting it here rather than in sonder_logging.child_environment() is on
    purpose: most of that helper's other callers decode their children with the
    parent's locale, so forcing utf-8 on them would swap this bug for mojibake
    in a dozen places.
    """
    environment = runtime_logging.child_environment()
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def _drain_pipe(pipe, sink, state, limit):
    # Reserve half the byte budget for the final verdict. Keep draining after
    # filling the window so a verbose child cannot block on its output pipe.
    head_limit = limit // 2
    try:
        while True:
            block = pipe.read(65536)
            if not block:
                break
            state["bytes"] += len(block)
            sink.extend(block)
            if len(sink) > limit:
                del sink[head_limit:len(sink) - (limit - head_limit)]
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _write_stdin(pipe, payload):
    try:
        if payload:
            pipe.write(payload)
            pipe.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _abandon_child(proc, threads, tree=None):
    """Kill and reap ``proc`` and close its pipes after a failed I/O setup."""
    try:
        _terminate_process_tree(proc, tree)
    except OSError:
        pass
    try:
        proc.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        try:
            if stream is not None:
                stream.close()
        except (OSError, ValueError):
            pass
    for thread in threads:
        thread.join(timeout=2)
    if tree is not None:
        tree.close()


def _terminate_process_tree(proc, tree=None):
    """Tear down ``proc``'s tree: the tree adapter for a real child, else legacy.

    Runs after the leader exits as well as on timeout: a program that starts
    a background child and exits 0 must not leave that child running past
    the run's time bound, holding the output pipes. ``tree`` is the adapter
    handle taken right after launch (it holds the Windows Job Object or POSIX
    process group). The legacy branch only serves test doubles.
    """
    if tree is None and process_tree.is_real_popen(proc):
        tree = process_tree.ProcessTree.adopt(proc)
    if tree is not None:
        tree.terminate(grace_seconds=0.2, kill_wait_seconds=5.0)
        return
    if proc.poll() is not None:
        return
    if os.name == "nt":
        taskkill = shutil.which("taskkill")
        if taskkill:
            subprocess.run(
                [taskkill, "/PID", str(proc.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


def _wait_leader(proc, timeout):
    """Wait up to ``timeout`` seconds for ``proc`` to exit.

    Raises ``subprocess.TimeoutExpired`` like ``Popen.wait``. Where
    ``os.waitid`` exists (POSIX) the exited leader is left unreaped
    (``WNOWAIT``): as a zombie it keeps its pid -- which is also its process
    group id -- from being reused until _terminate_process_tree has signalled
    the group. The caller reaps it afterwards with ``proc.wait()``.
    """
    waitid = getattr(os, "waitid", None)
    if os.name == "nt" or waitid is None:
        proc.wait(timeout=timeout)
        return
    deadline = time.monotonic() + timeout
    delay = 0.0005
    while True:
        try:
            state = waitid(
                os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG,
            )
        except ChildProcessError:
            return
        if state is not None:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(proc.args, timeout)
        delay = min(delay * 2, remaining, 0.05)
        time.sleep(delay)


# Inline program text per interpreter. run_program is argv-only, so inline
# code (``bash -c ...``, ``python -c ...``, program text piped on stdin) is an
# ad-hoc script that never passes through run_script's per-suffix validation
# or the execution-risk policy. Refuse it uniformly and route callers to
# script_run, mirroring the PowerShell/cmd guard below.
#
# Matching the flag exactly or as a prefix was evaded by short-option bundles
# (``python -Ic``, ``bash -ec``, ``perl -we``) and by an option value in front
# of the flag (``python -X dev -c``), so argv is walked the way the
# interpreter parses it: bundles letter by letter (letters are case-sensitive,
# like the interpreters), option values skipped, and option parsing ending at
# the first operand -- the script, whose own arguments (``python script.py
# -c``) are not the interpreter's. Anything the grammar does not know marks
# the parse uncertain; from then on no operand ends the scan, so an unknown
# value-taking option can only cause a refusal, never hide an inline flag.
#
# Grammar fields: ``inline`` letters/longs take program text; ``value``
# letters take the rest of the bundle or, at its end, the next argument;
# ``after`` letters take the next argument and the bundle carries on (sh:
# ``-oc pipefail CODE``); ``program`` letters name the program to run
# (``python -m``; option parsing ends there when ``ends_at_program``);
# ``module`` letters take a perl module list, which perl pastes into
# ``use MODULE ...;`` source. ``checks`` maps a value letter to a check of
# its value and ``glued`` letters take only the rest of the bundle, never the
# next argument; ``stdin`` letters make the interpreter read program text
# from stdin; ``data_long`` options load a module specifier, where a
# ``data:`` URL is inline source; ``commands`` are subcommands the scan
# continues past; ``inline_commands`` take program text; with
# ``remote_operands`` a ``data:``/``http(s):`` program operand is refused.
# Long option names are compared with ``_`` folded into ``-`` (node does
# that); an interpreter that does not fold rejects the spelling outright.
def _inline_grammar(
    inline="", flags="", value="", after="", program="", stdin="", glued="",
    checks=None, checks_long=None, inline_long=(), value_long=(), flags_long=(),
    data_long=(), stdin_long=(), plus=False, ends_at_program=False, commands=(),
    inline_commands=(), stdin_commands=(), remote_operands=False,
):
    checks = dict(checks or {})
    checks_long = dict(checks_long or {})
    return {
        "inline": frozenset(inline), "flags": frozenset(flags),
        "value": frozenset(value + program + glued + "".join(checks)),
        "after": frozenset(after), "program": frozenset(program),
        "glued": frozenset(glued), "checks": checks,
        "checks_long": checks_long,
        "stdin": frozenset(stdin), "inline_long": frozenset(inline_long),
        "value_long": frozenset(value_long) | frozenset(checks_long),
        "flags_long": frozenset(flags_long),
        "data_long": frozenset(data_long), "stdin_long": frozenset(stdin_long),
        "plus": plus, "ends_at_program": ends_at_program,
        "commands": frozenset(commands),
        "inline_commands": frozenset(inline_commands),
        "stdin_commands": frozenset(stdin_commands),
        "remote_operands": remote_operands,
    }


_STDIN_OPERANDS = frozenset({
    "-",
    "/dev/stdin",
    "/dev/fd/0",
    "/proc/self/fd/0",
    "/proc/thread-self/fd/0",
})


def _is_stdin_operand(arg):
    """True when ``arg`` names standard input (``-``, empty, or ``/dev/stdin`` etc.)."""
    candidate = arg.strip()
    if candidate in ("", "-"):
        return True
    lowered = candidate.lower()
    if lowered.startswith("file:"):
        rest = candidate[5:]
        if rest.startswith("//localhost/"):
            rest = rest[len("//localhost"):]
        elif rest.startswith("///"):
            rest = rest[2:]
        candidate = rest
    if candidate.startswith("/"):
        # Collapse repeated slashes (normpath keeps a leading "//"), then the
        # "." and ".." segments, so /dev/./stdin and /proc/self/fd/../fd/0
        # match. Lexical only: it can over-match a symlinked path, which
        # refuses rather than allows.
        return posixpath.normpath(re.sub(r"/+", "/", candidate)) in _STDIN_OPERANDS
    return False


def _is_remote_operand(arg):
    return arg.strip().lower().startswith(("data:", "http:", "https:"))


# Option-value checks. Each returns "refuse" for a value that is program
# text, "stdin" when the option makes the interpreter read code from stdin.
# A perl -M/-m value perl turns into plain ``use Module LIST;``: a module name,
# optionally ``=a,b`` (pasted inside a NUL-quoted q{}). Anything else is code.
_PERL_MODULE_ARG = re.compile(r"-?[A-Za-z_][A-Za-z0-9_:]*(?:=.*)?\Z", re.DOTALL)
# perl -d[t]:MOD loads Devel::MOD; an "=LIST" or any other text is pasted into
# source, and a bare -d / -dt starts the debugger, which reads its commands
# (perl code) from stdin.
_PERL_DEBUGGER_ARG = re.compile(
    r"t?:[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*\Z")
# python -m modules whose command line runs program text from argv or stdin
# (code, timeit, pdb commands, idlelib/IPython -c, the asyncio and _pyrepl
# REPLs) or that hand off to another module named on argv (profile,
# cProfile, trace, runpy), which would let `-m runpy code` reach the rest. A
# denylist, not an allowlist: -m pytest/pip/venv/json.tool and the like are
# ordinary file-running tools. compileall (byte-compiles, never executes),
# site (prints paths), pydoc, doctest and unittest (run files) are not listed.
_PYTHON_CODE_MODULES = frozenset({
    "code", "timeit", "pdb", "profile", "cprofile", "trace", "runpy",
    "idlelib", "ipython", "asyncio", "_pyrepl",
})


def _is_data_url(value):
    return value.strip().lower().startswith("data:")


def _perl_module_verdict(value):
    return "refuse" if value and not _PERL_MODULE_ARG.match(value) else None


def _perl_debugger_verdict(value):
    if value in ("", "t"):
        return "stdin"
    return None if _PERL_DEBUGGER_ARG.match(value) else "refuse"


def _data_url_verdict(value):
    return "refuse" if _is_data_url(value) else None


def _python_module_verdict(value):
    top = value.strip().split(".", 1)[0].casefold()
    return "refuse" if top in _PYTHON_CODE_MODULES else None


def _stdin_file_verdict(value):
    return "stdin" if _is_stdin_operand(value) else None


def _php_define_verdict(value):
    """Refuse php -d/--define auto_prepend_file/auto_append_file stream injections."""
    setting, sep, target = str(value or "").partition("=")
    if not sep:
        return None
    if setting.strip().lower() not in ("auto_prepend_file", "auto_append_file"):
        return None
    cleaned = target.strip()
    while cleaned and (cleaned[0] in ("'", '"') or cleaned[-1] in ("'", '"')):
        cleaned = cleaned.strip("\"'").strip()
    lowered = cleaned.lower()
    if lowered in ("php://input", "php://stdin") or _is_stdin_operand(cleaned):
        return "stdin"
    if lowered.startswith(("data:", "php://")) or re.match(r"^[a-z][a-z0-9+.-]*://", lowered):
        return "refuse"
    return None


_PYTHON_GRAMMAR = _inline_grammar(
    inline="c", flags="bBdEhiIOPqRsSuvVx?", value="WX", program="m", stdin="i",
    checks={"m": _python_module_verdict},
    flags_long=("help", "version", "help-env", "help-xoptions", "help-all"),
    value_long=("check-hash-based-pycs",), ends_at_program=True,
)
_SH_GRAMMAR = _inline_grammar(
    inline="c", flags="abefhiklmnprstuvxBCDEHPT", after="oO", stdin="s",
    flags_long=(
        "login", "noprofile", "norc", "posix", "debugger", "dump-strings",
        "dump-po-strings", "verbose", "version", "help", "restricted",
        "noediting", "pretty-print",
    ),
    value_long=("rcfile", "init-file"),
    checks_long={"rcfile": _stdin_file_verdict, "init-file": _stdin_file_verdict},
    plus=True,
)
_CSH_GRAMMAR = _inline_grammar(
    inline="c", flags="bdefimnqsStvVxX", stdin="st",
    flags_long=("version", "help"),
)
_NU_GRAMMAR = _inline_grammar(
    inline="ce",
    inline_long=("commands", "execute"),
    value_long=("config", "env-config", "log-level", "threads", "table-mode"),
    flags_long=("stdin", "interactive", "login", "no-config-file", "help", "version"),
    stdin_long=("stdin", "interactive"),
)
_INLINE_GRAMMARS = {
    "python": _PYTHON_GRAMMAR,
    "py": _PYTHON_GRAMMAR,
    "bash": _SH_GRAMMAR,
    "git-bash": _SH_GRAMMAR,
    "sh": _SH_GRAMMAR,
    "zsh": _SH_GRAMMAR,
    "dash": _SH_GRAMMAR,
    "ash": _SH_GRAMMAR,
    "ksh": _SH_GRAMMAR,
    "mksh": _SH_GRAMMAR,
    "pdksh": _SH_GRAMMAR,
    "oksh": _SH_GRAMMAR,
    "csh": _CSH_GRAMMAR,
    "tcsh": _CSH_GRAMMAR,
    "fish": _inline_grammar(
        inline="cC", flags="dDilnNPv", value="f",
        inline_long=("command", "init-command"),
        flags_long=(
            "debug", "interactive", "login", "no-execute", "no-config",
            "private", "version", "help",
        ),
        value_long=("features", "debug-output", "profile"),
    ),
    "elvish": _inline_grammar(
        inline="c", flags="i", inline_long=("c",),
        flags_long=("compileonly", "help", "version", "no-rc"),
    ),
    "nu": _NU_GRAMMAR,
    "nushell": _NU_GRAMMAR,
    "perl": _inline_grammar(
        inline="eEF", flags="acfhlnpsStTuUvwWX0123456789", value="CDiIVx",
        glued="d", checks={
            "m": _perl_module_verdict, "M": _perl_module_verdict,
            "d": _perl_debugger_verdict,
        },
    ),
    "ruby": _inline_grammar(inline="e", value="CEIr"),
    "php": _inline_grammar(
        inline="rRBE", value="ctzS", program="fF",
        checks={"d": _php_define_verdict},
        checks_long={"define": _php_define_verdict},
        inline_long=("run", "process-begin", "process-code", "process-end"),
    ),
    "node": _inline_grammar(
        inline="ep", flags="chiv", value="C", stdin="i",
        checks={"r": _data_url_verdict},
        inline_long=("eval", "print"),
        flags_long=(
            "check", "interactive", "version", "help", "no-warnings",
            "no-deprecation", "trace-warnings", "throw-deprecation",
            "pending-deprecation", "preserve-symlinks",
            "preserve-symlinks-main", "experimental-strip-types",
            "no-experimental-strip-types", "enable-source-maps", "watch",
            "test",
        ),
        stdin_long=("interactive",),
        value_long=(
            "conditions", "input-type", "title", "max-old-space-size",
            "env-file", "test-name-pattern", "test-reporter-destination",
            "redirect-warnings", "heapsnapshot-signal", "diagnostic-dir",
        ),
        data_long=(
            "import", "require", "loader", "experimental-loader",
            "test-reporter",
        ),
    ),
    "deno": _inline_grammar(
        flags="Aq", value="c",
        flags_long=(
            "allow-all", "quiet", "no-check", "no-lock", "no-prompt",
            "no-remote", "reload", "watch", "help", "version",
            "allow-read", "allow-write", "allow-net", "allow-env",
            "allow-run", "allow-sys", "allow-ffi", "allow-hrtime",
        ),
        value_long=(
            "config", "import-map", "lock", "ext", "cert", "location",
            "seed", "v8-flags", "inspect", "inspect-brk", "inspect-wait",
            "log-level", "permission-set",
        ),
        inline_long=("eval", "eval-file"),
        commands=("run", "repl", "test", "bench", "serve", "compile"),
        inline_commands=("eval",), stdin_commands=("repl",),
        remote_operands=True,
    ),
    "bun": _inline_grammar(
        inline="ep", flags="bhv", value="c",
        checks={"r": _data_url_verdict},
        inline_long=("eval", "print"),
        flags_long=(
            "bun", "hot", "watch", "smol", "no-install", "help", "version",
        ),
        value_long=("cwd", "config", "env-file", "tsconfig-override"),
        data_long=("preload", "require"),
        commands=("run", "test", "repl"),
        inline_commands=("exec",), stdin_commands=("repl",),
        remote_operands=True,
    ),
    "lua": _inline_grammar(
        inline="e", flags="ivW", value="l", stdin="i",
    ),
    "luajit": _inline_grammar(
        inline="e", flags="iv", value="ljOb", stdin="i",
    ),
}
_INLINE_GRAMMARS["nodejs"] = _INLINE_GRAMMARS["node"]
_AWK_NAMES = frozenset({"awk", "gawk", "mawk", "nawk"})

# py.exe launcher version selectors (-3, -3.12, -3.12-32, -V:Company/Tag).
_PY_LAUNCHER_SELECTOR = re.compile(r"-(?:[0-9]|V:)")


def _normalized_program_basename(executable):
    """Normalise an executable path to a casefolded Windows-alias-free basename.

    Handles both ``/`` and ``\\``, Windows drive prefixes (``C:python``),
    NTFS alternate data streams (``python.exe::$DATA``), and trailing dots or
    spaces (``powershell.exe.``, ``cmd.exe ``).
    """
    text = str(executable or "").strip()
    if not text:
        return ""
    normalised = posixpath.normpath(text.replace("\\", "/"))
    base = normalised.rsplit("/", 1)[-1]
    if len(base) >= 2 and base[1] == ":" and base[0].isascii() and base[0].isalpha():
        base = base[2:]
    if ":" in base:
        base = base.split(":", 1)[0]
    return base.rstrip(". ").lower()


def _strip_win_exec_ext(name):
    for ext in (".exe", ".com", ".bat", ".cmd"):
        if name.endswith(ext):
            return name[:-len(ext)]
    return name

# Option letters after which the rest of a short-option cluster is that
# option's value, so a code letter inside it is not an option: python's -W
# and -m take a value, and the py launcher's -V:TAG selector ends at the
# colon. Interpreters not listed treat every later letter as an option,
# which can only over-refuse.
_INLINE_CLUSTER_VALUE_STOPS = {"python": "wm:"}

# Wrappers whose purpose is to launch another program named in their own
# argv (``env python -c ...``, ``nice bash -c ...``). Every check here keys on
# the executable's basename, so a wrapper would carry the real interpreter
# past all of them; they are refused whenever they are given arguments.
_COMMAND_LAUNCHERS = frozenset({
    "busybox", "env", "nice", "nohup", "setsid", "stdbuf", "timeout", "xargs",
})


def _inline_interpreter_key(basename):
    """Normalize a resolved executable basename to an _INLINE_GRAMMARS key.

    Strips a trailing ".exe"/".com"/".bat"/".cmd", Windows aliases/ADS, maps
    the py/pyw launchers onto "py", any python* name (python3.12, pythonw,
    python3.13t) onto "python", and drops a version suffix from the others
    (perl5.36.0, ruby3.2, php8.2). Returns None when the basename is not a
    known inline-capable interpreter.
    """
    name = _strip_win_exec_ext(_normalized_program_basename(basename))
    if name in ("py", "pyw"):
        return "py"
    if name.startswith(("python", "pypy")):
        return "python"
    name = name.rstrip("0123456789.")
    return name if name in _INLINE_GRAMMARS else None


def _is_inline_code_arg(arg, flags, value_stops=""):
    """True if ``arg`` is one of ``flags`` including its value-glued forms."""
    for flag in flags:
        if arg == flag:
            return True
        if flag.startswith("--") and arg.startswith(flag + "="):
            return True
    letters = {
        flag[1].lower() for flag in flags
        if len(flag) == 2 and flag[0] == "-" and flag[1] != "-"
    }
    if letters and len(arg) >= 2 and arg[0] == "-" and arg[1] != "-":
        for char in arg[1:].lower():
            if char in letters:
                return True
            if char in value_stops:
                return False
    return False


_AWK_SHORT_VALUE = frozenset("FvfilEW")
_AWK_LONG_VALUE = frozenset({
    "--field-separator", "--assign", "--file", "--include", "--load", "--exec",
})


def _awk_inline_refusal(key, args, stdin=""):
    """Refuse awk invocations that supply inline or stdin program text."""
    inline_refusal = "inline %s code is disabled; use script_run" % key
    has_file = False
    reads_stdin = False
    index = 0
    while index < len(args):
        arg = args[index]
        index += 1
        if arg == "--":
            break
        if arg.startswith("--"):
            head, has_eq, val = arg.partition("=")
            head_l = head.lower()
            if len(head_l) >= 4 and "--source".startswith(head_l):
                return inline_refusal
            matched_file = len(head_l) >= 4 and "--file".startswith(head_l)
            matched_include = len(head_l) >= 4 and "--include".startswith(head_l)
            matched_exec = len(head_l) >= 4 and "--exec".startswith(head_l)
            if matched_file or matched_include or matched_exec or head_l in _AWK_LONG_VALUE:
                if not has_eq:
                    val = args[index] if index < len(args) else ""
                    index += 1
                if matched_file or matched_include or matched_exec:
                    if _is_stdin_operand(val):
                        reads_stdin = True
                    if matched_file or matched_exec:
                        has_file = True
                    if matched_exec:
                        break
            continue
        if arg.startswith("-") and len(arg) > 1:
            letters = arg[1:]
            stop_options = False
            for pos, ch in enumerate(letters):
                if ch == "e":
                    return inline_refusal
                if ch in _AWK_SHORT_VALUE:
                    rest = letters[pos + 1:]
                    if rest:
                        val = rest
                    else:
                        val = args[index] if index < len(args) else ""
                        index += 1
                    if ch in ("f", "i", "E"):
                        if _is_stdin_operand(val):
                            reads_stdin = True
                        if ch in ("f", "E"):
                            has_file = True
                        if ch == "E":
                            stop_options = True
                    elif ch == "W":
                        w_head, w_has_eq, w_val = val.partition("=")
                        w_name = w_head.strip().lower()
                        if len(w_name) >= 2 and "source".startswith(w_name):
                            return inline_refusal
                        w_file = len(w_name) >= 2 and "file".startswith(w_name)
                        w_include = len(w_name) >= 2 and "include".startswith(w_name)
                        w_exec = len(w_name) >= 2 and "exec".startswith(w_name)
                        if w_file or w_include or w_exec:
                            if not w_has_eq and index < len(args):
                                w_val = args[index]
                                index += 1
                            if _is_stdin_operand(w_val):
                                reads_stdin = True
                            if w_file or w_exec:
                                has_file = True
                            if w_exec:
                                stop_options = True
                    break
            if stop_options:
                break
            continue
        # First non-option operand ends option scanning.
        break
    if reads_stdin and str(stdin or "").strip():
        return "%s program text on stdin is disabled; use script_run" % key
    if not has_file:
        return inline_refusal
    return None


def _inline_code_refusal(key, args, stdin=""):
    """Why interpreter ``key`` run with ``args`` would run inline program text.

    Returns the refusal message, or None when the argv names a program file
    (or no program at all, with nothing on stdin).
    """
    grammar = _INLINE_GRAMMARS[key]
    if key == "py" and args and _PY_LAUNCHER_SELECTOR.match(args[0]):
        args = args[1:]
    refusal = "inline %s code is disabled; use script_run" % key
    uncertain = False       # an option the grammar cannot parse was seen
    operand_seen = False    # a program file (or possible one) was named
    reads_stdin = False
    pending = 0             # option values still to be consumed from argv
    first_operand = True
    index = 0
    while index < len(args):
        arg = args[index]
        index += 1
        if pending:
            pending -= 1
            if not arg.startswith("-"):
                continue
            # An option where a value was expected is parsed as an option:
            # refusing a malformed argv beats letting a flag slip past.
        if arg == "--" and not uncertain:
            if index < len(args):
                operand = args[index]
                operand_seen = True
                reads_stdin = reads_stdin or _is_stdin_operand(operand)
                if grammar["remote_operands"] and _is_remote_operand(operand):
                    return refusal
                if first_operand and operand in grammar["inline_commands"]:
                    return refusal
                if first_operand and operand in grammar["stdin_commands"]:
                    reads_stdin = True
            break
        if arg.startswith("--"):
            name, has_value, value = arg[2:].partition("=")
            name = name.replace("_", "-")
            if name in grammar["inline_long"]:
                return refusal
            if name in grammar["stdin_long"]:
                reads_stdin = True
            if name in grammar["data_long"]:
                if not has_value:
                    value = args[index] if index < len(args) else ""
                if _is_data_url(value):
                    return refusal
            check_long = grammar["checks_long"].get(name)
            if check_long is not None:
                check_val = value if has_value else (args[index] if index < len(args) else "")
                verdict = check_long(check_val)
                if verdict == "refuse":
                    return refusal
                reads_stdin = reads_stdin or verdict == "stdin"
            if name in grammar["value_long"] or name in grammar["data_long"]:
                pending += 0 if has_value else 1
            elif name not in grammar["flags_long"]:
                uncertain = True
            continue
        plus = grammar["plus"] and arg.startswith("+")
        if len(arg) > 1 and (arg.startswith("-") or plus):
            letters = arg[1:]
            for position, letter in enumerate(letters):
                # bash/sh still execute +c/+lc as an inline command string.
                if letter in grammar["inline"]:
                    return refusal
                if letter in grammar["after"]:
                    pending += 1
                    continue
                if letter in grammar["value"]:
                    rest = letters[position + 1:]
                    glued = letter in grammar["glued"]
                    value = rest
                    if not rest and not glued and index < len(args):
                        value = args[index]
                    check = grammar["checks"].get(letter)
                    if check is not None:
                        verdict = check(value)
                        if verdict == "refuse":
                            return refusal
                        reads_stdin = reads_stdin or verdict == "stdin"
                    if letter in grammar["program"] and _is_stdin_operand(value):
                        reads_stdin = True
                    if not rest and not glued:
                        pending += 1
                    if letter in grammar["program"]:
                        operand_seen = True
                        if grammar["ends_at_program"]:
                            index = len(args)
                    break
                if letter in grammar["stdin"]:
                    reads_stdin = True
                if letter not in grammar["flags"]:
                    uncertain = True
            continue
        # An operand: a subcommand, the program file, or stdin.
        if (first_operand or uncertain) and arg in grammar["inline_commands"]:
            return refusal
        if (first_operand or uncertain) and arg in grammar["commands"]:
            first_operand = False
            reads_stdin = reads_stdin or arg in grammar["stdin_commands"]
            continue
        first_operand = False
        operand_seen = True
        reads_stdin = reads_stdin or _is_stdin_operand(arg)
        if grammar["remote_operands"] and _is_remote_operand(arg):
            return refusal
        if not uncertain:
            break
    if not operand_seen or uncertain:
        reads_stdin = True
    if reads_stdin and str(stdin or "").strip():
        return "%s program text on stdin is disabled; use script_run" % key
    return None


_PS_SWITCH_TRANS = str.maketrans({"\u2013": "-", "\u2014": "-", "\u2015": "-"})
_PS_VALUE_FLAGS = frozenset({
    "-executionpolicy", "-ep", "-outputformat", "-of", "-inputformat", "-if",
    "-windowstyle", "-w", "-configurationname", "-config", "-workingdirectory",
    "-wd", "-psconsolefile", "-settingsfile", "-custompipename",
    "-encodedarguments", "-ea",
})
_PS_BOOL_FLAGS = frozenset({
    "-noprofile", "-nop", "-noninteractive", "-noni", "-nologo", "-nol",
    "-noexit", "-noe", "-sta", "-mta", "-login", "-l", "-help", "-h", "-?",
    "-v",
})


def _normalize_ps_switch(arg):
    text = str(arg).strip().lower().translate(_PS_SWITCH_TRANS)
    if text.startswith("/"):
        text = "-" + text[1:]
    head, sep, value = re.split(r"([:=])", text, maxsplit=1) if (":" in text or "=" in text) else (text, "", "")
    return head, bool(sep), value


def _is_inline_powershell_arg(arg):
    """Match PowerShell's executable-level abbreviations for inline code."""
    switch, _has_val, _val = _normalize_ps_switch(arg)
    if switch.startswith("--") and len(switch) >= 3:
        switch = switch[1:]
    # powershell.exe / pwsh accept /, unicode dashes, ":" or "=" value
    # separators, whitespace padding, pwsh "--" prefixes, every Command /
    # CommandWithArgs / EncodedCommand prefix, and the -ec / -cwa aliases.
    return (
        len(switch) >= 2
        and (
            "-command".startswith(switch)
            or "-commandwithargs".startswith(switch)
            or "-encodedcommand".startswith(switch)
            or switch in ("-ec", "-cwa")
        )
    )


def _powershell_names_script(lowered_args):
    """True if PowerShell argv names a script file with ``-File <path>``."""
    for index, arg in enumerate(lowered_args[:-1]):
        if len(arg) >= 2 and "-file".startswith(arg):
            return lowered_args[index + 1] != "-"
    return False


_STDIN_ALIAS_ROOTS = ("/dev", "/proc")

# Same bound as Linux's MAXSYMLINKS: a longer chain is refused, not followed.
_SYMLINK_HOP_LIMIT = 40


def _under_stdin_alias_root(path):
    """True if absolute POSIX ``path`` is /dev or /proc or lies under them."""
    # POSIX normpath keeps a leading "//"; collapse every leading slash.
    path = "/" + path.lstrip("/")
    return any(
        path == root or path.startswith(root + "/") for root in _STDIN_ALIAS_ROOTS
    )


def _posix_operand_mode(path):
    """Return the st_mode ``path`` finally names, or None to refuse it.

    ``path`` must be absolute and must not have been normalised: its "."
    and ".." components are applied here, physically. Components are taken
    left to right; a symlink is replaced by its target's components (relative
    to the link's directory) before anything after it, so ``link/..`` is the
    parent of the link's target, not of the link. Nothing inside /dev or
    /proc is followed. Links there (/dev/stdin, /dev/fd/0, /proc/self/fd/0)
    mean something different in each process: os.path.realpath here would
    follow them to the runtime's own fd 0, not the child's. So a component
    whose resolved path is /dev, /proc or under them is refused before it is
    read, as is a chain longer than _SYMLINK_HOP_LIMIT, a missing component,
    and a non-directory with components still to come. Only the literal
    /dev and /proc paths are recognised; a procfs or devfs mounted anywhere
    else is not detected.
    """
    pending = [part for part in path.split("/") if part]
    pending.reverse()
    current = "/"
    hops = 0
    while pending:
        name = pending.pop()
        if name == ".":
            continue
        if name == "..":
            current = os.path.dirname(current)
            continue
        candidate = current.rstrip("/") + "/" + name
        if _under_stdin_alias_root(candidate):
            return None
        try:
            info = os.lstat(candidate)
        except (OSError, ValueError):
            return None
        if stat.S_ISLNK(info.st_mode):
            hops += 1
            if hops > _SYMLINK_HOP_LIMIT:
                return None
            try:
                target = os.readlink(candidate)
            except (OSError, ValueError):
                return None
            if target.startswith("/"):
                current = "/"
            pending.extend(reversed([part for part in target.split("/") if part]))
            continue
        if pending and not stat.S_ISDIR(info.st_mode):
            return None
        current = candidate
    try:
        # lstat: a link swapped in since the walk is not followed either.
        return os.lstat(current).st_mode
    except (OSError, ValueError):
        return None


def _stdin_program_operand(args, working):
    """True if ``args`` could leave an interpreter reading its program from stdin.

    That is no operand at all, a first argument that is an option (``-``,
    ``-s``, ``-i``, ``+o``: the options' grammar is not modelled, so any
    leading option counts), or a first operand that is not an existing
    regular file once resolved against ``working``.

    On POSIX the operand is refused if its lexically normalised path is
    under /dev or /proc (``/./dev/stdin``, ``//dev/stdin``,
    ``../../dev/stdin``). That lexical check can only over-refuse; the
    deciding check is _posix_operand_mode on the joined path with its "."
    and ".." components left in, which applies ".." after following the
    symlink before it (``link/../fd/0`` with ``link`` pointing into
    /proc/self is refused) and refuses any component or symlink hop into
    /dev or /proc (a workspace symlink to ``/dev/stdin``) without following
    it, so the runtime's own fd 0 never decides the answer. A FIFO or
    directory is not a regular file and is refused too.

    The check runs before the child starts, so a concurrent call that swaps
    the operand for a link after it is made is not covered.
    """
    if not args:
        return True
    first = args[0]
    if first[:1] in ("-", "+"):
        return True
    # os.path.join keeps ``first`` as-is when it is absolute (or, on Windows,
    # rooted), so this is the path the child would open from ``working``.
    # Not normalised: a lexical ".." collapse would drop the symlink that
    # the kernel follows before applying it.
    base = str(working)
    if not os.path.isabs(base):
        base = os.path.join(os.getcwd(), base)
    joined = os.path.join(base, first)
    if os.name != "nt":
        if _under_stdin_alias_root(os.path.normpath(joined)):
            return True
        mode = _posix_operand_mode(joined)
        return mode is None or not stat.S_ISREG(mode)
    try:
        mode = os.stat(os.path.realpath(joined)).st_mode
    except (OSError, ValueError):
        return True
    return not stat.S_ISREG(mode)


def _refuse_inline_code(
    executable, args, *, has_stdin, allow_cmd_script, script_args_start,
    working,
):
    """Raise PermissionError when ``executable args`` would run ad-hoc code.

    Ad-hoc code -- an inline ``-c``/``-e`` string, a program fed on stdin, or
    a wrapper launching another program -- never passes through script_run's
    validation. ``script_args_start`` is set only by run_script, which built
    ``args`` with its script operand just before that index: what follows is
    the script's own argv and is not inspected, and the script operand means
    stdin is the script's input, not a program. Direct callers pass None and
    every argument is inspected, because the interpreters' option grammars
    (which options take a separate value) are not modelled here and guessing
    where options end could let a later ``-c`` through. ``working`` is the
    resolved directory the child will start in; a stdin-fed program operand
    is resolved against it.
    """
    basename = _interpreter_argv.program_name(executable)
    stem = basename
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if stem.endswith(suffix):
            stem = stem[:-len(suffix)]
            break
    inspected = args if script_args_start is None else args[:script_args_start]
    lowered_args = [arg.lower() for arg in inspected]
    stdin_is_program_input = has_stdin and script_args_start is None
    if stem in _COMMAND_LAUNCHERS and args:
        raise PermissionError(
            "%s launches the program named in its arguments; run that program"
            " directly or use script_run" % stem
        )
    if stem == "cmd" and not allow_cmd_script:
        raise PermissionError("inline cmd execution is disabled; use script_run")
    if Path(basename).suffix.lower() in (".bat", ".cmd") and not allow_cmd_script:
        raise PermissionError("batch files must be executed with script_run")
    if script_args_start is None:
        launcher = _interpreter_argv.command_string_launcher([basename, *inspected])
        if launcher:
            raise PermissionError(
                "%s launches the program named in its arguments; run that program"
                " directly or use script_run" % launcher
            )
    family = _interpreter_argv.interpreter_family(basename)
    if family in ("powershell", "pwsh"):
        if any(_is_inline_powershell_arg(arg) for arg in lowered_args) or (
            script_args_start is None and _interpreter_argv.runs_inline_code(family, inspected)
        ):
            raise PermissionError("inline PowerShell commands are disabled; use script_run")
        if stdin_is_program_input and (
            not _powershell_names_script(lowered_args)
            or _interpreter_argv.stdin_runs_as_code(family, inspected)
        ):
            raise PermissionError(
                "PowerShell would run stdin as commands; pass -File <script>"
                " or use script_run"
            )
    if script_args_start is None:
        verdict, detail = _interpreter_argv.analyze_argv_command(
            basename, inspected, stdin_present=stdin_is_program_input,
        )
        if verdict == "inline":
            if detail in ("powershell", "pwsh"):
                raise PermissionError("inline PowerShell commands are disabled; use script_run")
            if detail == "cmd" and not allow_cmd_script:
                raise PermissionError("inline cmd execution is disabled; use script_run")
            if detail != "cmd":
                raise PermissionError("inline %s code is disabled; use script_run" % detail)
        if verdict == "stdin":
            if detail in ("powershell", "pwsh"):
                raise PermissionError(
                    "PowerShell would run stdin as commands; pass -File <script>"
                    " or use script_run"
                )
            raise PermissionError(
                "%s would run stdin as its program; pass the script path as the"
                " first argument or use script_run" % detail
            )
    interp_key = _inline_interpreter_key(basename)
    if interp_key is None:
        return
    flags_key = "python" if interp_key == "py" else interp_key
    value_stops = _INLINE_CLUSTER_VALUE_STOPS.get(flags_key, "")
    if flags_key in _INLINE_CODE_FLAGS and any(
        _is_inline_code_arg(arg, _INLINE_CODE_FLAGS[flags_key], value_stops)
        for arg in lowered_args
    ):
        raise PermissionError(
            "inline %s code is disabled; use script_run" % interp_key
        )
    if stdin_is_program_input and _stdin_program_operand(args, working):
        raise PermissionError(
            "%s would run stdin as its program; pass the script path as the"
            " first argument or use script_run" % interp_key
        )


def _powershell_inline_refusal(args, stdin=""):
    """Refuse inline PowerShell commands, positional commands, and stdin code."""
    refusal = "inline PowerShell commands are disabled; use script_run"
    file_seen = False
    reads_stdin = False
    pending = 0
    index = 0
    while index < len(args):
        raw = args[index]
        index += 1
        if _is_inline_powershell_arg(raw):
            return refusal
        if pending:
            pending -= 1
            continue
        switch, has_val, value = _normalize_ps_switch(raw)
        if len(switch) >= 2 and "-file".startswith(switch):
            target = value if has_val else (args[index] if index < len(args) else "")
            if not has_val and index < len(args):
                index += 1
            if not target or _is_stdin_operand(target):
                reads_stdin = True
                break
            file_seen = True
            break
        if switch in _PS_VALUE_FLAGS:
            if not has_val:
                pending = 1
            continue
        if len(switch) >= 2 and "-version".startswith(switch):
            # Windows PowerShell -Version takes a version operand unless it's
            # the last arg or followed by another switch.
            if not has_val and index < len(args) and not _normalize_ps_switch(args[index])[0].startswith("-"):
                pending = 1
            continue
        if len(switch) >= 4 and "-noexit".startswith(switch):
            # -NoExit drops into the stdin command loop after running -File.
            reads_stdin = True
            continue
        if switch in _PS_BOOL_FLAGS or switch in ("--help", "--version"):
            continue
        if _is_stdin_operand(raw):
            reads_stdin = True
            break
        # Any positional argument or unrecognised switch (which Windows
        # PowerShell executes as the start of a -Command) fails closed.
        return refusal
    if (not file_seen or reads_stdin) and str(stdin or "").strip():
        return "PowerShell program text on stdin is disabled; use script_run"
    return None


def _launcher_key(basename):
    return _strip_win_exec_ext(_normalized_program_basename(basename))


_COMMAND_STRING_LAUNCHERS = frozenset({
    "forfiles", "wmic", "mshta", "rundll32", "wscript", "cscript",
    "regsvr32", "msiexec", "pcalua", "syncappvpublishingserver",
    "scriptrunner", "schtasks", "runas", "certutil", "bitsadmin",
    "cmstp", "infdefaultinstall", "odbcconf", "mavinject",
    "regasm", "regsvcs", "installutil", "diskshadow",
})


_LAUNCHER_SPECS = {
    "conhost": {
        "flags": frozenset({"--headless", "--forcev1", "--vtmode", "-headless", "/headless"}),
        "value": frozenset({"--width", "--height", "--signal", "--server", "--feature"}),
        "slash_as_dash": True,
    },
    "openconsole": {
        "flags": frozenset({"--headless", "--forcev1", "--vtmode"}),
        "value": frozenset({"--width", "--height", "--signal", "--server", "--feature"}),
        "slash_as_dash": True,
    },
    # env has its own getopt-style parser (_unwrap_env); its argv is
    # case-sensitive and -S re-feeds the split words through option parsing.
    "env": {"flags": frozenset(), "value": frozenset(), "parser": "env"},
    "nice": {
        "flags": frozenset({"--help", "--version"}),
        "value": frozenset({"-n", "--adjustment"}),
        "numeric_dash": True,
    },
    "nohup": {"flags": frozenset({"--help", "--version"}), "value": frozenset()},
    "setsid": {
        "flags": frozenset({"-c", "--ctty", "-f", "--fork", "-w", "--wait", "--help", "--version"}),
        "value": frozenset(),
    },
    "stdbuf": {
        "flags": frozenset({"--help", "--version"}),
        "value": frozenset({"-i", "--input", "-o", "--output", "-e", "--error"}),
    },
    "time": {
        "flags": frozenset({"-p", "-v", "--verbose", "-q", "--quiet", "-a", "--append", "--portability", "--help", "--version"}),
        "value": frozenset({"-f", "--format", "-o", "--output"}),
    },
    "timeout": {
        "flags": frozenset({"--preserve-status", "--foreground", "-v", "--verbose", "--help", "--version"}),
        "value": frozenset({"-k", "--kill-after", "-s", "--signal"}),
        "leading_operands": 1,
        # Windows timeout.exe only waits; it never runs a command. Its argv is
        # recognised by a leading switch (see _is_windows_timeout_syntax).
        "windows_timeout_bail": True,
    },
    "wsl": {
        "flags": frozenset({"--system", "--web-download", "--no-distribution", "--help", "--version", "-l", "--list", "--status", "--shutdown", "--terminate"}),
        "value": frozenset({"-d", "--distribution", "--distribution-id", "-u", "--user", "--cd", "--shell-type"}),
        "exec_flags": frozenset({"-e", "--exec"}),
        "stdin_if_no_inner": True,
        # A leading "~" only selects the home directory; it is not a program.
        "leading_skip": frozenset({"~"}),
        # Without -e/--exec, wsl.exe hands the rest of the command line to the
        # distro's default Linux shell, so it is shell text, not an argv.
        "shell_unless_exec": True,
    },
    "busybox": {"flags": frozenset(), "value": frozenset()},
    "toybox": {"flags": frozenset(), "value": frozenset()},
    "sudo": {
        "flags": frozenset({
            "-A", "-b", "-E", "-e", "-H", "-i", "-K", "-k", "-n", "-P", "-S", "-s", "-v",
            "--askpass", "--background", "--preserve-env", "--edit", "--set-home",
            "--login", "--remove-timestamp", "--reset-timestamp", "--non-interactive",
            "--preserve-groups", "--stdin", "--shell", "--validate", "--version", "--help",
        }),
        "value": frozenset({
            "-u", "-g", "-c", "-C", "-d", "-D", "-p", "-r", "-t", "-T", "-U",
            "--user", "--group", "--close-from", "--chdir", "--prompt",
            "--role", "--type", "--command-timeout", "--other-user",
        }),
        "stdin_flags": frozenset({"-s", "--shell", "-i", "--login"}),
        "shell_flags": frozenset({"-s", "--shell", "-i", "--login"}),
        "case_sensitive_short": True,
        "skip_env_assignments": True,
    },
    "doas": {"flags": frozenset({"-l", "-n", "-s"}), "value": frozenset({"-u", "-c"}), "stdin_flags": frozenset({"-s"})},
    "xargs": {"flags": frozenset(), "value": frozenset(), "refuse_any": True},
    "npx": {
        "flags": frozenset({"-y", "--yes", "-q", "--quiet", "--no", "--no-install", "--prefer-online", "--prefer-offline", "--offline", "--help", "--version"}),
        "value": frozenset({"-p", "--package", "--cache", "--userconfig", "--prefix", "--workspace"}),
        "refuse_flags": frozenset({"-c", "--call"}),
    },
    "pnpx": {
        "flags": frozenset({"-y", "--yes", "--help", "--version"}),
        "value": frozenset({"--package"}),
        "refuse_flags": frozenset({"-c", "--call"}),
    },
    "bunx": {
        "flags": frozenset({"--bun", "-y", "--help", "--version"}),
        "value": frozenset({"--cwd"}),
    },
    "uv": {
        "flags": frozenset({"-q", "--quiet", "-v", "--verbose", "--no-cache", "--offline", "--frozen", "--locked", "--isolated", "--no-project", "--no-sync", "--refresh", "--help", "--version"}),
        "value": frozenset({"--python", "-p", "--directory", "--project", "--with", "--with-editable", "--with-requirements", "--from", "--index-url", "--extra-index-url", "--default-index", "--index", "--cache-dir", "--script"}),
        "stdin_value": frozenset({"--script"}),
        "python_module_value": frozenset({"-m", "--module"}),
        "subcommands": frozenset({"run"}),
        "subcommand_sequences": (("tool", "run"),),
        "stdin_operand": True,
    },
    "uvx": {
        "flags": frozenset({"-q", "--quiet", "-v", "--verbose", "--no-cache", "--offline", "--isolated", "--refresh", "--help", "--version"}),
        "value": frozenset({"--from", "--python", "-p", "--with", "--with-editable", "--with-requirements", "--index-url", "--extra-index-url", "--default-index", "--index", "--cache-dir", "--directory", "--project"}),
    },
}
_LAUNCHER_SPECS["wslg"] = _LAUNCHER_SPECS["wsl"]


_WINDOWS_TIMEOUT_SWITCHES = frozenset({"/t", "/nobreak", "/?"})


def _is_windows_system_timeout(executable):
    """True when ``executable`` is the Windows System32/SysWOW64 timeout.exe."""
    if os.name != "nt" or not executable:
        return False
    root = os.environ.get("SystemRoot") or os.environ.get("windir")
    if not root:
        return False
    target = os.path.normcase(os.path.normpath(str(executable)))
    for sub in ("System32", "SysWOW64", "Sysnative"):
        candidate = os.path.normcase(os.path.normpath(os.path.join(root, sub, "timeout.exe")))
        if target == candidate:
            return True
    return False


def _is_windows_timeout_syntax(executable, args):
    """Windows timeout.exe argv: a leading /t, /nobreak or /? switch, or the
    resolved executable is the system timeout.exe (which never runs a command).

    Any other "/" argument (``/bin/sh``, a trailing ``/x``) is a POSIX path and
    does not stop the GNU timeout unwrap.
    """
    if args and args[0].lower() in _WINDOWS_TIMEOUT_SWITCHES:
        return True
    return _is_windows_system_timeout(executable)


# env's short options, read as getopt bundles ("+0iC:S:u:v" in GNU env, plus
# -a/--argv0 in newer coreutils). P, L and U take a value in BSD env and are
# invalid in GNU env; treating them as value options is right for both.
_ENV_SHORT_FLAGS = frozenset("0iv")
_ENV_SHORT_VALUE = frozenset("CuaPLU")
# GNU env long options and how each takes its argument. getopt_long accepts
# any unambiguous prefix ("--sp=...", "--split-str X"); an optional argument
# is only ever given with "=", never as the next element.
_ENV_LONG_OPTIONS = {
    "ignore-environment": "none",
    "null": "none",
    "debug": "none",
    "list-signal-handling": "none",
    "help": "none",
    "version": "none",
    "unset": "required",
    "chdir": "required",
    "argv0": "required",
    "split-string": "split",
    "block-signal": "optional",
    "default-signal": "optional",
    "ignore-signal": "optional",
}
# env -S has its own escapes (\_ is a word break, \c ends the string), ${VAR}
# expansion and "#" comments, none of which shlex reproduces; control
# characters other than tab may split differently. Such strings are refused.
_ENV_SPLIT_UNCHECKABLE = re.compile(r"[\\$#\x00-\x08\x0a-\x1f\x7f]")
_ENV_MAX_SPLIT_DEPTH = 8


def _env_split(value, rest, depth):
    """Split an env -S string and re-read it, then ``rest``, as env argv."""
    if _ENV_SPLIT_UNCHECKABLE.search(value):
        raise PermissionError(
            "env -S string with escapes, ${VAR}, comments or control "
            "characters cannot be checked; use script_run"
        )
    try:
        tokens = shlex.split(value)
    except ValueError as exc:
        raise PermissionError("malformed env -S command string; use script_run") from exc
    return _unwrap_env([*tokens, *rest], depth + 1)


def _unwrap_env(args, depth=0):
    """Extract the command candidates from an ``env`` argv.

    Parses env's options the way getopt does: short options are bundles
    (``-iSsh -c id``), an ``S`` takes the rest of its element or the next
    element as the split string, and long options match by unambiguous prefix.
    GNU env feeds the split words back through option parsing, so they are
    re-read here as env argv (``-S '-i sh -c id'``).

    Returns ``(candidates, uncertain)``. After an option that cannot be parsed
    (``uncertain``), it may have consumed the next element, so every later
    operand is a candidate and later option-looking elements are still parsed.
    An unparseable short bundle that contains ``S`` after the unknown letter is
    refused, since some env could read it as a split string.
    """
    if depth >= _ENV_MAX_SPLIT_DEPTH:
        raise PermissionError("env -S nesting exceeds limit; use script_run")
    candidates = []
    uncertain = False
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            index += 1
            if uncertain:
                # "--" may be the value of the unknown option; keep parsing.
                continue
            while index < len(args) and "=" in args[index]:
                index += 1
            if index < len(args):
                candidates.append((args[index], args[index + 1:]))
            return candidates, False
        if arg == "-":
            # A lone "-" means -i.
            index += 1
            continue
        if arg.startswith("--"):
            name, has_eq, value = arg[2:].partition("=")
            if name in _ENV_LONG_OPTIONS:
                matches = [name]
            elif name:
                matches = [opt for opt in _ENV_LONG_OPTIONS if opt.startswith(name)]
            else:
                matches = []
            kinds = {_ENV_LONG_OPTIONS[opt] for opt in matches}
            if "split" in kinds:
                if has_eq:
                    split_value, step = value, 1
                elif index + 1 < len(args):
                    split_value, step = args[index + 1], 2
                else:
                    return candidates, uncertain
                inner, inner_uncertain = _env_split(split_value, args[index + step:], depth)
                return [*candidates, *inner], uncertain or inner_uncertain
            if len(matches) != 1:
                # Unknown or ambiguous: GNU env rejects it; another env may not.
                uncertain = True
                index += 1
                continue
            kind = kinds.pop()
            if kind == "required" and not has_eq:
                index += 2
                continue
            if kind == "none" and has_eq:
                uncertain = True
            index += 1
            continue
        if arg.startswith("-"):
            bundle = arg[1:]
            step = 1
            for pos, letter in enumerate(bundle):
                if letter in _ENV_SHORT_FLAGS:
                    continue
                if letter == "S":
                    tail = bundle[pos + 1:]
                    if tail:
                        split_value, step = tail, 1
                    elif index + 1 < len(args):
                        split_value, step = args[index + 1], 2
                    else:
                        return candidates, uncertain
                    inner, inner_uncertain = _env_split(split_value, args[index + step:], depth)
                    return [*candidates, *inner], uncertain or inner_uncertain
                if letter in _ENV_SHORT_VALUE:
                    if pos + 1 == len(bundle):
                        step = 2
                    break
                if "S" in bundle[pos + 1:]:
                    raise PermissionError(
                        "env option %r cannot be parsed and may carry -S; "
                        "use script_run" % arg
                    )
                uncertain = True
                break
            index += step
            continue
        if "=" in arg:
            # NAME=VALUE assignment (or, when uncertain, an option's value).
            index += 1
            continue
        candidates.append((arg, args[index + 1:]))
        if not uncertain:
            return candidates, False
        index += 1
    return candidates, uncertain


def _unwrap_launcher(key, args, stdin="", executable=""):
    """Extract inner command candidates from a known launcher's argv.

    Returns ``(candidates, uncertain, reads_stdin)`` where ``candidates`` is a
    list of ``(prog, inner_args)`` tuples to guard-check. When ``uncertain`` is
    True, an unknown option may have shifted the command boundary, so every
    plausible token after the uncertain flag is included as a candidate.

    Raises PermissionError when the launcher would hand the remaining command
    line to a shell (``wsl`` without ``-e``/``--exec``).
    """
    spec = _LAUNCHER_SPECS[key]
    if spec.get("refuse_any") and (args or str(stdin or "").strip()):
        raise PermissionError(
            "%s launches commands constructed from its arguments or stdin; "
            "use script_run" % key
        )
    if spec.get("parser") == "env":
        candidates, uncertain = _unwrap_env(list(args))
        return candidates, uncertain, False
    if spec.get("windows_timeout_bail") and _is_windows_timeout_syntax(executable, args):
        return [], False, False
    uncertain = False
    reads_stdin = False
    exec_seen = False
    shell_flag_seen = False
    skip_operands = spec.get("leading_operands", 0)
    subcommands = spec.get("subcommands", ())
    subcommand_sequences = spec.get("subcommand_sequences", ())
    subcommand_seen = not bool(subcommands or subcommand_sequences)
    pending_seq = None
    refuse_flags = spec.get("refuse_flags", ())
    shell_flags = spec.get("shell_flags", ())
    stdin_value = spec.get("stdin_value", ())
    python_module_value = spec.get("python_module_value", ())
    case_sensitive_short = bool(spec.get("case_sensitive_short"))
    skip_env_assignments = bool(spec.get("skip_env_assignments"))
    short_flags = frozenset(f[1] for f in spec["flags"] if len(f) == 2 and f.startswith("-"))
    short_value = frozenset(f[1] for f in spec["value"] if len(f) == 2 and f.startswith("-"))
    short_refuse = frozenset(f[1] for f in refuse_flags if len(f) == 2 and f.startswith("-"))
    short_shell = frozenset(f[1] for f in shell_flags if len(f) == 2 and f.startswith("-"))
    index = 0
    if args and args[0] in spec.get("leading_skip", ()):
        index = 1
    while index < len(args):
        arg = args[index]
        lowered = arg.lower()
        if spec.get("slash_as_dash") and lowered.startswith("/"):
            lowered = "-" + lowered[1:]
        is_short_arg = arg.startswith("-") and not arg.startswith("--")
        match_arg = arg if (case_sensitive_short and is_short_arg) else lowered
        if arg == "--":
            index += 1 + skip_operands
            break
        if match_arg in spec.get("exec_flags", ()):
            exec_seen = True
            index += 1
            break
        head, has_eq, eq_val = match_arg.partition("=")
        if head in refuse_flags:
            raise PermissionError(
                "%s %s executes an inline shell command; use script_run" % (key, head)
            )
        if subcommand_seen and python_module_value:
            if head in python_module_value:
                mod_val = (
                    arg.partition("=")[2]
                    if has_eq
                    else (args[index + 1] if index + 1 < len(args) else "")
                )
                if _python_module_verdict(mod_val) == "refuse":
                    raise PermissionError(
                        "inline %s -m %s code is disabled; use script_run" % (key, mod_val)
                    )
                if not mod_val:
                    uncertain = True
                    index += 1
                    continue
                return [], uncertain, reads_stdin
            if is_short_arg and len(arg) > 2 and arg.startswith("-m") and "-m" in python_module_value:
                mod_val = arg[2:]
                if _python_module_verdict(mod_val) == "refuse":
                    raise PermissionError(
                        "inline %s -m %s code is disabled; use script_run" % (key, mod_val)
                    )
                return [], uncertain, reads_stdin
        if match_arg in shell_flags:
            shell_flag_seen = True
            reads_stdin = True
        if match_arg in spec.get("stdin_flags", ()):
            reads_stdin = True
        if match_arg in spec["flags"]:
            index += 1
            continue
        if head in spec["value"]:
            if head in stdin_value:
                val = (
                    arg.partition("=")[2]
                    if has_eq
                    else (args[index + 1] if index + 1 < len(args) else "")
                )
                if _is_stdin_operand(val):
                    reads_stdin = True
            index += 1 if has_eq else 2
            continue
        if (
            len(match_arg) > 2 and match_arg.startswith("-") and not match_arg.startswith("--")
            and (short_refuse or short_shell)
        ):
            bundle = match_arg[1:]
            all_known = True
            consumed_next = False
            for pos, ch in enumerate(bundle):
                if ch in short_refuse:
                    raise PermissionError(
                        "%s -%s executes an inline shell command; use script_run" % (key, ch)
                    )
                if ch in short_shell:
                    shell_flag_seen = True
                    reads_stdin = True
                if ch in short_value:
                    if pos + 1 == len(bundle):
                        consumed_next = True
                    break
                if ch not in short_flags:
                    all_known = False
                    break
            if all_known:
                index += 2 if consumed_next else 1
                continue
        if spec.get("numeric_dash") and re.fullmatch(r"-\d+", lowered):
            index += 1
            continue
        if subcommand_seen and spec.get("stdin_operand") and _is_stdin_operand(arg):
            reads_stdin = True
            return [], uncertain, True
        if arg.startswith("-") or (spec.get("slash_as_dash") and arg.startswith("/")):
            uncertain = True
            index += 1
            continue
        if not subcommand_seen:
            if pending_seq is not None:
                if lowered == pending_seq[0]:
                    pending_seq = pending_seq[1:] or None
                    if pending_seq is None:
                        subcommand_seen = True
                    index += 1
                    continue
                return [], False, False
            if lowered in subcommands:
                subcommand_seen = True
                index += 1
                continue
            matched_prefix = next((seq for seq in subcommand_sequences if seq and seq[0] == lowered), None)
            if matched_prefix is not None:
                if len(matched_prefix) == 1:
                    subcommand_seen = True
                else:
                    pending_seq = matched_prefix[1:]
                index += 1
                continue
            return [], False, False
        if skip_operands > 0:
            skip_operands -= 1
            index += 1
            continue
        if skip_env_assignments and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", arg):
            index += 1
            continue
        break
    if skip_env_assignments:
        while index < len(args) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", args[index]):
            index += 1
    if index >= len(args):
        if spec.get("stdin_if_no_inner") or reads_stdin:
            reads_stdin = True
        return [], uncertain, reads_stdin
    if shell_flag_seen:
        raise PermissionError(
            "%s shell mode executes its arguments through a shell; use script_run" % key
        )
    if spec.get("shell_unless_exec") and not exec_seen:
        raise PermissionError(
            "%s command line without -e/--exec runs through the default shell; "
            "use script_run" % key
        )
    if subcommand_seen and spec.get("stdin_operand") and _is_stdin_operand(args[index]):
        return [], uncertain, True
    if (spec.get("stdin_if_no_inner") or reads_stdin) and uncertain:
        reads_stdin = True
    if not uncertain:
        return [(args[index], args[index + 1:])], False, reads_stdin
    candidates = []
    for pos in range(index, len(args)):
        if not args[pos].startswith("-"):
            candidates.append((args[pos], args[pos + 1:]))
    return candidates, True, reads_stdin


def _bun_x_remainder(args):
    """Return the argv after ``bun [flags...] x``, or None when not ``bun x``."""
    grammar = _INLINE_GRAMMARS["bun"]
    uncertain = False
    pending = 0
    index = 0
    while index < len(args):
        arg = args[index]
        index += 1
        if pending:
            pending -= 1
            if not arg.startswith("-"):
                continue
        if arg == "--":
            return None
        if arg.startswith("--"):
            name, has_val, _val = arg[2:].partition("=")
            name = name.replace("_", "-")
            if name in grammar["value_long"] or name in grammar["data_long"]:
                pending += 0 if has_val else 1
            elif name not in grammar["flags_long"]:
                uncertain = True
            continue
        if len(arg) > 1 and arg.startswith("-"):
            for pos, ch in enumerate(arg[1:]):
                if ch in grammar["value"]:
                    if not arg[1:][pos + 1:]:
                        pending += 1
                    break
                if ch not in grammar["flags"]:
                    uncertain = True
            continue
        if arg.lower() == "x":
            return args[index:]
        if not uncertain:
            return None
    return None


def _check_program_guards(
    executable, args, stdin="", *, _allow_cmd_script=False, _depth=0,
):
    """Refuse inline code, cmd/batch escapes, and launcher-wrapped payloads."""
    if _depth >= 8:
        raise PermissionError("launcher nesting exceeds limit; use script_run")
    basename = _normalized_program_basename(executable)
    stem = _strip_win_exec_ext(basename)
    if stem in _COMMAND_STRING_LAUNCHERS:
        raise PermissionError(
            "%s decodes and executes its own command string; use script_run" % stem
        )
    launcher = _launcher_key(basename)
    if launcher in _LAUNCHER_SPECS:
        candidates, _uncertain, reads_stdin = _unwrap_launcher(
            launcher, args, stdin, executable=executable,
        )
        if reads_stdin and str(stdin or "").strip():
            raise PermissionError(
                "%s program text on stdin is disabled; use script_run" % launcher
            )
        for inner_prog, inner_args in candidates:
            _check_program_guards(
                inner_prog, inner_args, stdin,
                _allow_cmd_script=False, _depth=_depth + 1,
            )
        return
    if stem in ("powershell", "powershell_ise", "pwsh"):
        refusal = _powershell_inline_refusal(args, stdin)
        if refusal:
            raise PermissionError(refusal)
        return
    if stem == "cmd" and not _allow_cmd_script:
        raise PermissionError("inline cmd execution is disabled; use script_run")
    if Path(basename).suffix.lower() in (".bat", ".cmd") and not _allow_cmd_script:
        raise PermissionError("batch files must be executed with script_run")
    if stem.rstrip("0123456789.") in _AWK_NAMES:
        refusal = _awk_inline_refusal(stem, args, stdin)
        if refusal:
            raise PermissionError(refusal)
        return
    interp_key = _inline_interpreter_key(basename)
    refusal = interp_key and _inline_code_refusal(interp_key, args, stdin)
    if refusal:
        raise PermissionError(refusal)
    if interp_key == "bun":
        bun_x_args = _bun_x_remainder(args)
        if bun_x_args is not None:
            candidates, _uncertain, reads_stdin = _unwrap_launcher(
                "bunx", bun_x_args, stdin, executable=executable,
            )
            if reads_stdin and str(stdin or "").strip():
                raise PermissionError(
                    "bun x program text on stdin is disabled; use script_run"
                )
            for inner_prog, inner_args in candidates:
                _check_program_guards(
                    inner_prog, inner_args, stdin,
                    _allow_cmd_script=False, _depth=_depth + 1,
                )


def run_program(
    program, *, args_json="[]", cwd=".", stdin="", timeout=30,
    max_output=MAX_EXEC_OUTPUT, extra_roots="", bypass=False,
    _allow_cmd_script=False, _pass_fds=(),
):
    return _run_program(
        program, args_json=args_json, cwd=cwd, stdin=stdin, timeout=timeout,
        max_output=max_output, extra_roots=extra_roots, bypass=bypass,
        allow_cmd_script=_allow_cmd_script, pass_fds=_pass_fds,
        script_args_start=None,
    )


def _run_program(
    program, *, args_json, cwd, stdin, timeout, max_output, extra_roots,
    bypass, allow_cmd_script, pass_fds, script_args_start,
):
    """run_program's body; ``script_args_start`` is run_script's alone.

    Kept off the public signature so a tool call forwarding its arguments to
    run_program can never mark its own argv as already-vetted script input.
    """
    if str(program).strip().casefold() in {"bash", "sh"}:
        executable = runtime_paths.bash_executable()
        if not executable:
            raise FileNotFoundError("bash/sh executable not found")
    else:
        executable = _resolve_program(program, extra_roots=extra_roots, bypass=bypass)
    args = _json_args(args_json)
    input_bytes = str(stdin or "").encode("utf-8")
    _check_program_guards(
        executable, args, stdin, _allow_cmd_script=_allow_cmd_script,
    )
    working = _resolve(cwd, extra_roots=extra_roots, bypass=bypass)
    _refuse_inline_code(
        executable, args, has_stdin=bool(input_bytes),
        allow_cmd_script=allow_cmd_script, script_args_start=script_args_start,
        working=working,
    )
    if not working.is_dir():
        raise ValueError("working directory is not a directory: %s" % working)
    timeout = _bounded_int(timeout, 30, 1, MAX_EXEC_TIMEOUT)
    output_limit = _bounded_int(max_output, MAX_EXEC_OUTPUT, 1, MAX_EXEC_OUTPUT)
    if len(input_bytes) > 256_000:
        raise ValueError("stdin exceeds 256000 bytes")
    command = [executable, *args]
    started = time.time()
    timed_out = False
    proc = subprocess.Popen(
        command,
        cwd=str(working),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        # Own process group (POSIX session / Windows group) so the whole tree
        # can be torn down; see process_tree.
        **process_tree.group_popen_kwargs(
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        ),
        # Only run_script's sealed exact-handoff descriptor is ever passed.
        pass_fds=tuple(pass_fds),
        # Strip control-plane secrets before handing the environment to a
        # model-directed child. Without this, a .py run through script_run or
        # workspace_run could base64-dump os.environ and read back
        # SONDER_API_KEY and the approval/bypass gates -- exactly what the
        # code_runner lane was hardened against and this lane was not.
        env=_run_program_environment(),
    )
    tree = (
        process_tree.ProcessTree.owning(proc)
        if process_tree.is_real_popen(proc) else None
    )
    stdout_bytes = bytearray()
    stderr_bytes = bytearray()
    stdout_state = {"bytes": 0}
    stderr_state = {"bytes": 0}
    started_threads = []
    try:
        readers = [
            owned_runtime_thread(
                target=_drain_pipe,
                args=(proc.stdout, stdout_bytes, stdout_state, output_limit),
                daemon=True,
            ),
            owned_runtime_thread(
                target=_drain_pipe,
                args=(proc.stderr, stderr_bytes, stderr_state, output_limit),
                daemon=True,
            ),
        ]
        writer = owned_runtime_thread(
            target=_write_stdin, args=(proc.stdin, input_bytes), daemon=True,
        )
        for thread in (*readers, writer):
            thread.start()
            started_threads.append(thread)
    except BaseException:
        # The child is already running, detached into its own process group.
        # If the owned-thread factory refuses a reader or the writer, nothing
        # would ever wait on, time out, or kill it: reap it here instead.
        _abandon_child(proc, started_threads, tree)
        raise
    try:
        try:
            _wait_leader(proc, timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            # Whether the leader exited or timed out, end whatever it left behind
            # in its group/job, then reap it. A leader that will not die within
            # the grace period is reported with returncode None, not raised.
            _terminate_process_tree(proc, tree)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        writer.join(timeout=2)
        for thread in readers:
            thread.join(timeout=2)
    except BaseException:
        _abandon_child(proc, [*readers, writer], tree)
        raise
    stdout_truncated = stdout_state["bytes"] > output_limit
    stderr_truncated = stderr_state["bytes"] > output_limit
    stdout = bytes(stdout_bytes).decode("utf-8", errors="replace")
    stderr = bytes(stderr_bytes).decode("utf-8", errors="replace")
    return {
        "ok": not timed_out and proc.returncode == 0,
        "program": executable,
        "command": command,
        "cwd": str(working),
        "returncode": proc.returncode,
        "timed_out": timed_out,
        "elapsed_ms": int((time.time() - started) * 1000),
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
    }


_SEALED_PYTHON_MAIN = Path(__file__).with_name("sealed_python_main.py")


def _sealed_script_command(script, sealed, args):
    """Build the argv that executes ``sealed``'s memfd instead of ``script``.

    ``sealed`` is an ``artifact_risk.SealedScript`` produced for exactly this
    script.  Python runs through ``sealed_python_main.py``, which reads the
    code from the inherited descriptor and restores ``__file__``,
    ``sys.argv[0]`` and ``sys.path[0]`` from the script path.  Bash reads
    ``/proc/self/fd/N`` (the same sealed memfd); ``$0`` and ``BASH_SOURCE``
    are therefore that ``/proc`` path, not the script path.
    """
    if not isinstance(sealed.fd, int) or sealed.fd < 0:
        raise PermissionError("sealed script descriptor is invalid")
    if Path(sealed.path) != script or sealed.suffix != script.suffix.lower():
        raise PermissionError("sealed script does not match the requested script")
    if sealed.suffix == ".py":
        return sys.executable, [
            "-P", str(_SEALED_PYTHON_MAIN), str(sealed.fd), str(script), *args,
        ]
    if sealed.suffix == ".sh":
        executable = runtime_paths.bash_executable()
        if not executable:
            raise FileNotFoundError("runner is not installed for .sh")
        return executable, ["/proc/self/fd/%d" % sealed.fd, *args]
    raise PermissionError("no exact-handoff runner for %s" % sealed.suffix)


def run_script(
    path, *, args_json="[]", cwd="", stdin="", timeout=30,
    max_output=MAX_EXEC_OUTPUT, extra_roots="", bypass=False,
    sealed_script=None,
):
    """Run ``path`` with its known interpreter.

    ``sealed_script`` (an ``artifact_risk.SealedScript``) makes the child
    execute the sealed, already-inspected bytes rather than reopening the
    path; the path still decides cwd and the script's visible identity.
    """
    script = _resolve(path, extra_roots=extra_roots, bypass=bypass)
    if not script.is_file():
        raise FileNotFoundError(str(script))
    suffix = script.suffix.lower()
    args = _json_args(args_json)
    if sealed_script is not None:
        executable, argv = _sealed_script_command(script, sealed_script, args)
        result = _run_program(
            executable,
            args_json=argv,
            cwd=cwd or str(script.parent),
            stdin=stdin,
            timeout=timeout,
            max_output=max_output,
            extra_roots=extra_roots,
            bypass=bypass,
            allow_cmd_script=False,
            pass_fds=(sealed_script.fd,),
            # The caller's args follow the sealed runner's own argv.
            script_args_start=len(argv) - len(args),
        )
        result["exact_handoff"] = {
            "mechanism": sealed_script.mechanism,
            "script": str(script),
            "bytes": sealed_script.size,
            "sha256": sealed_script.sha256,
        }
        return result
    if suffix == ".py":
        executable, prefix = sys.executable, [str(script)]
    elif suffix == ".ps1":
        executable = shutil.which("pwsh") or shutil.which("powershell")
        prefix = ["-NoProfile", "-File", str(script)]
    elif suffix in (".cmd", ".bat"):
        cmd_meta = re.compile(r"[&|<>^%!()\r\n]")
        if cmd_meta.search(str(script)) or any(cmd_meta.search(arg) for arg in args):
            raise PermissionError("unsafe batch path or argument metacharacters")
        executable = shutil.which("cmd") or os.environ.get("COMSPEC", "cmd.exe")
        prefix = ["/d", "/c", str(script)]
    elif suffix == ".js":
        executable, prefix = shutil.which("node"), [str(script)]
    elif suffix == ".dart":
        executable, prefix = shutil.which("dart"), [str(script)]
    elif suffix == ".sh":
        executable, prefix = runtime_paths.bash_executable(), [str(script)]
    elif suffix in (".exe", ".com"):
        executable, prefix = str(script), []
    else:
        raise ValueError("unsupported script type: %s" % (suffix or "(none)"))
    if not executable:
        raise FileNotFoundError("runner is not installed for %s" % suffix)
    return _run_program(
        executable,
        args_json=[*prefix, *args],
        cwd=cwd or str(script.parent),
        stdin=stdin,
        timeout=timeout,
        max_output=max_output,
        extra_roots=extra_roots,
        bypass=bypass,
        allow_cmd_script=suffix in (".cmd", ".bat"),
        pass_fds=(),
        # args after the script operand are the script's own; a .exe/.com
        # runs itself (no prefix), so its whole argv stays inspected.
        script_args_start=len(prefix) if prefix else None,
    )


def _jpeg_dimensions(path: Path):
    with path.open("rb") as handle:
        if handle.read(2) != b"\xff\xd8":
            return None
        while True:
            byte = handle.read(1)
            if not byte:
                return None
            if byte != b"\xff":
                continue
            marker = handle.read(1)
            while marker == b"\xff":
                marker = handle.read(1)
            if not marker or marker in (b"\xd8", b"\xd9"):
                continue
            raw_length = handle.read(2)
            if len(raw_length) != 2:
                return None
            length = struct.unpack(">H", raw_length)[0]
            if length < 2:
                return None
            if marker[0] in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                payload = handle.read(5)
                if len(payload) == 5:
                    height, width = struct.unpack(">HH", payload[1:5])
                    return width, height
                return None
            handle.seek(max(0, length - 2), 1)


def image_inspect(path, *, extra_roots="", bypass=False):
    target = _resolve(path, extra_roots=extra_roots, bypass=bypass)
    if not target.is_file():
        raise FileNotFoundError(str(target))
    size = target.stat().st_size
    if size > MAX_IMAGE_BYTES:
        raise ValueError("image exceeds %d bytes" % MAX_IMAGE_BYTES)
    with target.open("rb") as handle:
        header = handle.read(64)
    image_format = "unknown"
    width = height = None
    mime = "application/octet-stream"
    if header.startswith(b"\x89PNG\r\n\x1a\n") and len(header) >= 24:
        image_format, mime = "PNG", "image/png"
        width, height = struct.unpack(">II", header[16:24])
    elif header.startswith((b"GIF87a", b"GIF89a")) and len(header) >= 10:
        image_format, mime = "GIF", "image/gif"
        width, height = struct.unpack("<HH", header[6:10])
    elif header.startswith(b"BM") and len(header) >= 26:
        image_format, mime = "BMP", "image/bmp"
        width, height = struct.unpack("<ii", header[18:26])
        height = abs(height)
    elif header.startswith(b"\xff\xd8"):
        image_format, mime = "JPEG", "image/jpeg"
        dimensions = _jpeg_dimensions(target)
        if dimensions:
            width, height = dimensions
    elif header.startswith((b"P3", b"P6")):
        image_format, mime = "PPM", "image/x-portable-pixmap"
        with target.open("r", encoding="ascii", errors="replace") as handle:
            sample = handle.read(65536)
        tokens = []
        for line in sample.splitlines():
            clean = line.split("#", 1)[0]
            tokens.extend(clean.split())
            if len(tokens) >= 4:
                break
        if len(tokens) >= 3:
            width, height = int(tokens[1]), int(tokens[2])
    elif target.suffix.lower() == ".svg" or b"<svg" in header.lower():
        image_format, mime = "SVG", "image/svg+xml"
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(65536)
        width_match = re.search(r"\bwidth=[\"']([0-9.]+)", text, re.I)
        height_match = re.search(r"\bheight=[\"']([0-9.]+)", text, re.I)
        viewbox = re.search(r"\bviewBox=[\"']([^\"']+)[\"']", text, re.I)
        if width_match and height_match:
            width, height = int(float(width_match.group(1))), int(float(height_match.group(1)))
        elif viewbox:
            values = viewbox.group(1).replace(",", " ").split()
            if len(values) >= 4:
                width, height = int(float(values[2])), int(float(values[3]))
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return {
        "path": str(target),
        "format": image_format,
        "mime": mime,
        "width": width,
        "height": height,
        "bytes": size,
        "sha256": digest.hexdigest(),
        "note": "metadata/header inspection only; no visual-semantic claim",
    }
