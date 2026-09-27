"""Turn a finished build job's private log into a ``BuildJobReport``.

F8: an engine-scale log is far larger than any window a model sees, and its
first error is often in the middle -- a head-plus-tail read misses it. The
collector therefore streams the whole private ``output.log`` (and an MSBuild
``-flp`` log), up to 512 MiB, and keeps only lines that matter:

* diagnostics in every shape ``domain.diagnostics`` parses (GNU/Clang,
  MSVC/clang-cl with or without a code, LNK/MSB, linker ``undefined
  reference``), ``In file included from`` context and CMake ``Error``/``Warning``
  blocks;
* step boundaries: ninja ``FAILED:`` lines and the latest ``[n/m]`` progress
  line before a kept line (the thousands of progress lines between are
  dropped), make ``***`` lines, cl source echo lines and MSBuild project lines;
* for include traces, ``-H`` and ``/showIncludes`` lines;
* the last lines of the log, for the final status.

At most 50k kept lines of at most 4096 characters. The kept text is the one
string passed both to ``parse_diagnostics`` and to the attribution layer, so
``Diagnostic.raw_line_no`` indexes the same lines everywhere.

The binlog of an MSBuild run stays in the run's private directory; it is not
parsed in v1.
"""
from __future__ import annotations

import codecs
import json
import os
import re
import stat
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from ...application.build.ports import ACTION_INCLUDE_TRACE, BUILD_JOB_KIND
from ...application.ports.jobs import JobRecord, JobStatus

MAX_SCAN_BYTES = 512 * 1024 * 1024
MAX_KEPT_LINES = 50_000
MAX_LINE_CHARS = 4096
# Warnings and notes share a smaller budget so a flood of them can never
# crowd out the errors that follow.
MAX_SOFT_LINES = 10_000
TAIL_LINES = 40
CMAKE_BLOCK_LINES = 12
REPORT_CACHE_NAME = "report.json"
MAX_CACHE_BYTES = 256 * 1024
_CHUNK = 1 << 20

# Cheap substring gate before any regex runs on a line.
_GATE = ("rror", "arning", "note:", "FAILED", "***", "undefined reference", "LNK", "MSB",
         "In file included", "CMake ", "ld:", "collect2", "fatal", "cannot find", "multiple definition",
         ".vcxproj", "Hinweis", "Note:", "Fehler", "Warnung", "): ", "ninja:", "make")
_DIAG_RE = re.compile(
    r"(?:"
    r":\d+(?::\d+)?:\s*(?:fatal error|error|warning|note)\b"      # gnu/clang
    r"|\(\d+(?:,\d+)?\)\s*:\s*(?:fatal error|error|warning|note)\b"  # msvc/clang-cl
    r"|\b(?:error|warning|fatal error)\s+(?:C|LNK|MSB|D|RC|CS|MIDL)\d{3,5}\b"
    r"|\bundefined reference to\b|\bmultiple definition of\b|\bcollect2(?:\.exe)?:"
    r"|^\s*(?:/[^:\s]*/)?ld(?:\.lld|\.gold|\.bfd)?(?:\.exe)?:"
    r"|^In file included from |^\s+from .+:\d+[,:]"
    r"|^FAILED: |^ninja: (?:error|build stopped)|^g?make(?:\[\d+\])?: \*\*\*"
    r"|^CMake (?:Error|Warning)|^-- Configuring incomplete"
    r"|\berror:|\bError\s+\d+\b|^\S.*: (?:fatal )?error\b"
    r")"
)
_SOFT_RE = re.compile(r"(?:warning|note|Warnung|Hinweis)\b")
_HARD_RE = re.compile(r"(?i:error|fehler|fatal)|FAILED|\*\*\*|undefined reference|multiple definition")
_STEP_RE = re.compile(r"^\[\d+/\d+\] ")
_ECHO_RE = re.compile(r"^\s{0,8}(?:\d+>)?\s*[\w .()+-]{1,200}\.(?:c|cc|cpp|cxx|c\+\+|ixx)\s*$", re.IGNORECASE)
_MSBUILD_NODE_RE = re.compile(r"^\s{0,8}(?:\d+>)?\s*(?:Project \".+\" on node|Building |ClCompile:|Link:)")
_TRACE_RE = re.compile(r"^(?:\.+ |Note: including file:|Hinweis: Einlesen der Datei:|Remarque|Nota:)")


