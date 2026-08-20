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
