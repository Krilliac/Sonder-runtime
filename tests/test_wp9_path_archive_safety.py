from pathlib import Path
import tarfile
import zipfile

import pytest

from sonder_runtime.application.security.path_archive_safety import (
    ArchiveLimits,
    ArchiveLimitError,
    ProvenanceLabel,
    UnsafePathError,
    authorized_path,
    inspect_tar,
    inspect_zip,
    validate_archive_members,
)


def test_authorized_path_rejects_escape_and_accepts_child(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    assert authorized_path(root / "nested" / "file.txt", [root]).is_relative_to(root)
    with pytest.raises(UnsafePathError):
        authorized_path(root / ".." / "outside.txt", [root])


def test_archive_traversal_links_and_expansion_are_rejected():
    with pytest.raises(UnsafePathError):
        validate_archive_members([("../escape.txt", 1, False)])
    with pytest.raises(UnsafePathError):
        validate_archive_members([("link", 1, True)])
    with pytest.raises(ArchiveLimitError):
        validate_archive_members([("large", 11, False)], limits=ArchiveLimits(max_entry_bytes=10))


def test_zip_and_tar_inspection_apply_same_policy(tmp_path: Path):
    zpath = tmp_path / "safe.zip"
    with zipfile.ZipFile(zpath, "w") as archive:
        archive.writestr("docs/readme.txt", "ok")
    assert inspect_zip(zpath) == ("docs/readme.txt",)
    tpath = tmp_path / "safe.tar"
    source = tmp_path / "source.txt"
    source.write_text("ok", encoding="utf-8")
    with tarfile.open(tpath, "w") as archive:
        archive.add(source, arcname="docs/readme.txt")
    assert inspect_tar(tpath) == ("docs/readme.txt",)


def test_provenance_label_is_explicit_and_bounded():
    label = ProvenanceLabel("uploaded-archive", content_digest="sha256:abc")
    assert label.trust == "untrusted"
    with pytest.raises(ValueError):
        ProvenanceLabel("", trust="trusted")
