from pathlib import Path

import pytest

from sonder_runtime.adapters.updates import engine
from sonder_runtime.platform import version


def test_update_engine_reads_build_identity_through_packaged_boundary():
    assert engine.sonder_version is version
    assert engine.sonder_version.VERSION is version.VERSION
    assert engine.sonder_version.BuildInfo is version.BuildInfo
    assert engine.sonder_version.build_info is version.build_info


def test_update_status_preserves_build_metadata(monkeypatch):
    expected = version.BuildInfo(
        version="9.8.7",
        commit_sha="abc123",
        stamped=True,
    )
    monkeypatch.setattr(version, "build_info", lambda: expected)
    manager = engine.UpdateManager(repository=_StatusRepository())

    status = manager.status()

    assert status["running_version"] == expected.version
    assert status["running_commit"] == expected.commit_sha


class _StatusRepository:
    def release_by_status(self, _status):
        return None

    def list_plans(self, **_kwargs):
        return []

    def accepted_versions(self):
        return ()


def test_installer_bootstrap_pointer_is_allowed_before_managed_activation(monkeypatch):
    manager = object.__new__(engine.UpdateManager)
    manager.repository = _StatusRepository()
    manager.releases_dir = Path("/opt/sonder/releases")
    manager.current_link = Path("/opt/sonder/current")
    monkeypatch.setattr(engine.sonder_updates, "_read_pointer", lambda _link: "/opt/sonder/releases/bootstrap")

    manager._assert_activation_consistent()


def test_development_release_orders_before_final_release():
    assert engine._release_order("0.9.0.dev0") < engine._release_order("0.9.0")
    assert engine._release_order("0.9.0.dev0") < engine._release_order("0.9.0.dev1")
    assert engine._release_order("0.9.0rc1.dev0") < engine._release_order("0.9.0rc1")


def test_managed_release_still_requires_matching_pointer(monkeypatch):
    manager = object.__new__(engine.UpdateManager)
    manager.repository = _StatusRepository()
    manager.repository.release_by_status = lambda _status: {"install_path": "/opt/sonder/releases/managed"}
    manager.current_link = Path("/opt/sonder/current")
    monkeypatch.setattr(engine.sonder_updates, "_read_pointer", lambda _link: "/opt/sonder/releases/other")

    with pytest.raises(engine.UpdateError, match="disagree"):
        manager._assert_activation_consistent()