@dataclass(frozen=True, slots=True)
class ScannedLog:
    text: str
    kept_lines: int
    lines_scanned: int
    bytes_scanned: int
    source_bytes: int
    truncated: bool
    missing: bool = False


def _keep_mode(line: str, trace: bool) -> str:
    """'' drop, 'step' boundary candidate, 'keep' keep, 'cmake' opens a CMake block."""
    if trace and _TRACE_RE.match(line):
        return "keep"
    if _STEP_RE.match(line):
        return "step"
    if not any(token in line for token in _GATE) and not _ECHO_RE.match(line):
        return ""
    if line.startswith("CMake Error") or line.startswith("CMake Warning"):
        return "cmake"
    if _DIAG_RE.search(line) or _ECHO_RE.match(line) or _MSBUILD_NODE_RE.match(line):
        return "keep"
    return ""


def scan_log(path: str, *, trace: bool = False, max_bytes: int = MAX_SCAN_BYTES,
             max_lines: int = MAX_KEPT_LINES, max_line_chars: int = MAX_LINE_CHARS,
             tail_lines: int = TAIL_LINES) -> ScannedLog:
    """Stream one log file through the prefilter (no-follow, bounded)."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return ScannedLog("", 0, 0, 0, 0, False, missing=True)
    kept: list[str] = []
    tail: deque[tuple[int, str]] = deque(maxlen=max(1, tail_lines))
    pending_step = ""
    cmake_left = 0
    soft_kept = 0
    last_kept_index = -1
    index = -1
    scanned = 0
    truncated = False
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    carry = ""
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return ScannedLog("", 0, 0, 0, 0, False, missing=True)
        source_bytes = int(info.st_size)

        def consume(raw: str) -> None:
            nonlocal pending_step, cmake_left, last_kept_index, index, truncated, soft_kept
            index += 1
            line = raw.rstrip("\r")
            if len(line) > max_line_chars:
                line = line[:max_line_chars]
            tail.append((index, line))
            if len(kept) >= max_lines:
                truncated = True
                return
            if cmake_left > 0:
                if line.startswith(" ") or not line.strip():
                    kept.append(line)
                    last_kept_index = index
                    cmake_left -= 1
                    return
                cmake_left = 0
            mode = _keep_mode(line, trace)
            if mode == "step":
                pending_step = line
                return
            if not mode:
                return
            if mode == "keep" and not trace and _SOFT_RE.search(line) and not _HARD_RE.search(line):
                if soft_kept >= MAX_SOFT_LINES:
                    truncated = True
                    return
                soft_kept += 1
            if pending_step:
                kept.append(pending_step)
                pending_step = ""
            kept.append(line)
            last_kept_index = index
            if mode == "cmake":
                cmake_left = CMAKE_BLOCK_LINES

        while scanned < max_bytes:
            chunk = os.read(fd, min(_CHUNK, max_bytes - scanned))
            if not chunk:
                break
            scanned += len(chunk)
            text = carry + decoder.decode(chunk)
            lines = text.split("\n")
            carry = lines.pop()
            if len(carry) > max_line_chars * 4:
                carry = carry[: max_line_chars]
            for raw in lines:
                consume(raw)
        if scanned >= max_bytes and scanned < source_bytes:
            truncated = True
        carry += decoder.decode(b"", final=True)
        if carry:
            consume(carry)
    finally:
        os.close(fd)
    extra = [line for position, line in tail if position > last_kept_index]
    if pending_step and pending_step not in extra:
        extra.insert(0, pending_step)
    lines_out = kept + extra
    return ScannedLog("\n".join(lines_out), len(lines_out), index + 1, scanned, source_bytes, truncated)


def merge_scans(scans: list[ScannedLog]) -> ScannedLog:
    present = [scan for scan in scans if not scan.missing]
    if not present:
        return ScannedLog("", 0, 0, 0, 0, False, missing=True)
    return ScannedLog(
        "\n".join(scan.text for scan in present if scan.text),
        sum(scan.kept_lines for scan in present),
        sum(scan.lines_scanned for scan in present),
        sum(scan.bytes_scanned for scan in present),
        sum(scan.source_bytes for scan in present),
        any(scan.truncated for scan in present),
    )


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _epoch(text: str) -> float | None:
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(text)).timestamp()
    except (TypeError, ValueError):
        return None


def _default_summarize(text: str, label: str, *, bytes_scanned: int, truncated: bool) -> dict:
    from ...domain.diagnostics.digest import digest_text

    return digest_text(text, source_kind="job", source_label=label, bytes_scanned=bytes_scanned,
                       scan_truncated=truncated).to_wire()


class BuildOutputCollector:
    """``BuildOutputCollector`` over the private run logs."""

    def __init__(self, run_root: str, *, redact: Callable[[str], str] = lambda text: text,
                 clock: Callable[[], float] | None = None, output_reader: Any = None,
                 summarize: Callable[..., Mapping[str, Any]] | None = None,
                 max_scan_bytes: int = MAX_SCAN_BYTES) -> None:
        import time

        self._run_root = Path(run_root)
        self._redact = redact
        self._clock = clock or time.time
        self._output_reader = output_reader
        self._summarize = summarize or _default_summarize
        self._max_scan_bytes = int(max_scan_bytes)
        self._cache: dict[str, Any] = {}

    # -- paths -----------------------------------------------------------------

    def _private(self, job_id: str, path: str) -> str | None:
        """``path`` only when it is a file directly in this job's private run dir."""
        if not path:
            return None
        candidate = Path(path)
        if candidate.parent != self._run_root / job_id:
            return None
        return str(candidate)

    # -- collect ---------------------------------------------------------------

    def scan(self, job_id: str, meta: Mapping[str, str]) -> ScannedLog:
        trace = meta.get("action") == ACTION_INCLUDE_TRACE
        try:
            extra = [item for item in json.loads(meta.get("extra_logs_json") or "[]")
                     if isinstance(item, str)]
        except ValueError:
            extra = []
        paths = [self._private(job_id, item) for item in extra]
        paths = [item for item in paths if item and os.path.exists(item)]
        if not paths:  # MSBuild's -flp log supersedes its console; otherwise the tee log
            paths = [self._private(job_id, str(meta.get("log_file", "")))]
        scans = [scan_log(path, trace=trace, max_bytes=self._max_scan_bytes) for path in paths if path]
        merged = merge_scans(scans)
        if merged.missing and self._output_reader is not None:
            try:
                window = self._output_reader.read_output(job_id, max_bytes=2_000_000, head_bytes=65_536)
            except (KeyError, OSError, ValueError):
                return merged
            text = str(getattr(window, "text", "") or "")
            return ScannedLog(text, text.count("\n") + 1, text.count("\n") + 1,
                              len(text.encode("utf-8", "replace")),
                              int(getattr(window, "source_bytes", 0) or 0),
                              bool(getattr(window, "truncated", False)))
        return merged

    def collect(self, job_id: str, plan_meta: Mapping[str, str], model: Any, *,
                record: JobRecord | None = None, exit_code: int | None = None) -> Any:
        from ...domain.build import attribution as attr
        from ...domain.build import output as out
        from ...domain.build import report as rep

        terminal = record is not None and record.is_terminal
        if terminal and job_id in self._cache:
            return self._cache[job_id]
        meta = dict(plan_meta)
        if meta.get("kind", BUILD_JOB_KIND) != BUILD_JOB_KIND:
            raise ValueError("not a build job")
        source_root = str(meta.get("project_root", ""))
        build_dir = str(meta.get("build_dir", ""))
        action = str(meta.get("action", ""))
        scanned = self.scan(job_id, meta)
        text = scanned.text
        dset = out.parse_build_diagnostics(text, configure=action == "configure")
        segments = tuple(out.parse_ninja_segments(text)) + tuple(out.parse_make_failures(text))
        atts = attr.attribute_steps(text, dset, segments, model=model)
        atts = tuple(self._redact_attribution(
            rep.relabel_attribution(item, source_root=source_root, build_dir=build_dir))
            for item in atts)
        firsts = attr.first_errors(atts)
        notes = self._notes(meta)
        if scanned.missing:
            notes.append("the private build log is missing; output came from the job registry"
                         if text else "the private build log is missing")
        if scanned.truncated:
            notes.append("build log scan was bounded (%d bytes, %d kept lines)"
                         % (scanned.bytes_scanned, scanned.kept_lines))
        if attr.detect_non_english_msvc(text):
            notes.append("MSVC output is not in English (VSLANG=1033 needs the Visual Studio "
                         "English language pack); diagnostics parsing is degraded")
        trace = None
        if action == ACTION_INCLUDE_TRACE:
            root_file = str(meta.get("file_label", ""))
            parse = out.parse_show_includes if meta.get("trace_family") == "msvc" else out.parse_dash_H
            trace = rep.relabel_trace(parse(text, root_file=root_file), source_root=source_root,
                                      build_dir=build_dir)
            trace = self._with_forced(trace, meta, root_file)
        clean = rep.scrub_paths(text, source_root=source_root, build_dir=build_dir)
        digest = dict(self._summarize(self._redact(clean), "build job " + job_id,
                                      bytes_scanned=scanned.bytes_scanned,
                                      truncated=scanned.truncated))
        status = self._status(record, exit_code, text)
        started = _float(meta.get("started_at"), 0.0)
        finished = (_epoch(record.updated_at) if record is not None else None) or self._clock()
        artifacts = ["build log (private)"]
        if meta.get("binlog"):
            artifacts.append("msbuild binlog (private, not parsed in v1)")
        try:
            display = " ".join(json.loads(meta.get("display_argv_json") or "[]"))
        except (TypeError, ValueError):
            display = ""
        report = rep.make_build_report(
            status=status, job_id=job_id, action=action, system=str(meta.get("system", "")),
            target=str(meta.get("target", "")), config=str(meta.get("config", "")),
            command_digest=str(meta.get("command_digest", "")), display_command=display,
            world=str(meta.get("world", "host") or "host"),
            network=str(meta.get("network", "advisory_off") or "advisory_off"),
            isolation_truth=str(meta.get("isolation_truth", "unverified") or "unverified"),
            exit_code=exit_code if isinstance(exit_code, int) else self._exit_code(record),
            duration_seconds=round(max(0.0, finished - started), 3) if started else 0.0,
            attributions=atts, counts=tuple(dset.counts), first_errors=firsts,
            output_digest=digest, include_trace=trace, artifacts=tuple(artifacts),
            log_bytes_scanned=scanned.bytes_scanned,
            output_truncated=bool(scanned.truncated or dset.truncated),
            notes=tuple(self._redact(note) for note in notes),
        )
        if terminal:
            if len(self._cache) >= 256:
                self._cache.pop(next(iter(self._cache)))
            self._cache[job_id] = report
        return report

    @staticmethod
    def _with_forced(trace: Any, meta: Mapping[str, str], root_file: str) -> Any:
        """Forced includes (the real PCH header) as depth-1 edges; -H omits them."""
        try:
            forced = [item for item in json.loads(meta.get("trace_forced_json") or "[]")
                      if isinstance(item, str) and item][:16]
        except ValueError:
            forced = []
        present = set(trace.headers())
        extra = tuple((root_file, header, 1) for header in forced if header not in present)
        if not extra:
            return trace
        return replace(trace, edges=extra + tuple(trace.edges),
                       unique_headers=trace.unique_headers + len(extra),
                       max_depth=max(1, trace.max_depth))

    def _redact_attribution(self, item: Any) -> Any:
        def diag(value: Any) -> Any:
            return replace(value, message=self._redact(value.message)) if value is not None else None

        return replace(item, first_error=diag(item.first_error),
                       diagnostics=tuple(diag(value) for value in item.diagnostics))

    @staticmethod
    def _notes(meta: Mapping[str, str]) -> list[str]:
        try:
            values = json.loads(meta.get("notes_json") or "[]")
        except ValueError:
            return []
        return [item for item in values if isinstance(item, str)] if isinstance(values, list) else []

    @staticmethod
    def _exit_code(record: JobRecord | None) -> int | None:
        if record is not None and isinstance(record.result, Mapping):
            value = record.result.get("exit_code")
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        return None

    @staticmethod
    def _status(record: JobRecord | None, exit_code: int | None, text: str) -> str:
        if record is None or not record.is_terminal:
            return "running"
        if record.status is JobStatus.CANCELLED:
            return "timed_out" if "deadline" in (record.error or "").lower() else "cancelled"
        if exit_code == 127 and "sonder: could not start" in text:
            return "did_not_run"
        if record.status is JobStatus.SUCCEEDED and (exit_code in (0, None)):
            return "succeeded"
        return "failed"


__all__ = [
    "BuildOutputCollector", "MAX_KEPT_LINES", "MAX_SCAN_BYTES", "ScannedLog", "merge_scans", "scan_log",
]
