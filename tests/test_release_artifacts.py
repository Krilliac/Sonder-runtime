import hashlib
import io
import json
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts import release_artifacts as release

REVISION = "b" * 40


def _system_files(*, license_file: bool = True,
                  build_stamp_padding: int = 0) -> dict[str, bytes]:
    files = {
        "server.py": b"print('ready')\n",
        "bootstrap-engine.sh": b"#!/bin/sh\n",
        "bootstrap-engine.cmd": b"@echo off\r\n",
        "requirements-runtime.txt": b"# stdlib\n",
        "sonder_build.json": json.dumps({"version": "1.2.3", "commit_sha": REVISION}).encode()
        + b" " * build_stamp_padding,
    }
    if license_file:
        files["LICENSE"] = b"Apache 2.0\n"
    manifest = {
        "schema": 1,
        "files": [
            {"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "mode": 0o644}
            for name, data in sorted(files.items())
        ],
    }
    files["PACKAGE-MANIFEST.json"] = json.dumps(manifest).encode()
    return files


def _nested_system(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, data in files.items():
            archive.writestr("local-system/" + name, data)
    return output.getvalue()


def _zip(path: Path, *, license_file: bool = True, app_file: bool = True,
         invalid_nested: bool = False, build_stamp_padding: int = 0) -> None:
    files = _system_files(license_file=license_file, build_stamp_padding=build_stamp_padding)
    nested = b"invalid nested ZIP" if invalid_nested else _nested_system(files)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        if path.name.endswith(".apk"):
            archive.writestr("AndroidManifest.xml", b"manifest")
            archive.writestr("classes.dex", b"dex")
            if app_file:
                archive.writestr("lib/arm64-v8a/libapp.so", b"app")
            archive.writestr("assets/flutter_assets/assets/local-system.zip", nested)
        elif "windows" in path.name:
            if app_file:
                archive.writestr("sonder.exe", b"app")
            archive.writestr("flutter_windows.dll", b"flutter")
            archive.writestr("data/flutter_assets/assets/local-system.zip", nested)
            for name, data in files.items():
                archive.writestr("local-system/" + name, data)
        else:
            prefix = release.MACOS_APP_BUNDLE + "/Contents/"
            if app_file:
                archive.writestr(prefix + "MacOS/sonder", b"app")
            archive.writestr(prefix + "Info.plist", b"plist")
            archive.writestr(prefix + "Frameworks/App.framework/Versions/A/App", b"framework")
            link = zipfile.ZipInfo(prefix + "Frameworks/App.framework/App")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, "Versions/Current/App")
            current = zipfile.ZipInfo(prefix + "Frameworks/App.framework/Versions/Current")
            current.create_system = 3
            current.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(current, "A")
            archive.writestr(prefix + "Frameworks/App.framework/Versions/A/Resources/flutter_assets/assets/local-system.zip", nested)
            for name, data in files.items():
                archive.writestr(prefix + "Resources/local-system/" + name, data)


def _artifacts(root: Path) -> None:
    _zip(root / "android" / "sonder-runtime-android.apk")
    _zip(root / "windows" / "sonder-runtime-windows-x64.zip")
    _zip(root / "macos" / "sonder-runtime-macos.zip")
    linux = root / "linux" / "sonder-runtime-linux-x64.tar.gz"
    linux.parent.mkdir(parents=True)
    with tarfile.open(linux, "w:gz") as archive:
        payload = _system_files()
        payload.update({"sonder": b"app", "lib/libapp.so": b"flutter", "data/flutter_assets/assets/local-system.zip": _nested_system(_system_files())})
        for name, data in payload.items():
            name = name if not name.startswith(("server.py", "LICENSE", "bootstrap-engine", "requirements-runtime", "sonder_build.json", "PACKAGE-MANIFEST.json")) else "local-system/" + name
            info = tarfile.TarInfo("./" + name)
            info.size = len(data)
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
    assert provenance["predicate"]["buildDefinition"]["buildType"] == (
        "https://github.com/Krilliac/Sonder-runtime/"
        ".github/workflows/build-apps.yml@" + revision
    )


def test_fails_closed_when_artifact_is_missing(tmp_path):
    _artifacts(tmp_path)
    (tmp_path / "macos" / "sonder-runtime-macos.zip").unlink()
    with pytest.raises(ValueError, match="required artifact.*found 0 times"):
        release.generate_metadata(
            tmp_path,
            version="1.2.3",
            revision=REVISION,
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


def test_rejects_outer_license_that_hides_corrupt_android_payload(tmp_path):
    _artifacts(tmp_path)
    path = tmp_path / "android" / "sonder-runtime-android.apk"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("LICENSE", "decoy outer license")
        archive.writestr("AndroidManifest.xml", b"manifest")
        archive.writestr("classes.dex", b"dex")
        archive.writestr("lib/arm64-v8a/libapp.so", b"app")
        archive.writestr("assets/flutter_assets/assets/local-system.zip", b"invalid nested ZIP")
    with pytest.raises(ValueError, match="local-system.zip"):
        release.discover_artifacts(tmp_path)


@pytest.mark.parametrize("platform,name,missing", [
    ("android", "sonder-runtime-android.apk", "libapp.so"),
    ("windows", "sonder-runtime-windows-x64.zip", "sonder.exe"),
    ("macos", "sonder-runtime-macos.zip", "MacOS/sonder"),
])
def test_rejects_archive_without_application_binary_despite_license(tmp_path, platform, name, missing):
    _artifacts(tmp_path)
    _zip(tmp_path / platform / name, app_file=False)
    with pytest.raises(ValueError, match=missing):
        release.discover_artifacts(tmp_path)


def test_rejects_mac_framework_symlink_escape(tmp_path):
    _artifacts(tmp_path)
    path = tmp_path / "macos" / "sonder-runtime-macos.zip"
    with zipfile.ZipFile(path, "a") as archive:
        link = zipfile.ZipInfo(release.MACOS_APP_BUNDLE + "/Contents/Frameworks/escape")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "../../../../outside")
    with pytest.raises(ValueError, match="symlink escapes"):
        release.discover_artifacts(tmp_path)


def test_rejects_linux_tar_without_app_executable(tmp_path):
    _artifacts(tmp_path)
    path = tmp_path / "linux" / "sonder-runtime-linux-x64.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        data = b"Apache 2.0\n"
        info = tarfile.TarInfo("./local-system/LICENSE")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    with pytest.raises(ValueError, match="sonder"):
        release.discover_artifacts(tmp_path)


def test_rejects_truncated_linux_gzip_trailer(tmp_path):
    _artifacts(tmp_path)
    path = tmp_path / "linux" / "sonder-runtime-linux-x64.tar.gz"
    path.write_bytes(path.read_bytes()[:-8])
    with pytest.raises(ValueError, match="release archive"):
        release.discover_artifacts(tmp_path)


def test_rejects_local_system_manifest_mismatch(tmp_path):
    _artifacts(tmp_path)
    path = tmp_path / "android" / "sonder-runtime-android.apk"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"manifest")
        archive.writestr("classes.dex", b"dex")
        archive.writestr("lib/arm64-v8a/libapp.so", b"app")
        files = _system_files()
        files["server.py"] = b"tampered"
        archive.writestr("assets/flutter_assets/assets/local-system.zip", _nested_system(files))
    with pytest.raises(ValueError, match="server.py"):
        release.discover_artifacts(tmp_path)


