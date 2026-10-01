"""Rotated tool-audit chains have an aggregate count and byte bound.

Per-file bounds alone let sustained tool traffic grow the audit directory
without limit.  With pruning (the default) the oldest rotated chains are
deleted and the deletion is named in the next chain's first record; with
pruning off, a full retention quota fails the call closed instead.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sonder_runtime.adapters.persistence import tool_audit
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


def test_pruning_does_not_delete_evidence_when_continuation_write_fails(
    tmp_path, monkeypatch,
):
    repository = DurableToolAuditRepository(
        tmp_path / "audit.jsonl",
        limits=ToolAuditLimits(max_records=1, max_rotated_files=1),
    )
    _append(repository, 2)
    oldest = repository.rotated_files()[0]

    def fail_continuation(path):
        raise OSError("simulated write failure")

    monkeypatch.setattr(tool_audit, "prepare_private_file", fail_continuation)
    with pytest.raises(OSError, match="simulated write failure"):
        _append(repository, 1, start=2)

    assert "request-0" in oldest.read_text(encoding="utf-8")


def test_pruning_marker_exists_before_a_chain_is_deleted(tmp_path, monkeypatch):
    repository = DurableToolAuditRepository(
        tmp_path / "audit.jsonl",
        limits=ToolAuditLimits(max_records=1, max_rotated_files=1),
    )
    _append(repository, 2)
    oldest = repository.rotated_files()[0]
    original_unlink = Path.unlink
    observed = []

    def check_marker(path, *args, **kwargs):
        if path == oldest:
            continuation = json.loads(repository.path.read_text(encoding="utf-8"))["rotated_from"]
            assert oldest.name in continuation["pruned"]
            observed.append(True)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", check_marker)
    _append(repository, 1, start=2)

    assert observed == [True]
    assert not oldest.exists()


def _freeze_rotation_clock(monkeypatch, stamp="20260930T190000Z"):
    """Every rotation in the test lands in the same UTC second."""
    monkeypatch.setattr(tool_audit.time, "strftime", lambda fmt, t=None: stamp)


def test_pruning_order_survives_same_second_rotations_and_equal_mtimes(tmp_path, monkeypatch):
    """Rotations inside one second are `audit.<stamp>.jsonl`, `.1`, `.2`, ...;
    on a coarse file clock (Windows ticks every ~15 ms) their mtimes tie too.
    Retention must still prune the OLDEST chain, never a newer one."""
    _freeze_rotation_clock(monkeypatch)
    repository = DurableToolAuditRepository(
        tmp_path / "audit.jsonl",
        limits=ToolAuditLimits(max_records=1, max_rotated_files=2),
    )
    for index in range(8):
        for path in tmp_path.glob("audit*.jsonl"):
            os.utime(path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        repository.append(_request(index), _receipt(index))

    kept = [p.read_text(encoding="utf-8") for p in repository.rotated_files()]
    assert len(kept) == 2
    assert "request-5" in kept[0] and "request-6" in kept[1]
    assert not any("request-0" in text for text in kept)


def test_rotated_files_are_ordered_by_rotation_not_by_name_text(tmp_path):
    repository = DurableToolAuditRepository(tmp_path / "audit.jsonl")
    stamp = "20260930T190000Z"
    names = ["audit.%s.jsonl" % stamp] + ["audit.%s.%d.jsonl" % (stamp, i) for i in range(1, 12)]
    names.append("audit.20260930T190001Z.jsonl")
    for name in reversed(names):  # creation order is deliberately backwards
        (tmp_path / name).write_text(name + "\n", encoding="utf-8")
    assert [p.name for p in repository.rotated_files()] == names


def test_rotation_never_reuses_a_pruned_name_within_one_second(tmp_path, monkeypatch):
    """A freed lower index must not be handed to a newer chain, or the name
    order stops meaning the rotation order."""
    _freeze_rotation_clock(monkeypatch)
    repository = DurableToolAuditRepository(
        tmp_path / "audit.jsonl",
        limits=ToolAuditLimits(max_records=1, max_rotated_files=2),
    )
    _append(repository, 6)
    assert [p.name for p in repository.rotated_files()] == [
        "audit.20260930T190000Z.3.jsonl", "audit.20260930T190000Z.4.jsonl",
    ]


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
