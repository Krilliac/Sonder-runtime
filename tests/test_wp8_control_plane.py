from dataclasses import FrozenInstanceError

import pytest

from sonder_runtime.application.control_plane.snapshot import (
    ControlPlaneSnapshot,
    SnapshotSection,
    SnapshotValidationError,
)


def test_snapshot_exposes_all_operator_domains_as_immutable_sections():
    snapshot = ControlPlaneSnapshot.build(
        captured_at="2026-08-20T12:00:00Z",
        revision=4,
        sessions=[{"id": "s1", "state": "active"}],
        health=[{"component": "runtime", "status": "ok"}],
        startup_authorities=[{"authority": "composition-root", "source": "config"}],
    )

    assert snapshot.sessions.records[0]["id"] == "s1"
    assert snapshot.health.count == 1
    assert snapshot.startup_authorities.records[0]["authority"] == "composition-root"
    assert snapshot.total_records == 3
    with pytest.raises(TypeError):
        snapshot.sessions.records[0]["state"] = "stopped"
    with pytest.raises(FrozenInstanceError):
        snapshot.revision = 5


def test_snapshot_is_canonical_and_digest_changes_when_state_changes():
    first = ControlPlaneSnapshot.build(
        captured_at="2026-08-20T12:00:00Z", revision=1,
        agents=[{"id": "a1", "labels": {"tier": "local"}}],
        model_hardware=[{"model": "30b-q4", "gpu": "5070-ti"}],
    )
    equivalent = ControlPlaneSnapshot.build(
        captured_at="2026-08-20T12:00:00Z", revision=1,
        model_hardware=[{"gpu": "5070-ti", "model": "30b-q4"}],
        agents=[{"labels": {"tier": "local"}, "id": "a1"}],
    )
    changed = ControlPlaneSnapshot.build(
        captured_at="2026-08-20T12:00:00Z", revision=2,
        agents=[{"id": "a1"}],
    )

    assert first.digest() == equivalent.digest()
    assert first.digest() != changed.digest()
    assert first.as_dict()["sections"]["extensions"]["records"] == []


def test_invalid_or_unknown_sections_are_rejected():
    with pytest.raises(SnapshotValidationError):
        SnapshotSection.from_records("health", [{"ok": True}, "bad"])
    with pytest.raises(SnapshotValidationError):
        ControlPlaneSnapshot.build(captured_at="now", unknown=[{}])
    with pytest.raises(SnapshotValidationError):
        ControlPlaneSnapshot.build(captured_at="now", revision=-1)