def test_rejects_oversized_build_stamp_before_parsing(tmp_path):
    _artifacts(tmp_path)
    _zip(tmp_path / "android" / "sonder-runtime-android.apk",
         build_stamp_padding=release.MAX_BUILD_STAMP_BYTES)
    with pytest.raises(ValueError, match="build identity exceeds size bound"):
        release.discover_artifacts(tmp_path, version="1.2.3", revision=REVISION)


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
    assert "python-gate:" in workflow
    assert "uses: ./.github/workflows/ci.yml" in workflow
    assert "managed-runtime-profile-gate:" in workflow
    assert "uses: ./.github/workflows/managed-runtime-profile.yml" in workflow
    release_block = workflow.split("\n  release:\n", 1)[1]
    assert "needs: [integrity, analyze, python-gate, managed-runtime-profile-gate]" in release_block
    managed = (
        Path(__file__).resolve().parents[1] / ".github/workflows/managed-runtime-profile.yml"
    ).read_text(encoding="utf-8")
    assert "workflow_call:" in managed
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

    ci = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "workflow_call:" in ci
    assert "Run tag-time runtime smoke" in ci
    assert "scripts/release_smoke.sh --tag" in ci
    assert "  windows-focused:\n    runs-on: windows-latest" in ci
    # The root-only Linux selfmod candidate boundary (#517) is part of the
    # same required gate: its job runs the canaries under sudo, refuses any
    # skip, and the "tests" context fails unless it succeeded.
    assert "needs: [windows-focused, container-qualification, linux-selfmod-isolation]" in ci
    assert "needs.linux-selfmod-isolation.result != 'success'" in ci
    linux_isolation = ci.split("\n  linux-selfmod-isolation:\n", 1)[1].split("\n  windows-focused:\n", 1)[0]
    assert 'sudo "$python_bin" -B -m pytest' in linux_isolation
    assert "tests/test_linux_candidate_isolation.py" in linux_isolation
    assert "tests/test_wiring_selfmod_linux_nightly.py" in linux_isolation
    assert '("skipped", "errors", "failures")' in linux_isolation
    assert "python -m venv" in ci
    assert "tests/test_managed_runtime_payload.py" in ci
    assert "tests/test_managed_runtime_owner.py" in ci
    assert "tests/test_artifact_fetch.py" in ci
    assert "tests/test_selfmod_low_integrity.py" in ci


WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _job_block(workflow: str, job: str) -> str:
    """The text of one top-level job, up to the next job or the end."""
    marker = "\n  %s:\n" % job
    assert marker in workflow, job
    rest = workflow.split(marker, 1)[1]
    lines = []
    for line in rest.splitlines():
        if line.startswith("  ") and not line.startswith("   ") and line.rstrip().endswith(":"):
            break
        lines.append(line)
    return "\n".join(lines)


def _step_block(job_text: str, name: str) -> str:
    """The text of one ``- name:`` step inside a job, up to the next step."""
    marker = "      - name: %s\n" % name
    assert marker in job_text, name
    rest = job_text.split(marker, 1)[1]
    return rest.split("\n      - ", 1)[0]


def test_ci_runs_the_tuf_update_trust_suites_and_refuses_skips():
    ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    step = _step_block(_job_block(ci, "tests"), "Run the TUF update-trust suites (no skips allowed)")
    assert "if: ${{ !cancelled() }}" in step
    assert "uv pip install -r requirements-dev.txt -r requirements-update.txt --system" in step
    assert "tests/production/test_tuf_publisher.py" in step
    assert "tests/test_update_manifest_trust.py" in step
    assert '--junitxml="$RUNNER_TEMP/update-trust.xml"' in step
    assert 'assert totals["tests"] > 0' in step
    assert 'assert totals["skipped"] == totals["errors"] == totals["failures"] == 0' in step
    # The trust suites must stay importorskip-guarded on tuf only: the default
    # dev install leaves the TUF stack out, and this step is what runs them.
    assert "tuf" not in (Path(__file__).resolve().parents[1] / "requirements-dev.txt").read_text(
        encoding="utf-8")


WINDOWS_DEVTOOLS_SUITES = (
    "tests/test_build_environment_windows_fakes.py",
    "tests/test_debug_windows_fakes.py",
    "tests/test_host_tool_discovery_windows.py",
    "tests/test_slash_menu_windows_keys.py",
    "tests/test_build_executor.py",
    "tests/test_build_tree_reader.py",
    "tests/test_build_preimages.py",
    "tests/test_debug_launcher.py",
    "tests/test_debug_templates_symbol_path.py",
    "tests/test_build_clang_cl_real.py",
)


def test_windows_focused_runs_the_devtools_suites_in_their_own_step():
    ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    job = _job_block(ci, "windows-focused")
    step = _step_block(job, "Exercise the build, debug and host-tool Windows branches")
    assert "if: ${{ !cancelled() && steps.install-focused-venv.outcome == 'success' }}" in step
    assert "timeout-minutes: 8" in step
    assert "continue-on-error" not in step, "the devtools step gates the job"
    assert "-m pytest -q -rs" in step
    for suite in WINDOWS_DEVTOOLS_SUITES:
        assert suite in step, suite
        assert (WORKFLOWS.parents[1] / suite).is_file(), suite
