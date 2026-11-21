"""The only writer of the eval: ``items.jsonl``, ``report.json`` and ``summary.md`` under one directory.

The directory is named by the caller. It must be absolute, must not be a UNC, network or device
spelling (refused lexically, before any filesystem call could open an SMB connection), must not be
(or sit below) a symlink or junction, must not lie inside a repo root (compared both as written and
with links and 8.3 short names resolved), and none of the three files may be the dataset. Nothing
else is ever created: the atomic writes use a temp file that is removed in all cases.
"""
from __future__ import annotations

import json
import os
import stat
import uuid
from typing import Any, Sequence

import sonder_runtime.adapters.filesystem.file_ops as file_ops

from ...domain.security_eval.errors import OutputDirRefused
from ..filesystem.atomic_json import write_json_atomic

ITEMS_FILE = "items.jsonl"
REPORT_FILE = "report.json"
SUMMARY_FILE = "summary.md"
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _inside(child: str, parent: str) -> bool:
    low, high = _norm(child), _norm(parent)
    try:
        return os.path.commonpath([low, high]) == high
    except ValueError:
        return False


def _real(path: str) -> str | None:
    """``path`` with links and 8.3 short names resolved, or ``None`` for a network spelling.

    Never resolves a UNC/device spelling: that would open an SMB connection."""
    try:
        local = file_ops._reject_network_path(path)
        return os.path.normcase(os.path.realpath(os.path.abspath(local)))
    except (OSError, ValueError):  # PermissionError is an OSError
        return None


def _inside_resolved(real_child: str | None, parent: str) -> bool:
    real_parent = _real(parent)
    if real_child is None or real_parent is None:
        return False
    try:
        return os.path.commonpath([real_child, real_parent]) == real_parent
    except ValueError:
        return False


def _is_link(path: str) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE)


class OutputStore:
    """Append-only item rows plus atomically replaced report files."""

    def __init__(self, out_dir: str, *, protected_roots: Sequence[str] = (),
                 dataset_path: str | None = None) -> None:
        if not isinstance(out_dir, str) or not out_dir.strip() or "\x00" in out_dir:
            raise OutputDirRefused("out_dir is required")
        try:
            out_dir = file_ops._reject_network_path(out_dir)
        except PermissionError:
            raise OutputDirRefused("out_dir may not be a UNC, network or device path") from None
        if not os.path.isabs(out_dir):
            raise OutputDirRefused("out_dir must be an absolute path")
        target = os.path.abspath(out_dir)
        real_target = _real(target)
        for root in protected_roots:
            if root and (_inside(target, root) or _inside_resolved(real_target, root)):
                raise OutputDirRefused("out_dir may not lie inside a repo root")
        if os.path.lexists(target) and not os.path.isdir(target):
            raise OutputDirRefused("out_dir exists and is not a directory")
        self._check_components(target)
        self._dir = target
        self._paths = {name: os.path.join(target, name) for name in (ITEMS_FILE, REPORT_FILE, SUMMARY_FILE)}
        if dataset_path:
            dataset = _norm(dataset_path)
            if dataset == _norm(target) or dataset in {_norm(path) for path in self._paths.values()}:
                raise OutputDirRefused("out_dir may not be, or write over, the dataset file")
        os.makedirs(target, exist_ok=True)
        self._check_components(target)
        for path in self._paths.values():
            if os.path.lexists(path) and (_is_link(path) or not os.path.isfile(path)):
                raise OutputDirRefused("%s exists and is not a plain file" % os.path.basename(path))
        self.corrupt_lines = 0

    @staticmethod
    def _check_components(target: str) -> None:
        current = os.path.splitdrive(target)[0] + os.sep if os.path.splitdrive(target)[0] else os.sep
        parts = [part for part in target[len(current):].split(os.sep) if part]
        for part in parts:
            current = os.path.join(current, part)
            if os.path.lexists(current) and _is_link(current):
                raise OutputDirRefused("out_dir passes through a symlink or junction")

    @property
    def out_dir(self) -> str:
        return self._dir

    def read_rows(self) -> list[dict[str, Any]]:
        path = self._paths[ITEMS_FILE]
        rows: list[dict[str, Any]] = []
        self.corrupt_lines = 0
        if not os.path.isfile(path):
            return rows
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    self.corrupt_lines += 1  # a torn last line after a crash; the item is simply redone
                    continue
                if isinstance(row, dict):
                    rows.append(row)
                else:
                    self.corrupt_lines += 1
        return rows

    def append(self, row: dict[str, Any]) -> None:
        line = json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n"
        with open(self._paths[ITEMS_FILE], "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def write_report(self, report: dict[str, Any], markdown: str) -> None:
        write_json_atomic(self._paths[REPORT_FILE], report)
        summary = self._paths[SUMMARY_FILE]
        temporary = "%s.tmp-%s" % (summary, uuid.uuid4().hex)
        try:
            with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(markdown)
            os.replace(temporary, summary)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
