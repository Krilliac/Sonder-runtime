import hashlib
import json
import os

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
    assert "runtime_policy.py" in entries
    assert "learning_health.py" in entries
    assert "artifact_grounding.py" in entries
    assert "media_assets.py" in entries
    assert "model_assets.py" in entries
    assert "ooxml_assets.py" in entries
    assert "BUNDLED_SYSTEM_README.txt" in entries
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
