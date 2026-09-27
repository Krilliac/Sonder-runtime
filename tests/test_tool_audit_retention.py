"""Rotated tool-audit chains have an aggregate count and byte bound.

Per-file bounds alone let sustained tool traffic grow the audit directory
without limit.  With pruning (the default) the oldest rotated chains are
deleted and the deletion is named in the next chain's first record; with
pruning off, a full retention quota fails the call closed instead.
"""
from __future__ import annotations

import pytest

from sonder_runtime.adapters.persistence.tool_audit import (
    DurableToolAuditRepository,
    ToolAuditLimits,
)
from sonder_runtime.application.tools.audit import ToolAuditError
from sonder_runtime.application.tools.gateway_contract import (
    ApprovalMode,
    ToolGatewayRequest,
    ToolPermission,
    ToolReceipt,
    ToolScope,
)


def _request(index):
    return ToolGatewayRequest(
        request_id="request-%d" % index, tool_name="read", arguments={"path": "x"},
        scope=ToolScope("owner", ("project-root",), frozenset({"read"})),
        permission=ToolPermission(frozenset({"read"}), ApprovalMode.NOT_REQUIRED),
        session_id="session-1", project_id="project-1",
    )


def _receipt(index):
    return ToolReceipt(
        request_id="request-%d" % index, tool_name="read", success=True,
        output={"value": index},
    )


def _append(repository, count, start=0):
    for index in range(start, start + count):
        repository.append(_request(index), _receipt(index))


def test_rotated_chain_count_is_bounded_by_pruning_the_oldest(tmp_path):
    repository = DurableToolAuditRepository(
        tmp_path / "audit.jsonl",
        limits=ToolAuditLimits(max_records=1, max_rotated_files=2),
    )
    _append(repository, 8)

    rotated = repository.rotated_files()
    assert len(rotated) == 2
    # The active chain's first record names what retention deleted.
    first = repository.read()[0]
    assert first["rotated_from"]["pruned"]
    # The survivors are the newest chains: the most recent requests.
    kept = sorted(p.read_text(encoding="utf-8") for p in rotated)
    assert any("request-6" in text for text in kept)
    assert not any("request-0" in text for text in kept)


def test_rotated_bytes_are_bounded(tmp_path):
    probe = DurableToolAuditRepository(tmp_path / "probe.jsonl")
    _append(probe, 1)
    record_bytes = probe.path.stat().st_size

    repository = DurableToolAuditRepository(
        tmp_path / "audit.jsonl",
        limits=ToolAuditLimits(
            max_records=1, max_bytes=record_bytes * 4,
            max_rotated_files=100, max_rotated_bytes=record_bytes * 4,
        ),
    )
    _append(repository, 12)
    total = sum(p.stat().st_size for p in repository.rotated_files())
    assert total <= record_bytes * 4 + 64


def test_retention_quota_fails_closed_when_pruning_is_disabled(tmp_path):
    repository = DurableToolAuditRepository(
        tmp_path / "audit.jsonl",
        limits=ToolAuditLimits(max_records=1, max_rotated_files=2, prune_rotated=False),
    )
    _append(repository, 3)  # two rotations: quota now full
    before = repository.rotated_files()
    assert len(before) == 2
    with pytest.raises(ToolAuditError, match="retention"):
        _append(repository, 1, start=3)
    assert repository.rotated_files() == before
    repository.verify()


def test_retention_ignores_files_it_did_not_rotate(tmp_path):
    foreign = tmp_path / "audit.operator-notes.jsonl"
    foreign.write_text("keep me\n", encoding="utf-8")
    repository = DurableToolAuditRepository(
        tmp_path / "audit.jsonl",
        limits=ToolAuditLimits(max_records=1, max_rotated_files=1),
    )
    _append(repository, 5)
    assert foreign.read_text(encoding="utf-8") == "keep me\n"


@pytest.mark.parametrize("limits", [
    dict(max_rotated_files=0),
    dict(max_bytes=4096, max_rotated_bytes=1024),
])
def test_retention_limits_must_be_usable(tmp_path, limits):
    with pytest.raises(ValueError):
        DurableToolAuditRepository(tmp_path / "a.jsonl", limits=ToolAuditLimits(**limits))
