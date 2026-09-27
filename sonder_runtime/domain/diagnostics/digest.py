"""Bounded digest of any build, test or log output.

The generalization of the shell habit::

    pytest ... > out.txt; tail -1 out.txt; grep -E "^(FAILED|ERROR) " out.txt | cut -c1-230

``digest_text`` returns the final line (``tail -1``), the recognized run
summary, the failure lines (``grep``), the first distinct errors parsed into
typed diagnostics, signature groups, and a short tail -- every field capped,
so a digest of a 50,000-line log still fits a model-visible payload.

Pure: the caller supplies already-redacted text.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass

from .model import (
    SEVERITY_RANK,
    Diagnostic,
    DiagnosticGroup,
    Severity,
    clean_text,
    diagnostic_to_wire,
    group_to_wire,
    render_diagnostic,
    strip_ansi,
)
from .parsers import DEFAULT_ERROR_LINE_PATTERN, error_lines, parse_diagnostics
from .summary import RunSummary, find_summary


MAX_LINE_CHARS = 300
MAX_FINAL_LINE_CHARS = 400
MAX_LABEL_CHARS = 200
MAX_WIRE_BYTES = 48_000
MAX_RENDER_CHARS = 16_000
HARD_MAX_LINES = 50_000
_HEAD_KEEP_LINES = 5_000
_MAX_SCAN_LINE_CHARS = 4_096

FAILURE_LINE_PATTERN = (
    r"^(?:FAILED|ERROR)\b|\bFAIL(?:ED)?\b|^error(?:\[|:)|:\s*(?:fatal )?error\b"
    r"|\bError\s+\d+$|^---- .* stdout ----$"
)
_FAILURE_LINE_RE = re.compile(FAILURE_LINE_PATTERN)
_SOURCE_KINDS = frozenset({"job", "file", "text"})


_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _line_text(line: str, limit: int) -> str:
    """One output line as grep/tail would show it: spacing kept, controls gone."""
    text = strip_ansi(line).replace("\t", " ").replace("\r", "")
    return _CONTROL_RE.sub("", text).rstrip()[:limit]


def _clamp(value: object, default: int, low: int, high: int) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        number = default
    return max(low, min(number, high))


@dataclass(frozen=True, slots=True)
class OutputDigest:
    source_kind: str
    source_label: str
    lines_scanned: int
    bytes_scanned: int
    scan_truncated: bool
    final_line: str
    summary: RunSummary | None
    counts: tuple[tuple[str, int], ...]
    failure_lines: tuple[str, ...]
    first_errors: tuple[Diagnostic, ...]
    groups: tuple[DiagnosticGroup, ...]
    tail: tuple[str, ...]
    truncated: bool

    def to_wire(self, *, max_bytes: int = MAX_WIRE_BYTES) -> dict:
        """JSON-ready dict of at most ``max_bytes`` UTF-8 bytes.

        When the full digest would not fit, the tail is shortened first, then
        groups, then failure lines, then first errors; ``truncated`` and
        ``wire_truncated`` say so.
        """
        max_bytes = _clamp(max_bytes, MAX_WIRE_BYTES, 2_048, MAX_WIRE_BYTES)
        wire = {
            "object": "output_digest",
            "source_kind": self.source_kind,
            "source_label": self.source_label,
            "lines_scanned": self.lines_scanned,
            "bytes_scanned": self.bytes_scanned,
            "scan_truncated": self.scan_truncated,
            "final_line": self.final_line,
            "summary": self.summary.to_wire() if self.summary is not None else None,
            "counts": dict(self.counts),
            "failure_lines": list(self.failure_lines),
            "first_errors": [diagnostic_to_wire(item) for item in self.first_errors],
            "groups": [group_to_wire(item) for item in self.groups],
            "tail": list(self.tail),
            "truncated": self.truncated,
            "wire_truncated": False,
        }
        for field in ("tail", "groups", "failure_lines", "first_errors"):
            while _wire_size(wire) > max_bytes and wire[field]:
                wire[field].pop()
                wire["truncated"] = True
                wire["wire_truncated"] = True
        if _wire_size(wire) > max_bytes:
            # Only the scalar fields remain; their own caps keep this bounded.
            wire["final_line"] = wire["final_line"][:100]
            wire["source_label"] = wire["source_label"][:100]
            wire["truncated"] = True
            wire["wire_truncated"] = True
        return wire


def _wire_size(wire: dict) -> int:
    return len(json.dumps(wire, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _window(lines: list[str], max_lines: int) -> tuple[list[str], bool]:
    """Keep a head and a (larger) tail when the input exceeds ``max_lines``."""
    if len(lines) <= max_lines:
        return lines, False
    head = min(_HEAD_KEEP_LINES, max_lines // 10)
    return lines[:head] + lines[len(lines) - (max_lines - head):], True


def digest_text(
    text: str,
    *,
    source_kind: str = "text",
    source_label: str = "",
    tail_lines: int = 20,
    max_failure_lines: int = 40,
    max_first_errors: int = 10,
    max_groups: int = 10,
    max_lines: int = HARD_MAX_LINES,
    scan_truncated: bool = False,
    bytes_scanned: int | None = None,
    tools: Iterable[str] | None = None,
) -> OutputDigest:
    """Summarize one bounded text window."""
    tail_lines = _clamp(tail_lines, 20, 1, 200)
    max_failure_lines = _clamp(max_failure_lines, 40, 1, 200)
    max_first_errors = _clamp(max_first_errors, 10, 1, 50)
    max_groups = _clamp(max_groups, 10, 1, 50)
    max_lines = _clamp(max_lines, HARD_MAX_LINES, 1, HARD_MAX_LINES)
    kind = source_kind if source_kind in _SOURCE_KINDS else "text"
    raw = str(text or "")
    if bytes_scanned is None:
        bytes_scanned = len(raw.encode("utf-8", errors="replace"))
    all_lines = raw.splitlines()
    lines, line_capped = _window(all_lines, max_lines)
    lines = [strip_ansi(line[: _MAX_SCAN_LINE_CHARS * 2])[:_MAX_SCAN_LINE_CHARS].rstrip()
             for line in lines]

    final_line = ""
    for line in reversed(lines):
        if line.strip():
            final_line = clean_text(line, MAX_FINAL_LINE_CHARS)
            break

    summary = find_summary(lines)

    failure_lines: list[str] = []
    failure_seen: set[str] = set()
    failure_total = 0
    for line in lines:
        if not _FAILURE_LINE_RE.search(line):
            continue
        cleaned = _line_text(line, MAX_LINE_CHARS)
        if not cleaned.strip() or cleaned in failure_seen:
            continue
        failure_seen.add(cleaned)
        failure_total += 1
        if len(failure_lines) < max_failure_lines:
            failure_lines.append(cleaned)

    parsed = parse_diagnostics("\n".join(lines), tools=tools, max_lines=max_lines)
    first_errors = _first_errors(parsed.diagnostics, max_first_errors)
    error_line_total = len(error_lines("\n".join(lines), DEFAULT_ERROR_LINE_PATTERN))

    counts = dict(parsed.counts)
    for severity in (Severity.FATAL.value, Severity.ERROR.value,
                     Severity.WARNING.value, Severity.NOTE.value):
        counts.setdefault(severity, 0)
    counts["failure_lines"] = failure_total
    counts["error_lines"] = error_line_total

    trimmed = list(lines)
    while trimmed and not trimmed[-1].strip():
        trimmed.pop()
    tail = tuple(_line_text(line, MAX_LINE_CHARS) for line in trimmed[-tail_lines:])

    truncated = bool(
        scan_truncated or line_capped or parsed.truncated
        or failure_total > len(failure_lines)
        or len(parsed.groups) > max_groups
    )
    return OutputDigest(
        source_kind=kind,
        source_label=clean_text(source_label, MAX_LABEL_CHARS),
        lines_scanned=len(lines),
        bytes_scanned=max(0, int(bytes_scanned)),
        scan_truncated=bool(scan_truncated or line_capped),
        final_line=final_line,
        summary=summary,
        counts=tuple(counts.items()),
        failure_lines=tuple(failure_lines),
        first_errors=first_errors,
        groups=tuple(parsed.groups[:max_groups]),
        tail=tail,
        truncated=truncated,
    )


def _first_errors(diagnostics: tuple[Diagnostic, ...], limit: int) -> tuple[Diagnostic, ...]:
    """First ``limit`` diagnostics unique by signature; fatals and errors lead."""
    unique: list[Diagnostic] = []
    seen: set[str] = set()
    for diagnostic in diagnostics:
        signature = diagnostic.signature()
        if signature in seen:
            continue
        seen.add(signature)
        unique.append(diagnostic)
    serious_rank = SEVERITY_RANK[Severity.ERROR.value]
    unique.sort(key=lambda d: (
        0 if SEVERITY_RANK.get(d.severity, 9) <= serious_rank else 1, d.raw_line_no,
    ))
    return tuple(unique[:limit])


def render_digest(digest: OutputDigest, *, max_chars: int = 4000) -> str:
    """Operator/model text form; at most ``max_chars`` (hard cap 16,000)."""
    max_chars = _clamp(max_chars, 4000, 200, MAX_RENDER_CHARS)
    lines: list[str] = []
    if digest.summary is not None:
        lines.append("summary: %s" % digest.summary.line)
        detail = ["status=%s" % digest.summary.status]
        for name in ("passed", "failed", "skipped", "errors", "total"):
            value = getattr(digest.summary, name)
            if value is not None:
                detail.append("%s=%d" % (name, value))
        if digest.summary.duration_seconds is not None:
            detail.append("duration=%.2fs" % digest.summary.duration_seconds)
        lines.append("result: %s (%s)" % (digest.summary.tool, " ".join(detail)))
        if digest.final_line and digest.final_line != digest.summary.line:
            lines.append("final: %s" % digest.final_line)
    else:
        lines.append("final: %s" % (digest.final_line or "(no output)"))
    counts = " ".join("%s=%d" % (key, value) for key, value in digest.counts if value)
    lines.append("counts: %s" % (counts or "none"))
    scanned = "scanned: %d lines, %d bytes" % (digest.lines_scanned, digest.bytes_scanned)
    if digest.scan_truncated:
        scanned += " (window truncated)"
    if digest.source_label:
        scanned += " from %s" % digest.source_label
    lines.append(scanned)
    if digest.failure_lines:
        lines.append("failure lines:")
        lines.extend("  %s" % line for line in digest.failure_lines)
    if digest.first_errors:
        lines.append("first errors:")
        lines.extend("  %s" % render_diagnostic(item) for item in digest.first_errors)
    repeated = [group for group in digest.groups if group.count > 1]
    if repeated:
        lines.append("repeated:")
        for group in repeated:
            files = (" in " + ", ".join(group.files[:3])) if group.files else ""
            code = "[%s] " % group.code if group.code else ""
            lines.append("  %dx %s %s%s%s" % (
                group.count, group.severity, code, group.template[:160], files,
            ))
    if digest.tail:
        lines.append("tail:")
        lines.extend("  %s" % line for line in digest.tail)
    if digest.truncated:
        lines.append("(digest truncated)")
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    marker = "\n... (digest cut at %d chars)" % max_chars
    return text[: max(0, max_chars - len(marker))].rstrip() + marker


__all__ = [
    "FAILURE_LINE_PATTERN", "MAX_RENDER_CHARS", "MAX_WIRE_BYTES",
    "OutputDigest", "digest_text", "render_digest",
]
