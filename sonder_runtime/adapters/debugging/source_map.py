"""Map build-machine source paths in debug info to the local checkout.

PDBs and DWARF record the path a file had on the machine that built it, e.g.
``C:\\agent\\_work\\3\\s\\Engine\\Render\\render.cpp``. ``ProjectSourceMap``
keeps a bounded suffix index of the files under the project roots (at most
200,000 files, two seconds to build, symlinks/junctions and VCS/virtualenv
directories skipped) and maps a recorded path to the project file sharing
the longest unique path suffix -- ``Engine/Render/render.cpp`` here. Ties
are not guessed: an ambiguous suffix is left unmapped and noted. Only files
inside the roots are ever named, so a recorded path cannot point the brief at
anything outside the project.
"""
from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable

from ..filesystem import file_ops

MAX_FILES = 200_000
MAX_SECONDS = 2.0
INDEX_TTL_SECONDS = 30.0
MAX_MAPPED_NOTES = 4
_SKIP_DIRS = frozenset({".git", ".hg", ".svn", "node_modules", "venv", ".venv", "__pycache__",
                        ".pytest_cache", ".mypy_cache", ".tox"})
_SPLIT = re.compile(r"[\\/]+")


def _is_windows_path(text: str) -> bool:
    return "\\" in text or bool(re.match(r"^[A-Za-z]:", text))


def _parts(text: str) -> tuple[str, ...]:
    return tuple(part for part in _SPLIT.split(str(text or "")) if part and part not in (".",))


class _Index:
    def __init__(self) -> None:
        self.by_name: dict[str, list[tuple[tuple[str, ...], str]]] = {}
        self.files = 0
        self.truncated = False


class ProjectSourceMap:
    """``SourceMap`` port: longest-unique-suffix mapping into the project roots."""

    def __init__(self, roots: Callable[[], Iterable[str | Path]] | Iterable[str | Path], *,
                 max_files: int = MAX_FILES, max_seconds: float = MAX_SECONDS,
                 clock: Callable[[], float] = time.monotonic,
                 ttl_seconds: float = INDEX_TTL_SECONDS) -> None:
        self._roots = roots if callable(roots) else (lambda values=tuple(roots): values)
        self._max_files = int(max_files)
        self._max_seconds = float(max_seconds)
        self._clock = clock
        self._ttl = float(ttl_seconds)
        self._lock = threading.Lock()
        self._cached: tuple[tuple[str, ...], float, _Index] | None = None

    def _root_list(self) -> tuple[str, ...]:
        out = []
        for root in self._roots() or ():
            try:
                resolved = Path(root).resolve(strict=True)
            except (OSError, ValueError):
                continue
            if resolved.is_dir() and str(resolved) not in out:
                out.append(str(resolved))
        return tuple(out)

    def index(self) -> _Index:
        roots = self._root_list()
        now = self._clock()
        with self._lock:
            if self._cached is not None and self._cached[0] == roots and now - self._cached[1] < self._ttl:
                return self._cached[2]
        built = self._build(roots)
        with self._lock:
            self._cached = (roots, now, built)
        return built

    def _build(self, roots: tuple[str, ...]) -> _Index:
        index = _Index()
        deadline = self._clock() + self._max_seconds
        for root in roots:
            base = Path(root)
            for directory, dirs, files in os.walk(base):
                current = Path(directory)
                dirs[:] = sorted(
                    name for name in dirs
                    if name not in _SKIP_DIRS and not file_ops._is_reparse_point(current / name))
                for name in sorted(files):
                    if index.files >= self._max_files or self._clock() > deadline:
                        index.truncated = True
                        return index
                    full = current / name
                    try:
                        if file_ops._is_reparse_point(full):
                            continue
                    except PermissionError:
                        continue
                    relative = full.relative_to(base).as_posix()
                    parts = tuple(part.casefold() for part in _parts(relative))
                    index.by_name.setdefault(name.casefold(), []).append((parts, relative))
                    index.files += 1
        return index

    def lookup(self, recorded: str) -> tuple[str | None, str]:
        """``(project-relative path, note)``; the note explains a refusal or tie."""
        parts = _parts(recorded)
        if not parts:
            return None, ""
        index = self.index()
        wanted = tuple(part.casefold() for part in parts)
        candidates = index.by_name.get(wanted[-1], [])
        if not candidates:
            return None, ""
        case_sensitive = not _is_windows_path(recorded)
        best = 0
        chosen: list[str] = []
        for candidate_parts, relative in candidates:
            if case_sensitive and _parts(relative)[-1] != parts[-1]:
                continue
            length = 0
            for mine, theirs in zip(reversed(wanted), reversed(candidate_parts)):
                if mine != theirs:
                    break
                length += 1
            if length > best:
                best, chosen = length, [relative]
            elif length == best and length:
                chosen.append(relative)
        if not chosen:
            return None, ""
        if len(chosen) > 1:
            return None, "source path %s matches %d project files equally; not mapped" % (
                "/".join(parts[-3:]), len(chosen))
        return chosen[0], ""

    def map_report(self, report):
        """The report with ``local_file`` set on frames whose file maps uniquely."""
        notes: list[str] = []
        memo: dict[str, str | None] = {}

        def map_frame(frame):
            recorded = getattr(frame, "file", "") or ""
            if not recorded or getattr(frame, "local_file", None):
                return frame
            if recorded not in memo:
                local, note = self.lookup(recorded)
                memo[recorded] = local
                if note and len(notes) < MAX_MAPPED_NOTES:
                    notes.append(note)
            local = memo[recorded]
            return replace(frame, local_file=local) if local else frame

        threads = tuple(
            replace(thread, frames=tuple(map_frame(frame) for frame in thread.frames))
            for thread in report.threads)
        index = self.index()
        if index.truncated:
            notes.append("source index truncated at %d files; some paths were not mapped"
                         % index.files)
        merged = tuple(report.notes) + tuple(note for note in notes if note not in report.notes)
        return replace(report, threads=threads, notes=merged)


__all__ = ["MAX_FILES", "ProjectSourceMap"]
