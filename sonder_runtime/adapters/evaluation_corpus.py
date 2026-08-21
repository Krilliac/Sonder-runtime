"""Bounded adapters that turn repository, tool, and memory readers into EVAL-002 reports."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from sonder_runtime.application.evaluation.corpus_inventory import (
    CorpusSourceKind,
    CorpusSourceReport,
    CorpusSourceSpec,
    CoverageClassification,
    MAX_RECORD_BYTES,
    MAX_RECORDS_PER_SOURCE,
    MAX_TOTAL_BYTES,
    record_from_payload,
    EvaluationCorpusCoverageError,
    EvaluationCorpusInventory,
)

MAX_PERSISTED_INVENTORY_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class EvaluationCorpusSource:
    """A source declaration with an adapter-owned bounded reader."""

    spec: CorpusSourceSpec
    reader: Callable[..., Iterable[Mapping[str, Any]]]


class BoundedEvaluationCorpusScanner:
    """Scan all declared source classes without accepting partial coverage."""

    def __init__(
        self,
        sources: Iterable[EvaluationCorpusSource],
        *,
        max_records: int = MAX_RECORDS_PER_SOURCE,
        max_record_bytes: int = MAX_RECORD_BYTES,
        max_total_bytes: int = MAX_TOTAL_BYTES,
    ) -> None:
        self._sources = tuple(sources)
        if not 1 <= max_records <= MAX_RECORDS_PER_SOURCE:
            raise ValueError("max_records is outside the bounded range")
        if not 1 <= max_record_bytes <= MAX_RECORD_BYTES:
            raise ValueError("max_record_bytes is outside the bounded range")
        if not 1 <= max_total_bytes <= MAX_TOTAL_BYTES:
            raise ValueError("max_total_bytes is outside the bounded range")
        self._max_records = max_records
        self._max_record_bytes = max_record_bytes
        self._max_total_bytes = max_total_bytes

    def scan(self) -> tuple[CorpusSourceReport, ...]:
        reports: list[CorpusSourceReport] = []
        seen: set[str] = set()
        for source in self._sources:
            if source.spec.source_id in seen:
                reports.append(CorpusSourceReport(
                    source.spec.source_id, source.spec.kind,
                    CoverageClassification.DUPLICATE_SOURCE,
                    reason="duplicate source ID",
                ))
                continue
            seen.add(source.spec.source_id)
            reports.append(self._scan_one(source))
        return tuple(reports)

    def _scan_one(self, source: EvaluationCorpusSource) -> CorpusSourceReport:
        try:
            rows = source.reader(
                max_records=self._max_records + 1,
                max_record_bytes=self._max_record_bytes,
                max_total_bytes=self._max_total_bytes,
            )
            records = []
            total = 0
            for index, row in enumerate(rows):
                if index >= self._max_records:
                    return CorpusSourceReport(
                        source.spec.source_id, source.spec.kind,
                        CoverageClassification.SCAN_LIMIT,
                        tuple(records), _source_digest(records), total,
                        "source returned more records than max_records",
                    )
                record = record_from_payload(row, max_record_bytes=self._max_record_bytes)
                total += record.bytes_scanned
                if total > self._max_total_bytes:
                    return CorpusSourceReport(
                        source.spec.source_id, source.spec.kind,
                        CoverageClassification.SCAN_LIMIT,
                        tuple(records), _source_digest(records), total,
                        "source exceeded max_total_bytes",
                    )
                records.append(record)
            return CorpusSourceReport(
                source.spec.source_id, source.spec.kind,
                CoverageClassification.COMPLETE,
                tuple(records), _source_digest(records), total,
            )
        except (OSError, RuntimeError) as exc:
            return CorpusSourceReport(
                source.spec.source_id, source.spec.kind,
                CoverageClassification.READ_ERROR, reason=type(exc).__name__,
            )
        except (TypeError, ValueError) as exc:
            return CorpusSourceReport(
                source.spec.source_id, source.spec.kind,
                CoverageClassification.INVALID_SOURCE, reason=str(exc)[:256],
            )


class JsonEvaluationCorpusInventoryRepository:
    """Small durable adapter with atomic writes and digest-verified reads."""

    def __init__(self, path: str | Path, *, max_bytes: int = MAX_PERSISTED_INVENTORY_BYTES) -> None:
        self._path = Path(path)
        if not 1 <= max_bytes <= MAX_PERSISTED_INVENTORY_BYTES:
            raise ValueError("max_bytes is outside the bounded range")
        self._max_bytes = max_bytes

    def save(self, inventory: EvaluationCorpusInventory) -> None:
        encoded = json.dumps(inventory.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(encoded) > self._max_bytes:
            raise EvaluationCorpusCoverageError("persisted corpus inventory exceeds byte bound")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(self._path.name + ".tmp")
        temporary.write_bytes(encoded)
        temporary.replace(self._path)

    def load(self) -> EvaluationCorpusInventory:
        try:
            if self._path.stat().st_size > self._max_bytes:
                raise EvaluationCorpusCoverageError("persisted corpus inventory exceeds byte bound")
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            return EvaluationCorpusInventory.from_dict(payload)
        except EvaluationCorpusCoverageError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise EvaluationCorpusCoverageError("persisted corpus inventory is unreadable") from exc


def _source_digest(records: list[Any] | tuple[Any, ...]) -> str:
    material = [
        {"record_id": item.record_id, "digest": item.digest, "bytes_scanned": item.bytes_scanned}
        for item in records
    ]
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "BoundedEvaluationCorpusScanner", "EvaluationCorpusSource",
    "JsonEvaluationCorpusInventoryRepository", "MAX_PERSISTED_INVENTORY_BYTES",
]
