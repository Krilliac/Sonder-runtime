"""The build job report: what ran, what failed where, bounded for the wire.

``relabel_*`` turn absolute paths inside diagnostics and traces into the
model's labels so no absolute path reaches a model-facing payload; the
collector applies them before building a report.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace

from ..diagnostics.model import Diagnostic, diagnostic_to_wire, render_diagnostic
from .attribution import Attribution
from .model import (
    ISOLATION_LABELS,
    BuildDomainError,
    bounded_notes,
    clip,
    norm_path,
    path_label,
)
from .output import IncludeTrace


BUILD_STATUSES = frozenset({"succeeded", "failed", "cancelled", "timed_out", "did_not_run",
                            "running"})
MAX_REPORT_WIRE_BYTES = 48_000
MAX_ATTRIBUTIONS = 50
MAX_REPORT_FIRST_ERRORS = 24
MAX_DISPLAY_CHARS = 2000


@dataclass(frozen=True, slots=True)
class BuildJobReport:
    status: str
    job_id: str
    action: str
    system: str
    target: str = ""
    config: str = ""
    command_digest: str = ""
    display_command: str = ""
    world: str = "host"
    network: str = "advisory_off"
    isolation_truth: str = "unverified"
    exit_code: int | None = None
    duration_seconds: float = 0.0
    attributions: tuple[Attribution, ...] = ()
    counts: tuple[tuple[str, int], ...] = ()
    first_errors: tuple[Diagnostic, ...] = ()
    output_digest: dict | None = None
    include_trace: IncludeTrace | None = None
    artifacts: tuple[str, ...] = ()
    log_bytes_scanned: int = 0
    output_truncated: bool = False
    notes: tuple[str, ...] = ()
    digest: str = ""

    def __post_init__(self) -> None:
        if self.status not in BUILD_STATUSES:
            raise BuildDomainError("BUILD_MODEL_UNAVAILABLE", "unknown build status")
        if self.isolation_truth not in ISOLATION_LABELS:
            raise BuildDomainError("BUILD_MODEL_UNAVAILABLE", "isolation_truth must be an EXEC-006 label")
        if len(self.attributions) > MAX_ATTRIBUTIONS or len(self.first_errors) > MAX_REPORT_FIRST_ERRORS:
            raise BuildDomainError("BUILD_MODEL_UNAVAILABLE", "report lists exceed their bounds")

    def count(self, severity: str) -> int:
        return dict(self.counts).get(severity, 0)


def _attribution_wire(item: Attribution) -> dict:
    return {
        "kind": item.kind,
        "label": item.label,
        "project": item.project,
        "first_error": diagnostic_to_wire(item.first_error) if item.first_error else None,
        "error_count": item.error_count,
        "warning_count": item.warning_count,
        "diagnostics": [diagnostic_to_wire(diag) for diag in item.diagnostics],
    }


def trace_to_wire(trace: IncludeTrace) -> dict:
    return {
        "root_file": trace.root_file,
        "edges": [[parent, child, depth] for parent, child, depth in trace.edges],
        "truncated": trace.truncated,
        "unique_headers": trace.unique_headers,
        "max_depth": trace.max_depth,
        "pch_consumed": trace.pch_consumed,
    }


def _full_wire(report: BuildJobReport) -> dict:
    return {
        "object": "build_job_report",
        "status": report.status,
        "job_id": report.job_id,
        "action": report.action,
        "system": report.system,
        "target": report.target,
        "config": report.config,
        "command_digest": report.command_digest,
        "display_command": report.display_command,
        "world": report.world,
        "network": report.network,
        "isolation_truth": report.isolation_truth,
        "exit_code": report.exit_code,
        "duration_seconds": round(float(report.duration_seconds), 3),
        "attributions": [_attribution_wire(item) for item in report.attributions],
        "counts": dict(report.counts),
        "first_errors": [diagnostic_to_wire(item) for item in report.first_errors],
        "output_digest": report.output_digest,
        "include_trace": trace_to_wire(report.include_trace) if report.include_trace else None,
        "artifacts": list(report.artifacts),
        "log_bytes_scanned": report.log_bytes_scanned,
        "output_truncated": report.output_truncated,
        "notes": list(report.notes),
        "digest": report.digest,
        "wire_truncated": False,
    }


def _size(wire: dict) -> int:
    return len(json.dumps(wire, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def report_digest(report: BuildJobReport) -> str:
    wire = _full_wire(replace(report, digest=""))
    material = json.dumps(wire, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def make_build_report(**fields) -> BuildJobReport:
    """Build a report with bounded notes, lists and display text, and its digest."""
    fields["notes"] = bounded_notes(fields.get("notes", ()))
    fields["display_command"] = clip(fields.get("display_command", ""), MAX_DISPLAY_CHARS)
    fields["attributions"] = tuple(fields.get("attributions", ()))[:MAX_ATTRIBUTIONS]
    fields["first_errors"] = tuple(fields.get("first_errors", ()))[:MAX_REPORT_FIRST_ERRORS]
    report = BuildJobReport(**fields)
    return replace(report, digest=report_digest(report))


def build_report_to_wire(report: BuildJobReport, *, max_bytes: int = MAX_REPORT_WIRE_BYTES) -> dict:
    """JSON-ready report of at most ``max_bytes`` UTF-8 bytes (<= 48 KB).

    Shrinks in order: the embedded output digest, include-trace edges,
    per-attribution diagnostic lists, attributions, first errors, notes.
    """
    budget = max(4096, min(int(max_bytes), MAX_REPORT_WIRE_BYTES))
    wire = _full_wire(report)
    if _size(wire) <= budget:
        return wire
    wire["wire_truncated"] = True
    if report.output_digest is not None:
        digest = dict(report.output_digest)
        for field in ("tail", "groups", "failure_lines", "first_errors"):
            while _size(wire) > budget and digest.get(field):
                digest[field] = list(digest[field])[:-1]
                digest["wire_truncated"] = True
                wire["output_digest"] = digest
    trace = wire.get("include_trace")
    if trace:
        while _size(wire) > budget and trace["edges"]:
            keep = max(0, len(trace["edges"]) // 2 if len(trace["edges"]) > 64 else len(trace["edges"]) - 1)
            trace["edges"] = trace["edges"][:keep]
            trace["truncated"] = True
    for item in wire["attributions"]:
        if _size(wire) <= budget:
            break
        item["diagnostics"] = item["diagnostics"][:1]
    while _size(wire) > budget and wire["attributions"]:
        wire["attributions"].pop()
    while _size(wire) > budget and wire["first_errors"]:
        wire["first_errors"].pop()
    while _size(wire) > budget and wire["notes"]:
        wire["notes"].pop()
    if _size(wire) > budget:
        wire["output_digest"] = None
        wire["include_trace"] = None
        wire["display_command"] = wire["display_command"][:200]
    return wire


def render_build_report(report: BuildJobReport, *, max_chars: int = 4000) -> str:
    """Human-readable report for REPL and logs."""
    limit = max(200, min(int(max_chars), 16_000))
    head = "build %s %s: %s" % (report.action, report.target or "(default)", report.status)
    if report.exit_code is not None:
        head += " (exit %d)" % report.exit_code
    lines = [head, "  %s | world=%s network=%s isolation=%s | %.1fs" % (
        report.display_command[:300], report.world, report.network, report.isolation_truth,
        float(report.duration_seconds))]
    counts = ", ".join("%s=%d" % item for item in report.counts)
    if counts:
        lines.append("  diagnostics: " + counts)
    for item in report.attributions[:12]:
        first = render_diagnostic(item.first_error, max_chars=200) if item.first_error else "-"
        lines.append("  [%s] %s: %d error(s), %d warning(s); first: %s" % (
            item.kind, item.label, item.error_count, item.warning_count, first))
    if report.include_trace is not None:
        trace = report.include_trace
        lines.append("  include trace of %s: %d headers, depth %d%s" % (
            trace.root_file, trace.unique_headers, trace.max_depth,
            " (truncated)" if trace.truncated else ""))
    for note in report.notes[:8]:
        lines.append("  note: " + note)
    text = "\n".join(lines)
    return text if len(text) <= limit else text[: limit - 3] + "..."


# --- labeling -------------------------------------------------------------------------

def _roots_pattern(source_root: str, build_dir: str) -> list[tuple[str, str]]:
    pairs = []
    for root, replacement in ((build_dir, "<build>"), (source_root, "")):
        normalized = norm_path(root)
        if normalized and normalized not in ("/", "."):
            pairs.append((normalized, replacement))
            windows = normalized.replace("/", "\\")
            if windows != normalized:
                pairs.append((windows, replacement))
    return pairs


def scrub_paths(text: str, *, source_root: str, build_dir: str) -> str:
    """Replace the source root and build dir prefixes inside free text."""
    value = str(text or "")
    for root, replacement in _roots_pattern(source_root, build_dir):
        pattern = re.compile(re.escape(root) + r"[/\\]?", re.IGNORECASE if ":" in root[:3] else 0)
        value = pattern.sub(lambda _m: (replacement + "/") if replacement else "", value)
    return value


def relabel_diagnostic(diagnostic: Diagnostic, *, source_root: str, build_dir: str) -> Diagnostic:
    label = diagnostic.file
    if label:
        label, _rel = path_label(label, source_root=source_root, build_dir=build_dir)
    message = scrub_paths(diagnostic.message, source_root=source_root, build_dir=build_dir)
    return replace(diagnostic, file=label[:260], message=message[:400])


def relabel_attribution(item: Attribution, *, source_root: str, build_dir: str) -> Attribution:
    label = item.label
    if label and label.startswith(("/", "\\")) or (len(label) > 2 and label[1] == ":"):
        label, _ = path_label(label, source_root=source_root, build_dir=build_dir)
    return replace(
        item, label=label,
        first_error=relabel_diagnostic(item.first_error, source_root=source_root,
                                       build_dir=build_dir) if item.first_error else None,
        diagnostics=tuple(relabel_diagnostic(diag, source_root=source_root, build_dir=build_dir)
                          for diag in item.diagnostics),
    )


def relabel_trace(trace: IncludeTrace, *, source_root: str, build_dir: str) -> IncludeTrace:
    def label(path: str) -> str:
        return path_label(path, source_root=source_root, build_dir=build_dir)[0] \
            if path.startswith("/") or (len(path) > 2 and path[1] == ":") else path

    return replace(trace, root_file=label(trace.root_file),
                   edges=tuple((label(parent), label(child), depth)
                               for parent, child, depth in trace.edges))


__all__ = [
    "BUILD_STATUSES", "BuildJobReport", "MAX_REPORT_WIRE_BYTES", "build_report_to_wire",
    "make_build_report", "relabel_attribution", "relabel_diagnostic", "relabel_trace",
    "render_build_report", "report_digest", "scrub_paths", "trace_to_wire",
]
