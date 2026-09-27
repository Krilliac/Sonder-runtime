"""The typed result of one structured test run, bounded for a model.

A report is evidence produced by project code (the test runner and whatever
the project's tests print); it is never an attestation. ``totals_reliable``
is false whenever the structured totals and the runner's own summary line
disagree, or only the text digest was available.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

MAX_FAILURES = 50
MAX_ID_CHARS = 300
MAX_FILE_CHARS = 260
MAX_MESSAGE_CHARS = 400
MAX_SUMMARY_CHARS = 400
MAX_NOTES = 8
MAX_NOTE_CHARS = 200
MAX_WIRE_BYTES = 48_000

STATUSES = ("passed", "failed", "error", "no_tests", "cancelled", "timed_out")


def one_line(text: Any, limit: int) -> str:
    """Collapse whitespace and control characters; cap at ``limit`` chars."""
    value = "".join(ch if ch.isprintable() else " " for ch in str(text or ""))
    value = " ".join(value.split())
    if len(value) > limit:
        value = value[: max(0, limit - 3)] + "..."
    return value


@dataclass(frozen=True, slots=True)
class TestTotals:
    __test__ = False  # not a pytest test class

    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    total: int = 0

    def to_wire(self) -> dict:
        return {"passed": self.passed, "failed": self.failed, "skipped": self.skipped,
                "errors": self.errors, "total": self.total}


@dataclass(frozen=True, slots=True)
class TestFailure:
    __test__ = False  # not a pytest test class

    id: str
    file: str = ""
    line: int | None = None
    kind: str = "failure"
    message_excerpt: str = ""

    @classmethod
    def bounded(cls, id: str, file: str = "", line: int | None = None, kind: str = "failure",
                message: str = "") -> "TestFailure":
        normalized = str(file or "").replace("\\", "/")
        if isinstance(line, bool) or not isinstance(line, int) or line < 0:
            line = None
        return cls(
            id=one_line(id, MAX_ID_CHARS),
            file=one_line(normalized, MAX_FILE_CHARS),
            line=line,
            kind="error" if kind == "error" else "failure",
            message_excerpt=one_line(message, MAX_MESSAGE_CHARS),
        )

    def to_wire(self) -> dict:
        return {"id": self.id, "file": self.file, "line": self.line, "kind": self.kind,
                "message_excerpt": self.message_excerpt}


@dataclass(frozen=True, slots=True)
class TestReport:
    __test__ = False  # not a pytest test class

    runner: str
    status: str
    job_id: str
    command_digest: str
    display_command: tuple[str, ...]
    project: str
    selector: str
    exit_code: int | None
    duration_seconds: float
    totals: TestTotals | None
    totals_source: str
    totals_reliable: bool
    failures: tuple[TestFailure, ...] = ()
    failures_truncated: bool = False
    report_truncated: bool = False
    output_truncated: bool = False
    summary_line: str = ""
    digest: Mapping[str, object] | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


def report_to_wire(report: TestReport) -> dict:
    return {
        "object": "test_report",
        "runner": report.runner,
        "status": report.status,
        "job_id": report.job_id,
        "command_digest": report.command_digest,
        "display_command": list(report.display_command),
        "project": report.project,
        "selector": report.selector,
        "exit_code": report.exit_code,
        "duration_seconds": round(float(report.duration_seconds), 3),
        "totals": report.totals.to_wire() if report.totals is not None else None,
        "totals_source": report.totals_source,
        "totals_reliable": report.totals_reliable,
        "failures": [item.to_wire() for item in report.failures],
        "failures_truncated": report.failures_truncated,
        "report_truncated": report.report_truncated,
        "output_truncated": report.output_truncated,
        "summary_line": report.summary_line,
        "digest": dict(report.digest) if report.digest is not None else None,
        "notes": list(report.notes),
    }


def report_from_wire(data: Mapping[str, Any]) -> TestReport:
    """Rebuild a cached report; raises ``ValueError`` on any malformed field."""
    if not isinstance(data, Mapping) or data.get("object") != "test_report":
        raise ValueError("not a cached test report")
    totals = data.get("totals")
    failures = data.get("failures") or []
    if not isinstance(failures, list) or len(failures) > MAX_FAILURES:
        raise ValueError("cached report failures are malformed")
    status = str(data.get("status", ""))
    if status not in STATUSES:
        raise ValueError("cached report status is unknown")
    exit_code = data.get("exit_code")
    if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
        raise ValueError("cached report exit code is malformed")
    digest = data.get("digest")
    return TestReport(
        runner=str(data.get("runner", "")),
        status=status,
        job_id=str(data.get("job_id", "")),
        command_digest=str(data.get("command_digest", "")),
        display_command=tuple(str(item) for item in data.get("display_command") or ()),
        project=str(data.get("project", "")),
        selector=str(data.get("selector", "")),
        exit_code=exit_code,
        duration_seconds=float(data.get("duration_seconds") or 0.0),
        totals=(TestTotals(**{key: int(totals[key]) for key in
                              ("passed", "failed", "skipped", "errors", "total")})
                if isinstance(totals, Mapping) else None),
        totals_source=str(data.get("totals_source", "")),
        totals_reliable=bool(data.get("totals_reliable")),
        failures=tuple(TestFailure.bounded(
            str(item.get("id", "")), str(item.get("file", "")), item.get("line"),
            str(item.get("kind", "failure")), str(item.get("message_excerpt", "")),
        ) for item in failures if isinstance(item, Mapping)),
        failures_truncated=bool(data.get("failures_truncated")),
        report_truncated=bool(data.get("report_truncated")),
        output_truncated=bool(data.get("output_truncated")),
        summary_line=one_line(data.get("summary_line", ""), MAX_SUMMARY_CHARS),
        digest=dict(digest) if isinstance(digest, Mapping) else None,
        notes=tuple(one_line(item, MAX_NOTE_CHARS) for item in (data.get("notes") or ())[:MAX_NOTES]),
    )


def _size(payload: Mapping) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def fit_wire(report: TestReport, max_bytes: int = MAX_WIRE_BYTES) -> dict:
    """``report_to_wire`` shrunk under ``max_bytes`` UTF-8 bytes.

    Drops, in order: the digest's tail, then its remaining list fields, then
    failures (from the end), then notes; every drop sets a truncation flag.
    The small scalar core (status, totals, command identity) always fits.
    """
    payload = report_to_wire(report)
    if _size(payload) <= max_bytes:
        return payload
    digest = payload.get("digest")
    if isinstance(digest, dict):
        digest = dict(digest)
        if digest.get("tail"):
            digest["tail"] = []
            digest["truncated"] = True
            payload["digest"] = digest
            payload["output_truncated"] = True
            if _size(payload) <= max_bytes:
                return payload
        for key in ("groups", "first_errors", "failure_lines"):
            if digest.get(key):
                digest[key] = []
                digest["truncated"] = True
                payload["digest"] = dict(digest)
                if _size(payload) <= max_bytes:
                    return payload
    failures = list(payload["failures"])
    while failures and _size(payload) > max_bytes:
        # Halve rather than pop one at a time: 50 x 1 KiB entries converge fast.
        failures = failures[: len(failures) // 2]
        payload["failures"] = failures
        payload["failures_truncated"] = True
    if _size(payload) <= max_bytes:
        return payload
    payload["notes"] = []
    if _size(payload) <= max_bytes:
        return payload
    payload["digest"] = None
    payload["output_truncated"] = True
    if _size(payload) <= max_bytes:
        return payload
    payload["display_command"] = payload["display_command"][:1]
    payload["summary_line"] = payload["summary_line"][:120]
    return payload


def render_report(report: TestReport, *, max_chars: int = 6000) -> str:
    """Human-readable text: status line, totals, failures, digest summary."""
    max_chars = max(200, min(int(max_chars), 16_000))
    lines = []
    totals = report.totals
    counts = ("%d passed, %d failed, %d skipped, %d errors (of %d)" % (
        totals.passed, totals.failed, totals.skipped, totals.errors, totals.total)
        if totals is not None else "totals unavailable")
    lines.append("test run %s: %s [%s] exit=%s in %.1fs" % (
        report.job_id, report.status, report.runner,
        "-" if report.exit_code is None else report.exit_code, report.duration_seconds))
    lines.append("command: %s" % " ".join(report.display_command))
    lines.append("project: %s%s" % (report.project, "  selector: %s" % report.selector
                                    if report.selector else ""))
    lines.append("totals: %s (source %s%s)" % (
        counts, report.totals_source or "none",
        "" if report.totals_reliable else ", unverified"))
    if report.summary_line:
        lines.append("summary: %s" % report.summary_line)
    if report.failures:
        lines.append("failures:")
        for item in report.failures:
            where = item.file + (":%d" % item.line if item.line is not None else "")
            lines.append("  %s %s%s%s" % (
                "ERROR" if item.kind == "error" else "FAIL", item.id,
                " (%s)" % where if where else "",
                " - %s" % item.message_excerpt if item.message_excerpt else ""))
        if report.failures_truncated:
            lines.append("  ... more failures not shown")
    digest = report.digest or {}
    failure_lines = digest.get("failure_lines") if isinstance(digest, Mapping) else None
    if not report.failures and failure_lines:
        lines.append("failure lines:")
        lines.extend("  " + str(item) for item in list(failure_lines)[:20])
    tail = digest.get("tail") if isinstance(digest, Mapping) else None
    if tail and report.status not in {"passed"}:
        lines.append("output tail:")
        lines.extend("  " + str(item) for item in list(tail)[-10:])
    flags = [name for name, value in (("report truncated", report.report_truncated),
                                      ("output truncated", report.output_truncated)) if value]
    if flags:
        lines.append("note: " + ", ".join(flags))
    lines.extend("note: " + note for note in report.notes)
    text = "\n".join(lines)
    suffix = "\n... (truncated)"
    if len(text) > max_chars:
        text = text[: max_chars - len(suffix)].rstrip() + suffix
    return text


def with_notes(report: TestReport, *notes: str) -> TestReport:
    merged = tuple(dict.fromkeys((*report.notes, *(one_line(n, MAX_NOTE_CHARS) for n in notes if n))))
    return replace(report, notes=merged[:MAX_NOTES])


__all__ = [
    "MAX_FAILURES", "MAX_WIRE_BYTES", "STATUSES", "TestFailure", "TestReport", "TestTotals",
    "fit_wire", "one_line", "render_report", "report_from_wire", "report_to_wire", "with_notes",
]
