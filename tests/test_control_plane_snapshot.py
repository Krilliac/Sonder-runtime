from __future__ import annotations

import pytest

from sonder_runtime.application.control_plane.snapshot import (
    ControlPlaneSnapshot,
    SnapshotSection,
    SnapshotValidationError,
)


def test_control_plane_snapshot_is_complete_immutable_and_digest_bound():
    snapshot = ControlPlaneSnapshot.build(
        captured_at="2026-08-21T12:00:00Z",
        revision=7,
        sessions=[{"id": "session-1", "status": "active"}],
        plans=[{"id": "plan-1", "status": "in_progress"}],
        approvals=[{"id": "approval-1", "state": "pending"}],
        health=[{"ready": True}],
    )

    data = snapshot.as_dict()
    assert data["revision"] == 7
    assert data["sections"]["sessions"]["count"] == 1
    assert snapshot.total_records == 4
    assert snapshot.digest() == ControlPlaneSnapshot.build(
        captured_at="2026-08-21T12:00:00Z", revision=7,
        sessions=[{"status": "active", "id": "session-1"}],
        plans=[{"status": "in_progress", "id": "plan-1"}],
        approvals=[{"state": "pending", "id": "approval-1"}],
        health=[{"ready": True}],
    ).digest()
    with pytest.raises(TypeError):
        snapshot.sessions.records[0]["status"] = "done"


def test_control_plane_snapshot_rejects_unknown_sections_and_mismatched_names():
    with pytest.raises(SnapshotValidationError, match="unknown sections"):
        ControlPlaneSnapshot.build(captured_at="now", unexpected=[])
    with pytest.raises(SnapshotValidationError, match="matching"):
        ControlPlaneSnapshot(
            captured_at="now", revision=0,
            sessions=SnapshotSection("wrong"),
            **{name: SnapshotSection(name) for name in (
                "plans", "approvals", "jobs", "agents", "model_hardware",
                "context", "memory_explanations", "extensions", "training",
                "selfmod", "updates", "health", "startup_authorities",
            )},
        )


def test_control_plane_snapshot_freezes_nested_records_and_rejects_bad_revision():
    with pytest.raises(SnapshotValidationError, match="revision"):
        ControlPlaneSnapshot.build(captured_at="now", revision=-1)
    snapshot = ControlPlaneSnapshot.build(
        captured_at="now", health=[{"nested": {"value": 1}}],
    )
    with pytest.raises(TypeError):
        snapshot.health.records[0]["nested"]["value"] = 2
