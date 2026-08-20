import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import package_local_system as package


def _fake_repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    tracked = []
    for rel in sorted(package.REQUIRED_FILES | {"README.md", "tests/test_demo.py"}):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"safe content for {rel}\n", encoding="utf-8")
        tracked.append(path)
    for rel, text in {
        "file_roots.local": "D:\\private\n",
        "Modelfile.personal": "FROM C:\\Users\\private\\model\n",
        "system_profile.md": "private instructions\n",
        "memory.db": "not really sqlite\n",
        ".vs/state.txt": "private IDE state\n",
    }.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        tracked.append(path)
    monkeypatch.setattr(package, "ROOT", root)
    monkeypatch.setattr(package, "_tracked_files", lambda: tracked)
    return root


def test_rejects_destructive_destinations_and_preserves_repo(monkeypatch, tmp_path):
    root = _fake_repo(tmp_path, monkeypatch)
    sentinel = root / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    for unsafe in (
        root,
        root.parent,
        root / ".git",
        root / "scripts",
        root / "dist" / "other",
        root / "app" / "build" / "other",
    ):
        with pytest.raises(ValueError):
            package.copy_payload(unsafe)
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_payload_is_manifested_and_excludes_private_state(monkeypatch, tmp_path):
    root = _fake_repo(tmp_path, monkeypatch)
    dest = root / "dist" / "local-system"
    package.copy_payload(dest)
    manifest = json.loads((dest / "PACKAGE-MANIFEST.json").read_text(encoding="utf-8"))
    entries = {item["path"]: item for item in manifest["files"]}
    assert package.REQUIRED_FILES <= set(entries)
    assert "LICENSE" in entries
    assert "sonder_build.json" in entries
    build = json.loads((dest / "sonder_build.json").read_text(encoding="utf-8"))
    assert build["version"]
    assert build["commit_sha"] == "unknown" or len(build["commit_sha"]) == 40
    assert "runtime_policy.py" not in entries
    assert "sonder_runtime/adapters/runtime_policy.py" in entries
    assert "learning_health.py" in entries
    assert "sonder_health.py" in entries
    assert "sonder_runtime/adapters/inspection_executor.py" in entries
    assert {
        "sonder_runtime/adapters/backup.py",
        "sonder_runtime/adapters/backup.py",
        "sonder_runtime/adapters/backup_gateway.py",
        "sonder_runtime/application/backup/__init__.py",
        "sonder_runtime/application/backup/use_cases.py",
        "sonder_runtime/application/ports/backup.py",
    } <= set(entries)
    assert "recall.py" not in entries
    assert {
        "sonder_runtime/adapters/recall_gateway.py",
        "sonder_runtime/adapters/recall.py",
        "sonder_runtime/application/ports/recall.py",
        "sonder_runtime/application/recall/__init__.py",
        "sonder_runtime/application/recall/use_cases.py",
    } <= set(entries)
    assert "sonder_runtime/adapters/git_discovery.py" in entries
    assert {
        "sonder_runtime/adapters/preflight.py",
        "sonder_runtime/adapters/preflight_executor.py",
        "sonder_runtime/adapters/preflight.py",
        "sonder_runtime/application/ports/preflight.py",
        "sonder_runtime/application/preflight/__init__.py",
        "sonder_runtime/application/preflight/use_cases.py",
    } <= set(entries)
    assert "sonder_runtime/application/inspection/use_cases.py" in entries
    assert {
        "sonder_runtime/adapters/preference_adapters.py",
        "sonder_runtime/application/ports/preferences.py",
        "sonder_runtime/application/preferences/__init__.py",
        "sonder_runtime/application/preferences/use_cases.py",
    } <= set(entries)
    assert "memory_store.py" in entries
    assert "sonder_runtime/adapters/memory_store.py" in entries
    assert "eval_history.py" not in entries
    assert "sonder_runtime/adapters/evaluation_history_store.py" in entries
    assert "sonder_runtime/application/evaluation_history/__init__.py" in entries
    assert "sonder_runtime/application/evaluation_history/use_cases.py" in entries
    assert "sonder_runtime/application/ports/evaluation_history.py" in entries
    assert "process_liveness.py" not in entries
    assert "artifact_grounding.py" in entries
    assert {
        "artifact_risk.py", "pdf_risk.py", "process_risk.py", "unsafe_lab.py",
    } <= set(entries)
    assert "media_assets.py" in entries
    assert "sonder_runtime/adapters/model_transport.py" in entries
    assert "ollama_endpoint.py" not in entries
    assert "sonder_runtime/adapters/ollama/endpoint.py" in entries
    assert "sonder_runtime/adapters/embedding_cache.py" in entries
    assert "sonder_runtime/adapters/embeddings.py" in entries
    assert "sonder_runtime/adapters/accelerators/npu/contract.py" in entries
    assert "sonder_runtime/adapters/accelerators/npu/manifest.py" in entries
    assert "sonder_runtime/adapters/accelerators/npu/providers.py" in entries
    assert "sonder_runtime/adapters/accelerators/npu/npu_broker.py" in entries
    assert "sonder_runtime/adapters/accelerators/npu/npu_worker.py" in entries
    assert "model_assets.py" in entries
    assert "ooxml_assets.py" in entries
    assert "requirements-runtime.txt" in entries
    assert {
        "sonder_runtime/adapters/filesystem/workflow_store.py",
        "sonder_runtime/adapters/workflow_adapters.py",
        "sonder_runtime/application/ports/workflows.py",
        "sonder_runtime/application/workflows/__init__.py",
        "sonder_runtime/application/workflows/loop.py",
        "sonder_runtime/application/workflows/use_cases.py",
    } <= set(entries)
    assert "BUNDLED_SYSTEM_README.txt" in entries
    assert {
        "sonder-headless.cmd",
        "sonder-headless.sh",
        "sonder_headless.py",
        "sonder-runtime.cmd",
        "sonder-runtime.sh",
        "sonder-serve.cmd",
        "sonder-serve.sh",
        "sonder_runtime/interfaces/http/serve.py",
    } <= package.REQUIRED_FILES
    bundled_readme = (dest / "BUNDLED_SYSTEM_README.txt").read_text(
        encoding="utf-8"
    )
    assert "Sonder Runtime local system" in bundled_readme
    assert "sonder-headless" in bundled_readme
    assert "sonder-launcher" in bundled_readme
    assert "mobile clients can start, stop, or restart" in bundled_readme
    for private in (
        "file_roots.local",
        "Modelfile.personal",
        "system_profile.md",
        "memory.db",
        ".vs/state.txt",
        "tests/test_demo.py",
    ):
        assert private not in entries
        assert not (dest / private).exists()
    for rel, item in entries.items():
        data = (dest / rel).read_bytes()
        assert item["size"] == len(data)
        assert item["sha256"] == hashlib.sha256(data).hexdigest()
    assert not (dest / "dist" / "pkg").exists()


