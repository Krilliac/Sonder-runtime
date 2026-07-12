"""Guarded local workbench discovery, inspection, and bounded execution.

The module is stdlib-only and deliberately keeps policy in ``file_ops``. It
never accepts a shell command string: execution is argv-based, workspace paths
must resolve through guarded roots, traversal is bounded, process trees are
timed out, and output pipes are continuously drained with capped retention.
"""
from __future__ import annotations

import fnmatch
import hashlib
import heapq
import json
import os
import re
import signal
import shutil
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import file_ops


MAX_TREE_ENTRIES = 500
MAX_SEARCH_RESULTS = 500
MAX_SEARCH_FILE_BYTES = 2_000_000
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


def _note_skip(state, path, reason):
    state["skipped_entries"] += 1
    counts = state["skipped_by_reason"]
    counts[reason] = counts.get(reason, 0) + 1
    if len(state["skipped_examples"]) < 20:
        state["skipped_examples"].append({"path": str(path), "reason": reason})


def _bounded_walk(
    root: Path, *, include_hidden=False, include_ignored=False,
    max_entries=DEFAULT_WALK_ENTRIES, timeout_seconds=DEFAULT_WALK_SECONDS,
):
    """Return a deterministic guarded-tree iterator and mutable scan state.

    The caller already resolved ``root`` through ``file_ops``. Entries are
    yielded depth-first in case-insensitive path order. Symlinks are never
    followed. Both inspected directory entries and wall time are bounded, so a
    small output limit cannot hide an unbounded filesystem walk.
    """
    max_entries = _bounded_int(
        max_entries, DEFAULT_WALK_ENTRIES, 1, MAX_WALK_ENTRIES,
    )
    timeout_seconds = _bounded_float(
        timeout_seconds, DEFAULT_WALK_SECONDS, 0.01, MAX_WALK_SECONDS,
    )
    state = _new_walk_state(root, max_entries, timeout_seconds)
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
                    try:
                        if child.is_symlink():
                            _note_skip(state, path, "symlink")
                            continue
                        if not include_hidden and _hidden(child.name):
                            _note_skip(state, path, "hidden")
                            continue
                        is_dir = child.is_dir(follow_symlinks=False)
                        if is_dir and _skip_dir(child.name, include_ignored):
                            _note_skip(state, path, "ignored_directory")
                            continue
                        size = 0 if is_dir else child.stat(follow_symlinks=False).st_size
                    except (OSError, PermissionError):
                        _note_skip(state, path, "unreadable_entry")
                        continue
                    relative = str(path.relative_to(root))
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
            if child.is_symlink():
                skipped += 1
                continue
            if not include_hidden and _hidden(child.name):
                skipped += 1
                continue
            is_dir = child.is_dir(follow_symlinks=False)
            if is_dir and child.name in SKIP_DIRS:
                skipped += 1
                continue
            candidate = Path(child.path)
            try:
                size = child.stat(follow_symlinks=False).st_size if not is_dir else 0
            except OSError:
                size = 0
            entries.append({
                "path": str(candidate),
                "relative": str(candidate.relative_to(root)) or ".",
                "name": child.name,
                "type": "dir" if is_dir else "file",
                "depth": level,
                "bytes": size,
            })
            if is_dir and level < depth:
                visit(candidate, level + 1)

    visit(root, 1)
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
        raise FileNotFoundError(str(target))
    start = _bounded_int(start_line, 1, 1, 10_000_000)
    end = _bounded_int(end_line, start + 199, start, start + MAX_RANGE_LINES - 1)
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
    pattern = re.compile(str(query) if regex else re.escape(str(query)), flags)
    matches = []
    files_scanned = 0
    files_skipped = 0
    iterator, state = _bounded_walk(
        root_path,
        include_hidden=include_hidden,
        include_ignored=include_ignored,
        max_entries=max_entries,
        timeout_seconds=timeout_seconds,
    )
    result_limited = False
    for item in iterator:
        if item["type"] != "file":
            continue
        path = Path(item["path"])
        name = item["name"]
        relative = item["relative"]
        if not (fnmatch.fnmatch(name, glob) or fnmatch.fnmatch(relative, glob)):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES or item["bytes"] > size_limit:
            files_skipped += 1
            continue
        try:
            with path.open("rb") as raw:
                if b"\0" in raw.read(4096):
                    files_skipped += 1
                    continue
            handle = path.open("r", encoding="utf-8", errors="replace")
        except (OSError, PermissionError):
            files_skipped += 1
            continue
        files_scanned += 1
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
    scan = _finish_walk(iterator, state)
    if result_limited:
        scan["truncated"] = True
        scan["truncation_reason"] = "max_results"
    return {
        "root": str(root_path), "query": str(query), "glob": glob,
        "regex": bool(regex), "case_sensitive": bool(case_sensitive),
        "matches": matches, "files_scanned": files_scanned,
        "files_skipped": files_skipped, **scan,
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


def _drain_pipe(pipe, sink, state, limit):
    try:
        while True:
            block = pipe.read(65536)
            if not block:
                break
            state["bytes"] += len(block)
            remaining = max(0, limit - len(sink))
            if remaining:
                sink.extend(block[:remaining])
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


def _terminate_process_tree(proc):
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
        proc.kill()


def run_program(
    program, *, args_json="[]", cwd=".", stdin="", timeout=30,
    max_output=MAX_EXEC_OUTPUT, extra_roots="", bypass=False,
    _allow_cmd_script=False,
):
    executable = _resolve_program(program, extra_roots=extra_roots, bypass=bypass)
    args = _json_args(args_json)
    basename = Path(executable).name.lower()
    lowered_args = [arg.lower() for arg in args]
    if basename in ("powershell", "powershell.exe", "pwsh", "pwsh.exe") and any(
        arg in ("-command", "-c", "-encodedcommand", "-enc") for arg in lowered_args
    ):
        raise PermissionError("inline PowerShell commands are disabled; use script_run")
    if basename in ("cmd", "cmd.exe") and not _allow_cmd_script:
        raise PermissionError("inline cmd execution is disabled; use script_run")
    if Path(executable).suffix.lower() in (".bat", ".cmd") and not _allow_cmd_script:
        raise PermissionError("batch files must be executed with script_run")
    working = _resolve(cwd, extra_roots=extra_roots, bypass=bypass)
    if not working.is_dir():
        raise ValueError("working directory is not a directory: %s" % working)
    timeout = _bounded_int(timeout, 30, 1, MAX_EXEC_TIMEOUT)
    output_limit = _bounded_int(max_output, MAX_EXEC_OUTPUT, 1, MAX_EXEC_OUTPUT)
    input_bytes = str(stdin or "").encode("utf-8")
    if len(input_bytes) > 256_000:
        raise ValueError("stdin exceeds 256000 bytes")
    command = [executable, *args]
    started = time.time()
    timed_out = False
    creationflags = 0
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    proc = subprocess.Popen(
        command,
        cwd=str(working),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        creationflags=creationflags,
        start_new_session=os.name != "nt",
    )
    stdout_bytes = bytearray()
    stderr_bytes = bytearray()
    stdout_state = {"bytes": 0}
    stderr_state = {"bytes": 0}
    readers = [
        threading.Thread(
            target=_drain_pipe,
            args=(proc.stdout, stdout_bytes, stdout_state, output_limit),
            daemon=True,
        ),
        threading.Thread(
            target=_drain_pipe,
            args=(proc.stderr, stderr_bytes, stderr_state, output_limit),
            daemon=True,
        ),
    ]
    writer = threading.Thread(
        target=_write_stdin, args=(proc.stdin, input_bytes), daemon=True,
    )
    for thread in readers:
        thread.start()
    writer.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_tree(proc)
        proc.wait(timeout=10)
    writer.join(timeout=2)
    for thread in readers:
        thread.join(timeout=2)
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


def run_script(
    path, *, args_json="[]", cwd="", stdin="", timeout=30,
    max_output=MAX_EXEC_OUTPUT, extra_roots="", bypass=False,
):
    script = _resolve(path, extra_roots=extra_roots, bypass=bypass)
    if not script.is_file():
        raise FileNotFoundError(str(script))
    suffix = script.suffix.lower()
    args = _json_args(args_json)
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
        executable, prefix = shutil.which("bash"), [str(script)]
    elif suffix in (".exe", ".com"):
        executable, prefix = str(script), []
    else:
        raise ValueError("unsupported script type: %s" % (suffix or "(none)"))
    if not executable:
        raise FileNotFoundError("runner is not installed for %s" % suffix)
    return run_program(
        executable,
        args_json=[*prefix, *args],
        cwd=cwd or str(script.parent),
        stdin=stdin,
        timeout=timeout,
        max_output=max_output,
        extra_roots=extra_roots,
        bypass=bypass,
        _allow_cmd_script=suffix in (".cmd", ".bat"),
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
