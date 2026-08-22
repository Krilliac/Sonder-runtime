from sonder_runtime.adapters import backup
from sonder_runtime.adapters.web import lifecycle
from sonder_runtime.platform import version


def test_backup_consumes_the_packaged_version_boundary():
    assert backup.sonder_version is version
    assert backup.sonder_version.VERSION == version.VERSION
    assert backup.sonder_version.BuildInfo is version.BuildInfo
    assert backup.sonder_version.build_info is version.build_info


def test_packaged_version_boundary_preserves_root_build_identity():
    import sonder_version

    assert version.VERSION is sonder_version.VERSION
    assert version.BuildInfo is sonder_version.BuildInfo
    assert version.build_info is sonder_version.build_info


def test_lifecycle_consumes_the_packaged_version_boundary():
    assert lifecycle.sonder_version is version
    assert lifecycle.sonder_version.VERSION is version.VERSION
    assert lifecycle.sonder_version.BuildInfo is version.BuildInfo
    assert lifecycle.sonder_version.build_info is version.build_info


def test_lifecycle_preserves_packaged_build_identity(monkeypatch):
    expected = version.BuildInfo(
        version="9.8.7",
        commit_sha="abc123",
        stamped=True,
    )
    monkeypatch.setattr(version, "build_info", lambda: expected)

    runtime = lifecycle.RuntimeLifecycle(
        max_concurrent_requests=1,
        queue_depth=1,
    )

    assert runtime._build is expected
    assert runtime.version_payload()["version"] == expected.version