def test_repository_does_not_track_private_training_artifacts():
    tracked = {path.name.lower() for path in package._tracked_files()}
    forbidden = {
        "combined_personal.jsonl",
        "personal_dataset.jsonl",
        "modelfile.personal",
    }
    assert tracked.isdisjoint(forbidden)


def test_zip_is_deterministic_and_contains_manifest(monkeypatch, tmp_path):
    root = _fake_repo(tmp_path, monkeypatch)
    dest = root / "app" / "build" / "local-system"
    archive = root / "app" / "assets" / "local-system.zip"
    package.copy_payload(dest)
    package.zip_payload(dest, archive)
    first = hashlib.sha256(archive.read_bytes()).hexdigest()
    (dest / "unlisted-local-state.txt").write_text("private", encoding="utf-8")
    package.zip_payload(dest, archive)
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == first
    with package.zipfile.ZipFile(archive) as zf:
        assert not any(name.endswith("unlisted-local-state.txt") for name in zf.namelist())
        shell = zf.getinfo("local-system/bootstrap-engine.sh")
        assert (shell.external_attr >> 16) & 0o777 == 0o755


def test_payload_stamps_explicit_runtime_build_identity(monkeypatch, tmp_path):
    root = _fake_repo(tmp_path, monkeypatch)
    dest = root / "dist" / "local-system"
    revision = "a" * 40
    package.copy_payload(
        dest,
        build_version="1.2.3",
        build_revision=revision,
    )
    assert json.loads((dest / "sonder_build.json").read_text(encoding="utf-8")) == {
        "commit_sha": revision,
        "version": "1.2.3",
    }


