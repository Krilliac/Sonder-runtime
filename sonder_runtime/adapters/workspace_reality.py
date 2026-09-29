"""Bounded Git-backed workspace identity and resume delta adapter."""
from __future__ import annotations

import hashlib
import math
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from sonder_runtime.application.ports.workspace_reality import (
    WorkspaceDelta, WorkspaceSnapshot, scope_intersects,
)
from sonder_runtime.platform.runtime_threads import Thread as owned_runtime_thread

MAX_DIRTY = 500
MAX_FILES = 400
MAX_COMMITS = 25
MAX_OUTPUT = 256_000


def _env() -> dict[str, str]:
    return {
        **{k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")},
        # Preserve configured Git content semantics (notably core.autocrlf).
        # Disabling global config makes otherwise-clean Windows trees dirty.
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0", "GIT_EXTERNAL_DIFF": os.devnull,
    }


class _GitFailure(RuntimeError):
    def __init__(self, message: str, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode


class GitWorkspaceReality:
    def __init__(self, *, timeout_seconds: float = 10.0, git: str = "git") -> None:
        value = float(timeout_seconds)
        if not math.isfinite(value) or value <= 0 or value > 10.0:
            raise ValueError("timeout_seconds must be finite and in (0, 10]")
        self.timeout_seconds = max(0.05, value)
        self.git = git

    def _run(self, root: Path, args: Sequence[str], deadline: float) -> bytes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _GitFailure("workspace reality deadline exceeded")
        try:
            process = subprocess.Popen(
                [self.git, "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(root), *args],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=_env(), shell=False, bufsize=0,
            )
        except OSError as exc:
            raise _GitFailure(type(exc).__name__) from exc
        output = bytearray()
        overflow = threading.Event()

        def drain() -> None:
            assert process.stdout is not None
            while True:
                chunk = process.stdout.read(65536)
                if not chunk:
                    return
                if len(output) + len(chunk) > MAX_OUTPUT:
                    overflow.set()
                    try:
                        process.kill()
                    except OSError:
                        pass
                    return
                output.extend(chunk)

        reader = owned_runtime_thread(target=drain, daemon=True)
        reader.start()
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
            reader.join(timeout=1)
            if process.stdout is not None:
                process.stdout.close()
            raise _GitFailure("timeout") from exc
        reader.join(timeout=max(0.0, deadline - time.monotonic()))
        if reader.is_alive():
            process.kill()
            if process.stdout is not None:
                process.stdout.close()
            raise _GitFailure("git output reader did not close")
        if process.stdout is not None:
            process.stdout.close()
        if overflow.is_set():
            raise _GitFailure("git output exceeded bound")
        if process.returncode:
            raise _GitFailure("git failed", process.returncode)
        return bytes(output)

    @staticmethod
    def _root(root: str | os.PathLike[str]) -> Path:
        return Path(root).resolve()

    @staticmethod
    def _has_git_marker(path: Path) -> bool:
        for _ in range(64):
            try:
                (path / ".git").lstat()
            except FileNotFoundError:
                pass
            except OSError:
                # Failure to inspect is not proof that this is a non-Git root.
                return True
            else:
                return True
            if path.parent == path:
                break
            path = path.parent
        return False

    def capture(self, root: str, *, deadline_monotonic: float | None = None) -> WorkspaceSnapshot | None:
        path = self._root(root)
        deadline = time.monotonic() + self.timeout_seconds
        if deadline_monotonic is not None:
            deadline = min(deadline, float(deadline_monotonic))
        return self._capture(path, deadline)

    def _capture(self, path: Path, deadline: float) -> WorkspaceSnapshot | None:
        try:
            marker = self._run(path, ["rev-parse", "--is-inside-work-tree"], deadline)
        except _GitFailure:
            # An existing .git marker distinguishes non-git from an unavailable
            # Git invocation; never turn Git failures into the non-git path.
            if not self._has_git_marker(path):
                return None
            return self._unknown_snapshot(path, "git unavailable")
        if marker.strip() != b"true":
            return None
        try:
            head = self._run(path, ["rev-parse", "HEAD"], deadline).decode().strip()
            try:
                branch_raw = self._run(path, ["symbolic-ref", "--quiet", "--short", "HEAD"], deadline)
                branch = branch_raw.decode("utf-8", "replace").strip() or None
            except _GitFailure as exc:
                if exc.returncode != 1:
                    raise
                branch = None
            git_root = Path(self._run(path, ["rev-parse", "--show-toplevel"], deadline).decode().strip()).resolve()
            status = self._run(path, ["status", "--porcelain=v1", "-z", "--untracked-files=all",
                                      "--ignore-submodules=all"], deadline)
            dirty, complete = self._dirty(git_root, status)
            payload = "".join(f"{x['status']}\0{x['path']}\0{x['size']}\0{x['mtime_ns']}\n" for x in dirty)
            digest = hashlib.sha256(payload.encode("utf-8", "surrogateescape")).hexdigest()
            return {"version": 1, "root": str(path), "repository_root": str(git_root),
                    "head": head or None, "branch": branch,
                    "dirty_hash": digest, "dirty": dirty, "complete": complete}
        except (_GitFailure, OSError, UnicodeError, ValueError) as exc:
            return self._unknown_snapshot(path, str(exc))

    def _unknown_snapshot(self, path: Path, reason: str) -> WorkspaceSnapshot:
        return {"version": 1, "root": str(path), "head": None, "branch": None,
                "dirty_hash": "", "dirty": [], "complete": False, "reason": reason}

    @staticmethod
    def _dirty(root: Path, raw: bytes) -> tuple[list[dict[str, Any]], bool]:
        tokens = raw.split(b"\0")
        out: list[dict[str, Any]] = []
        complete = True
        i = 0
        while i < len(tokens) and tokens[i]:
            token = tokens[i].decode("utf-8", "surrogateescape")
            # Preserve both porcelain columns: staging the same dirty bytes
            # changes index identity even when lstat metadata stays identical.
            status = token[:2] if token[:2].strip() else "??"
            first = token[3:] if len(token) > 3 else ""
            paths = [first]
            if any(flag in status for flag in "RC") and i + 1 < len(tokens):
                i += 1
                paths.append(tokens[i].decode("utf-8", "surrogateescape"))
            for item in paths:
                rel = item.replace("\\", "/")
                try:
                    stat = (root / rel).lstat()
                    size, mtime = int(stat.st_size), int(stat.st_mtime_ns)
                except FileNotFoundError:
                    size, mtime = 0, 0
                except OSError:
                    size, mtime = 0, 0
                    complete = False
                if len(out) < MAX_DIRTY:
                    out.append({"status": status, "path": rel, "size": size, "mtime_ns": mtime})
                else:
                    complete = False
            i += 1
        if len(out) >= MAX_DIRTY and i < len(tokens) and tokens[i]:
            complete = False
        out.sort(key=lambda x: (x["path"], x["status"]))
        return out, complete

    @staticmethod
    def _valid_snapshot(snapshot: Any, root: Path) -> bool:
        if (not isinstance(snapshot, Mapping) or snapshot.get("version") != 1
                or snapshot.get("root") != str(root)):
            return False
        if not isinstance(snapshot.get("complete"), bool) or not isinstance(snapshot.get("dirty"), (list, tuple)):
            return False
        if len(snapshot.get("dirty", ())) > MAX_DIRTY:
            return False
        head = snapshot.get("head")
        if head is not None and (not isinstance(head, str) or len(head) not in (40, 64) or any(c not in "0123456789abcdefABCDEF" for c in head)):
            return False
        if snapshot.get("complete") and not head:
            return False
        if snapshot.get("branch") is not None and not isinstance(snapshot.get("branch"), str):
            return False
        for entry in snapshot.get("dirty", ()):
            if not isinstance(entry, Mapping) or any(
                not isinstance(entry.get(key), str) or not 1 <= len(entry[key]) <= 4096
                for key in ("status", "path")
            ) or any(type(entry.get(key)) is not int for key in ("size", "mtime_ns")):
                return False
        digest = snapshot.get("dirty_hash")
        if not isinstance(digest, str) or (digest and (len(digest) != 64 or any(c not in "0123456789abcdefABCDEF" for c in digest))):
            return False
        return True

    def revalidate(self, root: str, snapshot: Mapping[str, Any], owned_paths: Sequence[str] = (), *, deadline_monotonic: float | None = None) -> WorkspaceDelta | None:
        path = self._root(root)
        deadline = time.monotonic() + self.timeout_seconds
        if deadline_monotonic is not None:
            deadline = min(deadline, float(deadline_monotonic))
        current = self._capture(path, deadline)
        if current is None and (not isinstance(snapshot, Mapping) or not snapshot):
            return None
        if not self._valid_snapshot(snapshot, path):
            return {"status": "unavailable", "previous_head": snapshot.get("head") if isinstance(snapshot, Mapping) else None,
                    "current_head": None, "history_rewritten": None, "ahead": None, "behind": None,
                    "commits": [], "files": [], "truncated": False, "requires_reinspection": True,
                    "requires_replan": True, "reason": "invalid snapshot"}
        if current is None:
            if snapshot.get("head") or snapshot.get("dirty_hash"):
                return {"status": "unavailable", "previous_head": snapshot.get("head"), "current_head": None,
                        "history_rewritten": None, "ahead": None, "behind": None, "commits": [], "files": [],
                        "truncated": False, "requires_reinspection": True, "requires_replan": True,
                        "reason": "Git unavailable", "snapshot": {}}
            return None
        previous_root = snapshot.get("root")
        if previous_root != str(path) or not snapshot.get("complete", False):
            return {"status": "unavailable", "previous_head": snapshot.get("head"),
                    "current_head": current.get("head"), "history_rewritten": None,
                    "ahead": None, "behind": None, "commits": [], "files": [],
                    "truncated": False, "requires_reinspection": True,
                    "requires_replan": True, "reason": "invalid or incomplete snapshot",
                    "snapshot": current}
        if not current.get("complete"):
            return {"status": "unavailable", "previous_head": snapshot.get("head"), "current_head": current.get("head"),
                    "history_rewritten": None, "ahead": None, "behind": None, "commits": [], "files": [],
                    "truncated": True, "requires_reinspection": True, "requires_replan": True,
                    "reason": "current snapshot incomplete", "snapshot": current}
        if (snapshot.get("head") == current.get("head") and snapshot.get("branch") == current.get("branch")
                and snapshot.get("dirty_hash") == current.get("dirty_hash")):
            return None
        prev, now = snapshot.get("head"), current.get("head")
        rewritten: bool | None = False
        ahead = behind = None
        commits: list[dict[str, str]] = []
        files: list[dict[str, str]] = []
        truncated = False
        try:
            if not prev or not now:
                rewritten = None
            else:
                anc = self._run(path, ["merge-base", "--is-ancestor", str(prev), str(now)], deadline)
                _ = anc
                rewritten = False
        except _GitFailure as exc:
            if exc.returncode == 1:
                rewritten = True
            elif exc.returncode == 128:
                try:
                    self._run(path, ["cat-file", "-t", str(prev)], deadline)
                except _GitFailure as missing:
                    rewritten = True if missing.returncode == 128 else None
                else:
                    rewritten = None
                return {"status": "unavailable", "previous_head": prev, "current_head": now,
                        "history_rewritten": rewritten, "ahead": None, "behind": None,
                        "commits": [], "files": [], "truncated": False,
                        "requires_reinspection": True, "requires_replan": True,
                        "reason": "previous history unavailable", "snapshot": current}
            elif prev:
                return {"status": "unavailable", "previous_head": prev, "current_head": now,
                        "history_rewritten": None, "ahead": None, "behind": None, "commits": [], "files": [],
                        "truncated": False, "requires_reinspection": True, "requires_replan": True,
                        "reason": "Git history check unavailable", "snapshot": current}
            else:
                rewritten = None
        try:
            if prev and now:
                counts = self._run(path, ["rev-list", "--left-right", "--count", f"{prev}...{now}"], deadline).split()
                if len(counts) == 2:
                    behind, ahead = int(counts[0]), int(counts[1])
                log = self._run(path, ["log", "--format=%H%x00%s", "-n", str(MAX_COMMITS), f"{prev}..{now}"], deadline)
                for row in log.splitlines()[:MAX_COMMITS]:
                    ident, _, subject = row.partition(b"\0")
                    commits.append({"id": ident.decode("ascii", "replace"), "subject": subject.decode("utf-8", "replace")[:512]})
                diff = self._run(path, ["diff", "--no-ext-diff", "--no-textconv", "--no-relative",
                                        "--name-status", "-z", f"{prev}..{now}"], deadline)
                files.extend(self._parse_diff(diff))
            old = {str(x.get("path")): x for x in snapshot.get("dirty", []) if isinstance(x, Mapping)}
            new = {str(x.get("path")): x for x in current.get("dirty", []) if isinstance(x, Mapping)}
            for name in sorted(set(old) | set(new)):
                if old.get(name) != new.get(name):
                    files.append({"status": str(new.get(name, {}).get("status", "clean")), "path": name, "category": self._category(name)})
        except (ValueError, _GitFailure) as exc:
            return {"status": "unavailable", "previous_head": prev, "current_head": now,
                    "history_rewritten": rewritten, "ahead": ahead, "behind": behind,
                    "commits": commits, "files": [], "truncated": False,
                    "requires_reinspection": True, "requires_replan": True, "reason": str(exc), "snapshot": current}
        unique: dict[str, dict[str, str]] = {}
        for item in files:
            unique[item["path"]] = item
        ordered = list(unique.values())
        if len(ordered) > MAX_FILES:
            truncated = True
            ordered = ordered[:MAX_FILES]
        scope = tuple(str(Path(item) if Path(item).is_absolute() else path / item) for item in owned_paths)
        requires_replan = truncated or bool(rewritten) or scope_intersects(
            scope, ordered, current.get("repository_root", str(path)),
        )
        return {"status": "changed", "previous_head": prev, "current_head": now,
                "history_rewritten": rewritten, "ahead": ahead, "behind": behind,
                "commits": commits[:MAX_COMMITS], "files": ordered,
                "truncated": truncated, "requires_reinspection": True,
                "requires_replan": requires_replan, "snapshot": current}

    @staticmethod
    def _parse_diff(raw: bytes) -> list[dict[str, str]]:
        tokens = raw.split(b"\0")
        out: list[dict[str, str]] = []
        i = 0
        while i < len(tokens) and tokens[i]:
            token = tokens[i].decode("utf-8", "surrogateescape")
            bits = token.split("\t", 1)
            status = bits[0][:1] or "?"
            name = bits[1] if len(bits) == 2 else ""
            if not name and len(tokens) > i + 1:
                # ``--name-status -z`` separates status and pathname with NUL.
                i += 1
                name = tokens[i].decode("utf-8", "surrogateescape")
            if status in {"R", "C"} and i + 1 < len(tokens):
                i += 1
                old_name = name
                name = tokens[i].decode("utf-8", "surrogateescape")
                out.append({"status": status, "path": old_name, "category": GitWorkspaceReality._category(old_name)})
            out.append({"status": status, "path": name, "category": GitWorkspaceReality._category(name)})
            i += 1
        return out

    @staticmethod
    def _category(path: str) -> str:
        lower = path.lower().replace("\\", "/")
        basename = lower.rsplit("/", 1)[-1]
        if "/migrations/" in f"/{lower}/" or lower.endswith(("migration.sql", "migrations.py")):
            return "migration"
        if basename in {"requirements.txt", "pyproject.toml", "package.json", "package-lock.json",
                        "cargo.toml", "cargo.lock", "go.mod", "go.sum", "cmakelists.txt",
                        "pom.xml", "gemfile", "gemfile.lock", "setup.py", "setup.cfg"}:
            return "manifest"
        if lower.endswith((".yaml", ".yml", ".toml", ".ini", ".json")) or lower.startswith(("config/", ".github/")):
            return "config"
        if lower.endswith((".md", ".rst", ".txt")) or lower.startswith("docs/"):
            return "doc"
        if lower.endswith((".py", ".js", ".ts", ".cpp", ".h", ".c", ".rs", ".go", ".java")):
            return "source"
        return "source"


__all__ = ["GitWorkspaceReality"]
