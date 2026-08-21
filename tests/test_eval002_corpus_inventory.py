from __future__ import annotations

import pytest

from sonder_runtime.adapters.evaluation_corpus import (
    BoundedEvaluationCorpusScanner,
    EvaluationCorpusSource,
    JsonEvaluationCorpusInventoryRepository,
)
from sonder_runtime.application.evaluation.corpus_inventory import (
    CorpusSourceKind,
    CorpusSourceSpec,
    CoverageClassification,
    EvaluationCorpusCoverageError,
    build_inventory,
)


def _source(kind: CorpusSourceKind, source_id: str, rows):
    return EvaluationCorpusSource(CorpusSourceSpec(source_id, kind), lambda **_: rows)


def test_inventory_spans_required_repository_tool_and_memory_sources_with_digest():
    reports = BoundedEvaluationCorpusScanner([
        _source(CorpusSourceKind.REPOSITORY, "repo", [{"id": "repo-1", "task": "build"}]),
        _source(CorpusSourceKind.TOOL, "tools", [{"id": "tool-1", "name": "read"}]),
        _source(CorpusSourceKind.MEMORY, "memory", [{"id": "memory-1", "lesson": "ground"}]),
    ]).scan()
    inventory = build_inventory(reports)
    assert inventory.complete
    assert inventory.missing == ()
    assert len(inventory.digest) == 64
    assert inventory.digest == build_inventory(reports).digest
    assert {item["kind"] for item in inventory.as_dict()["reports"]} == {"repository", "tool", "memory"}


def test_missing_required_source_is_classified_and_fails_closed():
    reports = BoundedEvaluationCorpusScanner([
        _source(CorpusSourceKind.REPOSITORY, "repo", [{"id": "repo-1"}]),
        _source(CorpusSourceKind.TOOL, "tools", [{"id": "tool-1"}]),
    ]).scan()
    inventory = build_inventory(reports)
    assert CoverageClassification.MISSING_SOURCE in inventory.missing
    with pytest.raises(EvaluationCorpusCoverageError, match="incomplete"):
        inventory.require_complete()


def test_truncated_scan_is_not_reported_as_complete():
    rows = [{"id": f"r-{index}"} for index in range(3)]
    reports = BoundedEvaluationCorpusScanner([
        _source(CorpusSourceKind.REPOSITORY, "repo", rows),
        _source(CorpusSourceKind.TOOL, "tools", [{"id": "tool-1"}]),
        _source(CorpusSourceKind.MEMORY, "memory", [{"id": "memory-1"}]),
    ], max_records=2).scan()
    inventory = build_inventory(reports)
    repo = next(report for report in inventory.reports if report.kind is CorpusSourceKind.REPOSITORY)
    assert repo.classification is CoverageClassification.SCAN_LIMIT
    assert not inventory.complete


def test_invalid_record_and_reader_error_are_explicitly_fail_closed():
    def bad_reader(**_):
        return [{"not_id": "missing"}]

    def failing_reader(**_):
        raise OSError("source unavailable")

    reports = BoundedEvaluationCorpusScanner([
        EvaluationCorpusSource(CorpusSourceSpec("repo", CorpusSourceKind.REPOSITORY), bad_reader),
        EvaluationCorpusSource(CorpusSourceSpec("tools", CorpusSourceKind.TOOL), failing_reader),
        _source(CorpusSourceKind.MEMORY, "memory", [{"id": "memory-1"}]),
    ]).scan()
    assert {report.classification for report in reports} == {
        CoverageClassification.INVALID_SOURCE,
        CoverageClassification.READ_ERROR,
        CoverageClassification.COMPLETE,
    }
    assert not build_inventory(reports).complete


def test_duplicate_source_ids_cannot_hide_incomplete_coverage():
    reports = BoundedEvaluationCorpusScanner([
        _source(CorpusSourceKind.REPOSITORY, "same", [{"id": "repo-1"}]),
        _source(CorpusSourceKind.TOOL, "same", [{"id": "tool-1"}]),
        _source(CorpusSourceKind.MEMORY, "memory", [{"id": "memory-1"}]),
    ]).scan()
    inventory = build_inventory(reports)
    assert any(report.classification is CoverageClassification.DUPLICATE_SOURCE for report in reports)
    assert not inventory.complete


def test_record_digest_changes_when_source_content_changes():
    def scan(rows):
        return build_inventory(BoundedEvaluationCorpusScanner([
            _source(CorpusSourceKind.REPOSITORY, "repo", rows),
            _source(CorpusSourceKind.TOOL, "tools", [{"id": "tool-1"}]),
            _source(CorpusSourceKind.MEMORY, "memory", [{"id": "memory-1"}]),
        ]).scan())

    assert scan([{"id": "repo-1", "value": "a"}]).digest != scan([{"id": "repo-1", "value": "b"}]).digest


def test_durable_json_round_trip_verifies_digest(tmp_path):
    reports = BoundedEvaluationCorpusScanner([
        _source(CorpusSourceKind.REPOSITORY, "repo", [{"id": "repo-1"}]),
        _source(CorpusSourceKind.TOOL, "tools", [{"id": "tool-1"}]),
        _source(CorpusSourceKind.MEMORY, "memory", [{"id": "memory-1"}]),
    ]).scan()
    inventory = build_inventory(reports)
    repository = JsonEvaluationCorpusInventoryRepository(tmp_path / "inventory.json")
    repository.save(inventory)
    assert repository.load().digest == inventory.digest

    payload = (tmp_path / "inventory.json").read_text(encoding="utf-8").replace(inventory.digest, "0" * 64)
    (tmp_path / "inventory.json").write_text(payload, encoding="utf-8")
    with pytest.raises(EvaluationCorpusCoverageError, match="digest mismatch"):
        repository.load()
