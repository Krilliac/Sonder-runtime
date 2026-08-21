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
