import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts import release_artifacts as release


def _zip(path: Path, *, license_file: bool = True, nested: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        if nested:
            payload = io.BytesIO()
            with zipfile.ZipFile(payload, "w") as local_system:
                if license_file:
                    local_system.writestr("local-system/LICENSE", "Apache 2.0\n")
            archive.writestr("assets/local-system.zip", payload.getvalue())
        elif license_file:
            archive.writestr("bundle/local-system/LICENSE", "Apache 2.0\n")
        archive.writestr("payload.bin", b"payload")


def _artifacts(root: Path) -> None:
    _zip(root / "android" / "sonder-runtime-android.apk", nested=True)
    _zip(root / "windows" / "sonder-runtime-windows-x64.zip")
    _zip(root / "macos" / "sonder-runtime-macos.zip")
    linux = root / "linux" / "sonder-runtime-linux-x64.tar.gz"
    linux.parent.mkdir(parents=True)
    data = b"Apache 2.0\n"
    info = tarfile.TarInfo("local-system/LICENSE")
    info.size = len(data)
    with tarfile.open(linux, "w:gz") as archive:
        archive.addfile(info, io.BytesIO(data))


def test_generates_checksums_sbom_and_provenance(tmp_path):
    _artifacts(tmp_path)
    revision = "b" * 40
    outputs = release.generate_metadata(
        tmp_path,
        version="1.2.3",
        revision=revision,
        source_uri="https://github.com/Krilliac/Sonder-runtime",
        workflow_uri="https://github.com/Krilliac/Sonder-runtime/actions/runs/42",
        invocation_id="42-1",
        created="2026-08-08T12:00:00Z",
    )

    assert set(outputs) == set(release.OUTPUTS)
    checksums = (tmp_path / "SHA256SUMS").read_text(encoding="ascii").splitlines()
    assert len(checksums) == len(release.EXPECTED_ARTIFACTS) + 2
    for line in checksums:
        digest, name = line.split("  ", 1)
        matches = list(tmp_path.rglob(name))
        assert len(matches) == 1
        assert hashlib.sha256(matches[0].read_bytes()).hexdigest() == digest

    sbom = json.loads((tmp_path / release.OUTPUTS[0]).read_text(encoding="utf-8"))
    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["metadata"]["component"]["version"] == "1.2.3"
    assert {item["name"] for item in sbom["components"]} == set(
        release.EXPECTED_ARTIFACTS
    )
    provenance = json.loads(
        (tmp_path / release.OUTPUTS[1]).read_text(encoding="utf-8")
    )
    assert provenance["subject"][0]["digest"]["sha256"]
    assert (
        provenance["predicate"]["buildDefinition"]["internalParameters"]["revision"]
        == revision
    )


def test_fails_closed_when_artifact_is_missing(tmp_path):
    _artifacts(tmp_path)
    (tmp_path / "macos" / "sonder-runtime-macos.zip").unlink()
    with pytest.raises(ValueError, match="required artifact.*found 0 times"):
        release.generate_metadata(
            tmp_path,
            version="1.2.3",
            revision="c" * 40,
            source_uri="source",
            workflow_uri="workflow",
            invocation_id="run",
        )
    assert not any((tmp_path / name).exists() for name in release.OUTPUTS)


def test_fails_closed_when_license_is_missing(tmp_path):
    _artifacts(tmp_path)
    _zip(tmp_path / "windows" / "sonder-runtime-windows-x64.zip", license_file=False)
    with pytest.raises(ValueError, match="LICENSE is missing"):
        release.discover_artifacts(tmp_path)


def test_rejects_noncanonical_revision(tmp_path):
    _artifacts(tmp_path)
    with pytest.raises(ValueError, match="full 40-character"):
        release.generate_metadata(
            tmp_path,
            version="1.2.3",
            revision="deadbeef",
            source_uri="source",
            workflow_uri="workflow",
            invocation_id="run",
        )


def test_release_workflow_stamps_and_gates_artifacts():
    workflow = (
        Path(__file__).resolve().parents[1] / ".github/workflows/build-apps.yml"
    ).read_text(encoding="utf-8")
    assert "SONDER_BUILD_REVISION: ${{ github.sha }}" in workflow
    assert "integrity:\n    needs: [android, linux, windows, macos]" in workflow
    release_block = workflow.split("\n  release:\n", 1)[1]
    assert "needs: [integrity]" in release_block
    assert (
        "scripts/check_release_version.py --require-release --json" in release_block
    )
    assert release_block.index("--require-release") < release_block.index(
        "Publish release"
    )
    assert "scripts/release_artifacts.py dist" in workflow
    assert "fail_on_unmatched_files: true" in release_block
    for artifact in release.EXPECTED_ARTIFACTS:
        assert artifact in release_block
    for output in release.OUTPUTS:
        assert f"dist/{output}" in workflow
        assert output in release_block
