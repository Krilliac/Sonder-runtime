"""Read the report files a test run wrote, under hard bounds.

Report files are written by project code, so nothing here trusts them: the
run's report directory must resolve under the state ``test-runs`` root, files
are opened without following symlinks and must be regular files of bounded
size, gradle/maven report trees must stay inside the run directory and only
files modified since the run started count. Parsing is the domain's.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import stat
import tempfile
from pathlib import Path
from typing import Mapping

from ...domain.common.errors import InvalidInput
from ...domain.testing.report_parsers import (
    ParsedResults,
    merge_parsed,
    parse_jest_json,
    parse_junit_xml,
    parse_trx,
)
from ...domain.testing.runners import ReportFormat

logger = logging.getLogger(__name__)

MAX_REPORT_FILE_BYTES = 8 * 1024 * 1024
MAX_REPORT_TOTAL_BYTES = 16 * 1024 * 1024
MAX_REPORT_FILES = 512
MAX_CACHE_BYTES = 1024 * 1024
CACHE_NAME = "report.json"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_BINARY = getattr(os, "O_BINARY", 0)


def _is_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attrs = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def read_bounded(path: Path, limit: int, *, not_before: float | None = None) -> bytes | None:
    """Regular-file bytes via a no-follow handle; None when absent/refused."""
    if _is_reparse(path):
        return None
    try:
        descriptor = os.open(str(path), os.O_RDONLY | _NOFOLLOW | _BINARY)
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        if not_before is not None and info.st_mtime < not_before:
            return None
        data = handle.read(limit + 1)
    return data if len(data) <= limit else None


class ReportArtifactCollector:
    """``TestReportCollector`` over the state test-runs directory."""

    def __init__(self, report_root: str) -> None:
        self._root = Path(report_root)

    def _run_dir(self, meta: Mapping[str, str]) -> Path:
        raw = str(meta.get("report_dir") or "")
        if not raw:
            raise PermissionError("test run has no report directory")
        run_dir = Path(raw)
        root = Path(os.path.realpath(self._root))
        if run_dir.parent != self._root or _is_reparse(run_dir) or _is_reparse(self._root):
            raise PermissionError("test report directory is outside the report root")
        if not _inside(Path(os.path.realpath(run_dir)), root):
            raise PermissionError("test report directory resolves outside the report root")
        return run_dir

    def collect(self, plan_meta: Mapping[str, str]) -> tuple[ParsedResults | None, bool, str]:
        try:
            fmt = ReportFormat(str(plan_meta.get("report_format", "")))
        except ValueError:
            return None, False, "unknown report format"
        try:
            if plan_meta.get("report_glob") and not plan_meta.get("report_file"):
                return self._collect_tree(plan_meta)
            run_dir = self._run_dir(plan_meta)
            report_file = Path(str(plan_meta.get("report_file") or ""))
            if report_file.parent != run_dir:
                return None, False, "report file is outside the run directory"
            data = read_bounded(report_file, MAX_REPORT_FILE_BYTES)
        except PermissionError as exc:
            return None, False, "report refused: %s" % exc
        if data is None:
            return None, False, "the runner wrote no readable report file"
        try:
            if fmt is ReportFormat.JUNIT_XML:
                parsed = parse_junit_xml(data)
            elif fmt is ReportFormat.TRX:
                parsed = parse_trx(data)
            elif fmt is ReportFormat.JEST_JSON:
                parsed = parse_jest_json(data, strip_prefix=str(plan_meta.get("cwd") or ""))
            else:
                return None, False, ""
        except InvalidInput as exc:
            return None, False, "report rejected: %s" % exc
        return parsed, parsed.truncated, ""

    def _collect_tree(self, meta: Mapping[str, str]) -> tuple[ParsedResults | None, bool, str]:
        cwd = Path(str(meta.get("cwd") or ""))
        pattern = str(meta.get("report_glob") or "")
        if not cwd.is_absolute() or "/" not in pattern:
            return None, False, "report tree is not configured"
        directory_rel, name_pattern = pattern.rsplit("/", 1)
        parts = [part for part in directory_rel.split("/") if part]
        if any(part in {"", ".", ".."} for part in parts):
            return None, False, "report tree pattern is invalid"
        directory = cwd.joinpath(*parts)
        for index in range(len(parts)):
            if _is_reparse(cwd.joinpath(*parts[: index + 1])):
                return None, False, "report tree traverses a symlink"
        if not directory.is_dir() or not _inside(Path(os.path.realpath(directory)),
                                                  Path(os.path.realpath(cwd))):
            return None, False, "the runner wrote no report tree"
        try:
            started = float(meta.get("started_at") or 0) - 1.0
        except ValueError:
            started = 0.0
        results: list[ParsedResults] = []
        total = 0
        truncated = False
        rejected = 0
        with os.scandir(directory) as iterator:
            names = sorted(entry.name for index, entry in enumerate(iterator)
                           if index < MAX_REPORT_FILES * 4)
        matching = [name for name in names if fnmatch.fnmatchcase(name, name_pattern)]
        if len(matching) > MAX_REPORT_FILES:
            matching, truncated = matching[:MAX_REPORT_FILES], True
        for name in matching:
            data = read_bounded(directory / name, MAX_REPORT_FILE_BYTES, not_before=started)
            if data is None:
                continue
            if total + len(data) > MAX_REPORT_TOTAL_BYTES:
                truncated = True
                break
            total += len(data)
            try:
                results.append(parse_junit_xml(data))
            except InvalidInput:
                rejected += 1
        if not results:
            return None, truncated, "the runner wrote no report files for this run"
        merged = merge_parsed(results)
        note = "%d report file(s) rejected" % rejected if rejected else ""
        return merged, truncated or merged.truncated, note

    # -- report cache ------------------------------------------------------------

    def load_cached(self, plan_meta: Mapping[str, str]) -> Mapping | None:
        try:
            run_dir = self._run_dir(plan_meta)
        except PermissionError:
            return None
        data = read_bounded(run_dir / CACHE_NAME, MAX_CACHE_BYTES)
        if data is None:
            return None
        try:
            body = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        if not isinstance(body, dict) or body.get("job_id") != plan_meta.get("job_id"):
            return None
        return body

    def store_cached(self, plan_meta: Mapping[str, str], wire: Mapping) -> None:
        try:
            run_dir = self._run_dir(plan_meta)
        except PermissionError as exc:
            raise OSError(str(exc)) from None
        payload = json.dumps(dict(wire), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(payload) > MAX_CACHE_BYTES:
            raise OSError("report cache exceeds its bound")
        descriptor, temporary = tempfile.mkstemp(prefix=".report-", dir=str(run_dir))
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            os.replace(temporary, run_dir / CACHE_NAME)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


__all__ = [
    "CACHE_NAME", "MAX_REPORT_FILES", "MAX_REPORT_FILE_BYTES", "MAX_REPORT_TOTAL_BYTES",
    "ReportArtifactCollector", "read_bounded",
]
