"""Typed, fail-closed coverage inventory for the EVAL-002 corpus.

The inventory is deliberately separate from evaluation execution.  It records
what was actually scanned from each required source class (repository, tool,
and memory), binds the result to deterministic digests, and refuses to be
used when a source is absent, malformed, or truncated by a scan bound.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Any, Mapping, Protocol, Sequence


MAX_REQUIRED_SOURCE_KINDS = 3
MAX_SOURCE_REPORTS = 16
MAX_RECORDS_PER_SOURCE = 1_024
MAX_RECORD_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024


class EvaluationCorpusCoverageError(ValueError):
    """Raised when an inventory cannot prove complete corpus coverage."""


class CorpusSourceKind(str, Enum):
    REPOSITORY = "repository"
    TOOL = "tool"
    MEMORY = "memory"


class CoverageClassification(str, Enum):
    COMPLETE = "complete"
    MISSING_SOURCE = "missing_source"
    SCAN_LIMIT = "scan_limit"
    INVALID_SOURCE = "invalid_source"
    READ_ERROR = "read_error"
    DUPLICATE_SOURCE = "duplicate_source"
    UNEXPECTED_SOURCE = "unexpected_source"


class CorpusSourceReader(Protocol):
    """A bounded source reader supplied by an adapter."""

    def read(self, *, max_records: int, max_record_bytes: int, max_total_bytes: int) -> Sequence[Mapping[str, Any]]: ...


@dataclass(frozen=True)
class CorpusSourceSpec:
    source_id: str
    kind: CorpusSourceKind
    required: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise EvaluationCorpusCoverageError("source_id must be non-empty")
        if not isinstance(self.kind, CorpusSourceKind):
            raise EvaluationCorpusCoverageError("source kind is invalid")
        if not isinstance(self.required, bool):
            raise EvaluationCorpusCoverageError("required must be boolean")


@dataclass(frozen=True)
class CorpusRecord:
    record_id: str
    digest: str
    bytes_scanned: int


@dataclass(frozen=True)
class CorpusSourceReport:
    source_id: str
    kind: CorpusSourceKind
    classification: CoverageClassification
    records: tuple[CorpusRecord, ...] = ()
    source_digest: str = ""
    bytes_scanned: int = 0
    reason: str = ""

    @property
    def complete(self) -> bool:
        return self.classification is CoverageClassification.COMPLETE

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise EvaluationCorpusCoverageError("report source_id must be non-empty")
        if not isinstance(self.kind, CorpusSourceKind):
            raise EvaluationCorpusCoverageError("report kind is invalid")
        if not isinstance(self.classification, CoverageClassification):
            raise EvaluationCorpusCoverageError("report classification is invalid")
        if len(self.records) > MAX_RECORDS_PER_SOURCE:
            raise EvaluationCorpusCoverageError("source record count exceeds bound")
        if self.bytes_scanned < 0 or self.bytes_scanned > MAX_TOTAL_BYTES:
            raise EvaluationCorpusCoverageError("source byte count exceeds bound")
        if self.classification is CoverageClassification.COMPLETE and not self.source_digest:
            raise EvaluationCorpusCoverageError("complete source report requires a digest")


@dataclass(frozen=True)
class EvaluationCorpusInventory:
    """Immutable inventory of the required EVAL-002 source classes."""

    reports: tuple[CorpusSourceReport, ...]
    required_kinds: frozenset[CorpusSourceKind] = frozenset(CorpusSourceKind)
    schema: str = "sonder.evaluation-corpus-coverage.v1"

    def __post_init__(self) -> None:
        if len(self.reports) > MAX_SOURCE_REPORTS:
            raise EvaluationCorpusCoverageError("too many source reports")
        if self.required_kinds != frozenset(CorpusSourceKind):
            raise EvaluationCorpusCoverageError("repository, tool, and memory coverage are required")
        # Duplicate IDs are retained as reports so the caller can inspect the
        # explicit duplicate classification; ``complete`` remains false.

    @property
    def missing(self) -> tuple[CoverageClassification, ...]:
        classifications = {
            report.classification
            for report in self.reports
            if report.kind in self.required_kinds and not report.complete
        }
        present = {report.kind for report in self.reports if report.complete}
        classifications.update(CoverageClassification.MISSING_SOURCE for kind in self.required_kinds - present)
        return tuple(sorted(classifications, key=lambda item: item.value))

    @property
    def complete(self) -> bool:
        if len({report.kind for report in self.reports if report.complete}) != MAX_REQUIRED_SOURCE_KINDS:
            return False
        return all(
            any(report.kind is kind and report.complete for report in self.reports)
            for kind in self.required_kinds
        ) and not any(report.classification is not CoverageClassification.COMPLETE for report in self.reports)

    @property
    def digest(self) -> str:
        material = {
            "schema": self.schema,
            "required_kinds": sorted(kind.value for kind in self.required_kinds),
            "reports": [_report_payload(report) for report in sorted(self.reports, key=lambda item: item.source_id)],
        }
        return hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()

    def require_complete(self) -> "EvaluationCorpusInventory":
        if not self.complete:
            missing = ", ".join(item.value for item in self.missing) or "unknown"
            raise EvaluationCorpusCoverageError(f"evaluation corpus coverage is incomplete: {missing}")
        return self

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "digest": self.digest,
            "complete": self.complete,
            "missing": [item.value for item in self.missing],
            "reports": [_report_payload(report) for report in sorted(self.reports, key=lambda item: item.source_id)],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvaluationCorpusInventory":
        """Restore a persisted inventory and verify its content digest."""
        if not isinstance(payload, Mapping) or payload.get("schema") != "sonder.evaluation-corpus-coverage.v1":
            raise EvaluationCorpusCoverageError("unsupported corpus inventory payload")
        raw_reports = payload.get("reports")
        if not isinstance(raw_reports, list):
            raise EvaluationCorpusCoverageError("corpus inventory reports are missing")
        reports: list[CorpusSourceReport] = []
        try:
            for raw in raw_reports:
                if not isinstance(raw, Mapping):
                    raise EvaluationCorpusCoverageError("corpus report is malformed")
                records = tuple(
                    CorpusRecord(str(item["record_id"]), str(item["digest"]), int(item["bytes_scanned"]))
                    for item in raw.get("records", [])
                )
                reports.append(CorpusSourceReport(
                    str(raw["source_id"]), CorpusSourceKind(str(raw["kind"])),
                    CoverageClassification(str(raw["classification"])), records,
                    str(raw.get("source_digest", "")), int(raw.get("bytes_scanned", 0)),
                    str(raw.get("reason", "")),
                ))
        except (KeyError, TypeError, ValueError) as exc:
            raise EvaluationCorpusCoverageError("corpus inventory report is malformed") from exc
        inventory = cls(tuple(reports), schema=str(payload["schema"]))
        if payload.get("digest") != inventory.digest:
            raise EvaluationCorpusCoverageError("corpus inventory digest mismatch")
        if payload.get("complete") is not inventory.complete:
            raise EvaluationCorpusCoverageError("corpus inventory completeness mismatch")
        return inventory


def build_inventory(reports: Sequence[CorpusSourceReport]) -> EvaluationCorpusInventory:
    """Build an immutable inventory from already bounded adapter reports."""
    if not isinstance(reports, Sequence) or isinstance(reports, (str, bytes)):
        raise EvaluationCorpusCoverageError("reports must be a sequence")
    return EvaluationCorpusInventory(tuple(reports))


def record_from_payload(payload: Mapping[str, Any], *, max_record_bytes: int = MAX_RECORD_BYTES) -> CorpusRecord:
    if not isinstance(payload, Mapping):
        raise EvaluationCorpusCoverageError("corpus record must be a mapping")
    record_id = payload.get("id", payload.get("record_id"))
    if not isinstance(record_id, str) or not record_id.strip():
        raise EvaluationCorpusCoverageError("corpus record requires a non-empty id")
    encoded = _canonical(payload).encode("utf-8")
    if len(encoded) > max_record_bytes:
        raise EvaluationCorpusCoverageError("corpus record exceeds byte bound")
    return CorpusRecord(record_id.strip(), hashlib.sha256(encoded).hexdigest(), len(encoded))


def _report_payload(report: CorpusSourceReport) -> dict[str, Any]:
    return {
        "source_id": report.source_id,
        "kind": report.kind.value,
        "classification": report.classification.value,
        "records": [
            {"record_id": item.record_id, "digest": item.digest, "bytes_scanned": item.bytes_scanned}
            for item in report.records
        ],
        "source_digest": report.source_digest,
        "bytes_scanned": report.bytes_scanned,
        "reason": report.reason,
    }


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise EvaluationCorpusCoverageError("corpus data must be JSON-compatible") from exc


__all__ = [
    "CorpusRecord", "CorpusSourceKind", "CorpusSourceReader", "CorpusSourceReport",
    "CorpusSourceSpec", "CoverageClassification", "EvaluationCorpusCoverageError",
    "EvaluationCorpusInventory", "MAX_RECORD_BYTES", "MAX_RECORDS_PER_SOURCE",
    "MAX_TOTAL_BYTES", "build_inventory", "record_from_payload",
]
