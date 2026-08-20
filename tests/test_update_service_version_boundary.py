from sonder_runtime.adapters.updates import service
from sonder_runtime.platform import version


def test_update_service_reads_build_identity_through_packaged_boundary():
    assert service.sonder_version is version
    assert service.sonder_version.VERSION is version.VERSION
    assert service.sonder_version.BuildInfo is version.BuildInfo
    assert service.sonder_version.build_info is version.build_info


def test_bundle_build_uses_packaged_build_identity(monkeypatch, tmp_path):
    expected = version.BuildInfo(
        version="9.8.7",
        commit_sha="abc123",
        stamped=True,
    )
    monkeypatch.setattr(version, "build_info", lambda: expected)

    source = tmp_path / "source"
    source.mkdir()
    (source / "payload.txt").write_text("payload", encoding="utf-8")

    result = service.build_bundle(source, tmp_path / "out")

    assert result["manifest"]
    assert service.BundleManifest.load(result["manifest"])["version"] == expected.version