def test_payload_rejects_ambiguous_revision_before_replacing_output(
    monkeypatch, tmp_path
):
    root = _fake_repo(tmp_path, monkeypatch)
    dest = root / "dist" / "local-system"
    dest.mkdir(parents=True)
    sentinel = dest / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="full 40-character"):
        package.copy_payload(dest, build_revision="short-sha")
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_verified_install_copies_only_manifested_files(monkeypatch, tmp_path):
    root = _fake_repo(tmp_path, monkeypatch)
    package_dir = root / "dist" / "local-system"
    package.copy_payload(package_dir)
    (package_dir / "unlisted-private-state.txt").write_text(
        "must not be installed", encoding="utf-8"
    )

    installed = tmp_path / "release-staging"
    package.copy_verified_payload(package_dir, installed)

    assert (installed / "PACKAGE-MANIFEST.json").is_file()
    assert (installed / "README.md").read_text(encoding="utf-8").startswith(
        "safe content"
    )
    assert not (installed / "unlisted-private-state.txt").exists()


def test_verified_install_rejects_tampering_and_nonempty_destination(
    monkeypatch, tmp_path
):
    root = _fake_repo(tmp_path, monkeypatch)
    package_dir = root / "dist" / "local-system"
    package.copy_payload(package_dir)

    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "sentinel").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="must be empty"):
        package.copy_verified_payload(package_dir, nonempty)

    (package_dir / "README.md").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest (size|hash) mismatch"):
        package.copy_verified_payload(package_dir, tmp_path / "tampered-install")


def test_optional_engine_bundle_is_binary_safe_sealed_and_executable(
    monkeypatch,
    tmp_path,
):
    root = _fake_repo(tmp_path, monkeypatch)
    source = tmp_path / "prepared-engine"
    runtime = source / "runtime" / "ollama" / "ollama.exe"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"binary runtime\xff")
    engine_manifest = source / "ENGINE-BUNDLE.json"
    engine_manifest.write_text("{}\n", encoding="utf-8")
    record = package.engine_bundle.BundleFile(
        Path("runtime/ollama/ollama.exe"),
        runtime.stat().st_size,
        package.engine_bundle.sha256_file(runtime),
        True,
    )
    fake_bundle = SimpleNamespace(
        root=source,
        manifest_path=engine_manifest,
        identity="windows-x86_64",
        files=(record,),
    )
    monkeypatch.setattr(
        package.engine_bundle,
        "load_engine_bundle",
        lambda *args, **kwargs: fake_bundle,
    )

    dest = root / "dist" / "local-system"
    package.copy_payload(dest, source)
    copied = dest / "engine" / "windows-x86_64" / record.relative
    assert copied.read_bytes() == runtime.read_bytes()
    manifest = json.loads((dest / "PACKAGE-MANIFEST.json").read_text(encoding="utf-8"))
    entries = {item["path"]: item for item in manifest["files"]}
    key = "engine/windows-x86_64/runtime/ollama/ollama.exe"
    assert entries[key]["sha256"] == record.sha256
    assert entries[key]["mode"] == 0o755


def test_zip_rejects_tampered_manifest_content_and_preserves_archive(monkeypatch, tmp_path):
    root = _fake_repo(tmp_path, monkeypatch)
    dest = root / "dist" / "local-system"
    archive = root / "dist" / "local-system.zip"
    package.copy_payload(dest)
    package.zip_payload(dest, archive)
    before = archive.read_bytes()
    (dest / "README.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest (?:size|hash) mismatch"):
        package.zip_payload(dest, archive)
    assert archive.read_bytes() == before


@pytest.mark.parametrize(
    "unsafe_data",
    [
        b"C:\\Users\\someone\\private.txt",
        b"token=sk-" + (b"A" * 32),
        b"nul\x00data",
        b"\xff\xfe",
    ],
)
def test_privacy_scan_fails_closed_before_replacing_output(
    monkeypatch, tmp_path, unsafe_data
):
    root = _fake_repo(tmp_path, monkeypatch)
    leak = root / "docs" / "leak.md"
    leak.parent.mkdir(parents=True, exist_ok=True)
    leak.write_bytes(unsafe_data)
    package._tracked_files().append(leak)
    dest = root / "dist" / "local-system"
    dest.mkdir(parents=True)
    sentinel = dest / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError):
        package.copy_payload(dest)
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_shipped_selfmod_documentation_contains_no_absolute_user_home():
    package._privacy_scan(package.ROOT / "SELFMOD.md")


