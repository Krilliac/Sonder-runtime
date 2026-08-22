from sonder_runtime.application.control_plane import (
    CONTROL_PLANE_SECTIONS,
    ControlPlaneSnapshotService,
)
from sonder_runtime.bootstrap import app as bootstrap_app


def test_composition_root_preserves_explicit_control_plane_service(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_STATE_HOME", str(tmp_path))
    service = ControlPlaneSnapshotService(
        {name: (lambda name=name: ({"section": name},)) for name in CONTROL_PLANE_SECTIONS}
    )

    bootstrap_app.reset_for_tests()
    application = bootstrap_app.build_application(
        control_plane_snapshot_service=service,
    )

    assert application.control_plane_snapshot_service is service


def test_default_composition_exposes_real_and_explicitly_unavailable_sections(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("SONDER_STATE_HOME", str(tmp_path))
    bootstrap_app.reset_for_tests()
    application = bootstrap_app.build_application()

    snapshot = application.control_plane_snapshot_service.snapshot(captured_at="now")
    payload = snapshot.as_dict()
    assert payload["sections"]["sessions"]["records"] == []
    assert payload["sections"]["plans"]["records"] == []
    assert payload["sections"]["context"]["records"] == [{
        "available": False,
        "reason": "owning application port is not composed",
        "section": "context",
    }]
    assert payload["sections"]["memory_explanations"]["count"] == 8
    assert "content" not in str(payload["sections"]["memory_explanations"])
    assert payload["sections"]["health"]["count"] >= 1