def test_privacy_scan_distinguishes_prose_from_an_actual_home_path(tmp_path):
    document = tmp_path / "README.md"
    document.write_text("tool/root allowlists are guarded\n", encoding="utf-8")
    package._privacy_scan(document)
    document.write_text(
        f"private file: {Path.home() / 'secrets.txt'}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="absolute user-home"):
        package._privacy_scan(document)


def test_zip_rejects_noncanonical_source_and_archive_paths(monkeypatch, tmp_path):
    root = _fake_repo(tmp_path, monkeypatch)
    dest = root / "app" / "build" / "local-system"
    package.copy_payload(dest)
    sentinel = root / "app" / "assets" / "other.zip"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_bytes(b"keep")
    with pytest.raises(ValueError):
        package.zip_payload(root / "scripts", root / "app" / "assets" / "local-system.zip")
    with pytest.raises(ValueError):
        package.zip_payload(dest, sentinel)
    assert sentinel.read_bytes() == b"keep"


def test_rejects_symlink_escape(monkeypatch, tmp_path):
    root = _fake_repo(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    link = root / "dist"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlinks are unavailable")
    with pytest.raises(ValueError):
        package.copy_payload(link / "local-system")
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_rejects_nested_reparse_in_existing_package(monkeypatch, tmp_path):
    root = _fake_repo(tmp_path, monkeypatch)
    outside = tmp_path / "outside-tree"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    dest = root / "dist" / "local-system"
    dest.mkdir(parents=True)
    try:
        os.symlink(outside, dest / "escape", target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlinks are unavailable")
    with pytest.raises(ValueError):
        package.copy_payload(dest)
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_empty_package_marker_is_scannable_not_oversized(tmp_path):
    """An empty file and a >16MB file both read as b"", but they are opposite
    problems. Collapsing them rejected every zero-byte __init__.py -- the
    ordinary Python package marker -- claiming it was "too large to inspect
    safely", which is the reverse of what was true."""
    empty = tmp_path / "__init__.py"
    empty.write_bytes(b"")
    package._privacy_scan(empty)  # must not raise: nothing in it to leak

    oversized = tmp_path / "huge.txt"
    with open(oversized, "wb") as handle:
        handle.seek(16 * 1024 * 1024)
        handle.write(b"x")
    with pytest.raises(ValueError, match="too large to inspect safely"):
        package._privacy_scan(oversized)


def test_layered_package_ships_so_the_served_app_can_actually_start():
    """The flat modules alone are not a runnable server. runtime_policy.py
    and other entry modules import sonder_runtime.*, so omitting that
    directory produced bundles whose server died on first import with
    ModuleNotFoundError before binding a port -- and the desktop app showed
    only "cannot reach server", because the crash lands in a detached
    process whose log the GUI never reads."""
    assert "sonder_runtime" in package.ALLOWED_DIRS

    repo = Path(package.__file__).resolve().parents[1]
    layered = repo / "sonder_runtime"
    assert layered.is_dir(), "sonder_runtime package missing from the repo"

    # Every flat module the packager ships that imports the layered package
    # is a module that would crash a bundle built without it.
    importers = [
        path.name
        for path in sorted(repo.glob("*.py"))
        if path.name in package.REQUIRED_FILES
        and "sonder_runtime" in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert importers, "expected shipped modules importing sonder_runtime"


def test_store_migrations_are_required_in_desktop_payload():
    """A bundle without these baselines cannot create fresh runtime stores."""
    required = {
        "migrations/autopilot/0001_baseline.py",
        "migrations/fleet/0001_baseline.py",
        "migrations/memory/0001_baseline.py",
        "migrations/operations/0001_baseline.py",
        "migrations/updates/0001_baseline.py",
    }
    assert "migrations" in package.ALLOWED_DIRS
    assert required <= package.REQUIRED_FILES
    assert all(package._included(Path(item)) for item in required)
