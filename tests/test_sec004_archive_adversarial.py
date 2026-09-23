"""SEC-004 adversarial archive corpus across every production extraction path.

Each case is an attack an archive can carry.  A protection is only counted
when a case here drives it; cases assert both the rejection and the absence
of any escaped or partially promoted output.

Production archive readers covered:

* ``adapters.inspection.archive_tools`` -- ``archive_list``/``archive_extract``
  tools (untrusted, model/user supplied archives);
* ``adapters.updates.service.safe_extract`` -- update-bundle staging;
* ``adapters.filesystem.file_ops.inspect_data`` -- ``.zip``/``.tar`` previews.
"""
from __future__ import annotations

import io
import struct
import tarfile
import zipfile

import pytest

import sonder_runtime.adapters.filesystem.file_ops as file_ops
from sonder_runtime.adapters.inspection import archive_tools
from sonder_runtime.adapters.updates.service import ExtractionError, safe_extract


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    monkeypatch.setattr(file_ops.sonder_paths, "default_home", lambda: home)
    monkeypatch.delenv("SONDER_FILE_ROOTS", raising=False)
    return root


def _tar(path, rows, mode="w"):
    with tarfile.open(path, mode) as archive:
        for name, payload in rows:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return path


def _zip(path, rows, compression=zipfile.ZIP_DEFLATED):
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for name, payload in rows:
            archive.writestr(name, payload)
    return path


def _tree(path):
    if not path.exists():
        return []
    return sorted(p.relative_to(path).as_posix() for p in path.rglob("*"))


# ---------------------------------------------------------------------------
# Update-bundle staging (updates.service.safe_extract)
# ---------------------------------------------------------------------------

HOSTILE_UPDATE_NAMES = [
    # Drive-relative: on Windows ``dest / "C:x"`` discards ``dest`` when the
    # drive differs, so this escapes staging without being "absolute".
    "C:escape.txt",
    "D:/escape.txt",
    # UNC and backslash traversal spellings.
    "\\\\server\\share\\escape.txt",
    "sub\\..\\..\\escape.txt",
    # NTFS alternate data stream: writes hidden content onto another file.
    "app.py:hidden",
    # Reserved device names and trailing dot/space aliases.
    "CON",
    "nul.txt",
    "sub/com1.py",
    "trailing. ",
    # Control characters.
    "bad\x01name.txt",
]


@pytest.mark.parametrize("name", HOSTILE_UPDATE_NAMES)
def test_update_staging_rejects_non_portable_member_names(tmp_path, name):
    archive = _tar(tmp_path / "bundle.tar.gz", [(name, b"x")], mode="w:gz")
    dest = tmp_path / "stage" / "out"
    with pytest.raises(ExtractionError):
        safe_extract(archive, dest, max_expanded_bytes=1024)
    assert not (tmp_path / "escape.txt").exists()
    assert _tree(dest) == []


def test_update_staging_rejects_duplicate_and_case_colliding_members(tmp_path):
    # A later duplicate silently replaces an earlier member; on case-folding
    # filesystems (Windows, default macOS) the same happens for "A"/"a".
    for rows in (
        [("app.py", b"good"), ("app.py", b"evil")],
        [("App.py", b"good"), ("app.py", b"evil")],
        [("pkg/mod.py", b"good"), ("PKG/mod.py", b"evil")],
    ):
        archive = _tar(tmp_path / "dup.tar", rows)
        dest = tmp_path / "out"
        with pytest.raises(ExtractionError, match="duplicate|collid"):
            safe_extract(archive, dest, max_expanded_bytes=1024)
        assert _tree(dest) == []
        archive.unlink()


def test_update_staging_bounds_member_count(tmp_path):
    rows = [("f%03d.txt" % index, b"") for index in range(50)]
    archive = _tar(tmp_path / "many.tar", rows)
    with pytest.raises(ExtractionError, match="member"):
        safe_extract(
            archive, tmp_path / "out", max_expanded_bytes=1024, max_members=10,
        )
    # Validation precedes all writes: nothing is materialized.
    assert _tree(tmp_path / "out") == []


def test_update_staging_default_member_bound_is_finite(tmp_path):
    import inspect

    default = inspect.signature(safe_extract).parameters["max_members"].default
    assert isinstance(default, int) and 0 < default <= 100_000


def test_update_staging_rejects_hostile_names_before_writing_any_member(tmp_path):
    archive = _tar(
        tmp_path / "mixed.tar",
        [("ok.txt", b"fine"), ("../escape.txt", b"x")],
    )
    dest = tmp_path / "out"
    with pytest.raises(ExtractionError):
        safe_extract(archive, dest, max_expanded_bytes=1024)
    assert _tree(dest) == []
    assert not (tmp_path / "escape.txt").exists()


def test_update_staging_rejects_hardlink_and_fifo(tmp_path):
    for kind in (tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.BLKTYPE):
        archive = tmp_path / "special.tar"
        with tarfile.open(archive, "w") as tar:
            info = tarfile.TarInfo("special")
            info.type = kind
            info.linkname = "app.py" if kind == tarfile.LNKTYPE else ""
            tar.addfile(info)
        with pytest.raises(ExtractionError):
            safe_extract(archive, tmp_path / "out", max_expanded_bytes=1024)
        archive.unlink()


def test_update_staging_still_extracts_a_benign_bundle(tmp_path):
    archive = _tar(
        tmp_path / "ok.tar.gz",
        [("app.py", b"print('ok')\n"), ("pkg/mod.py", b"x = 1\n"), (".hidden/cfg", b"k=v")],
        mode="w:gz",
    )
    dest = tmp_path / "out"
    written = safe_extract(archive, dest, max_expanded_bytes=1024)
    assert written == len(b"print('ok')\n") + len(b"x = 1\n") + len(b"k=v")
    assert (dest / "pkg" / "mod.py").read_bytes() == b"x = 1\n"


# ---------------------------------------------------------------------------
# archive_list / archive_extract (inspection.archive_tools)
# ---------------------------------------------------------------------------

def test_tar_planning_stops_at_aggregate_budget_before_reading_further_members(
    workspace, monkeypatch,
):
    # For compressed TARs every step to the next header decompresses the
    # previous payload, so the aggregate budget must stop the walk as soon
    # as it is exceeded instead of after scanning the whole archive.
    rows = [("m%02d.bin" % index, b"z" * 600) for index in range(8)]
    _tar(workspace / "walk.tar.gz", rows, mode="w:gz")
    seen = []
    original = archive_tools._tar_entry

    def counting(info, limits):
        seen.append(info.name)
        return original(info, limits)

    monkeypatch.setattr(archive_tools, "_tar_entry", counting)
    result = archive_tools.list_archive(
        "walk.tar.gz", max_total_bytes=1000, max_ratio=1000,
    )
    assert result["valid"] is False
    assert "aggregate" in result["errors"][0]
    assert len(seen) == 2


def test_zip_overlapping_entries_fail_closed_without_promotion(workspace):
    # Two central-directory records aimed at one local header: the
    # overlapping-file construction used by non-recursive zip bombs.
    source = workspace / "overlap.zip"
    _zip(source, [("a.txt", b"A" * 64), ("b.txt", b"B" * 64)])
    raw = bytearray(source.read_bytes())
    central = raw.find(b"PK\x01\x02")
    second = raw.find(b"PK\x01\x02", central + 4)
    first_offset = struct.unpack_from("<I", raw, central + 42)[0]
    struct.pack_into("<I", raw, second + 42, first_offset)
    source.write_bytes(bytes(raw))

    with pytest.raises((archive_tools.ArchiveRejected, zipfile.BadZipFile, OSError)):
        archive_tools.extract_archive(
            "overlap.zip", "out", developer_authorized=True,
        )
    assert not (workspace / "out").exists()
    assert not any(p.name.startswith(".sonder-archive-") for p in workspace.iterdir())


@pytest.mark.parametrize("delta", [-8, 8])
def test_zip_declared_size_lies_fail_closed(workspace, delta):
    source = workspace / "lie.zip"
    payload = b"payload-" * 32
    _zip(source, [("a.txt", payload)], compression=zipfile.ZIP_STORED)
    raw = bytearray(source.read_bytes())
    local = raw.find(b"PK\x03\x04")
    central = raw.find(b"PK\x01\x02")
    lie = len(payload) + delta
    struct.pack_into("<I", raw, local + 22, lie)
    struct.pack_into("<I", raw, central + 24, lie)
    source.write_bytes(bytes(raw))

    with pytest.raises((archive_tools.ArchiveRejected, zipfile.BadZipFile, OSError)):
        archive_tools.extract_archive("lie.zip", "out", developer_authorized=True)
    assert not (workspace / "out").exists()


def test_deflate_bomb_is_rejected_by_ratio_before_decompression(workspace, monkeypatch):
    _zip(workspace / "bomb.zip", [("bomb.bin", b"\0" * 5_000_000)])

    def forbidden(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("bomb payload was decompressed")

    monkeypatch.setattr(archive_tools, "_copy_stream", forbidden)
    with pytest.raises(archive_tools.ArchiveRejected, match="ratio"):
        archive_tools.extract_archive("bomb.zip", "out", developer_authorized=True)
    assert not (workspace / "out").exists()


def test_tar_member_count_is_bounded_while_walking(workspace):
    _tar(workspace / "many.tar", [("f%03d" % i, b"") for i in range(30)])
    result = archive_tools.list_archive("many.tar", max_entries=5)
    assert result["valid"] is False
    assert "entry ceiling" in result["errors"][0]


# ---------------------------------------------------------------------------
# inspect_data previews (filesystem.file_ops)
# ---------------------------------------------------------------------------

def test_tar_preview_bounds_decompression_and_reports_a_floor(tmp_path, monkeypatch):
    monkeypatch.setattr(file_ops, "INSPECT_MAX_ARCHIVE_SCAN_BYTES", 1_000)
    rows = [("m%02d.bin" % index, b"q" * 600) for index in range(10)]
    archive = _tar(tmp_path / "big.tgz", rows, mode="w:gz")
    result = file_ops._inspect_tar(archive)
    assert result["truncated"] is True
    assert result["members"] < 10
    assert result["expanded_bytes"] <= 1_000 + 600


def test_tar_preview_bounds_member_count(tmp_path, monkeypatch):
    monkeypatch.setattr(file_ops, "INSPECT_MAX_ARCHIVE_MEMBERS", 5)
    archive = _tar(tmp_path / "many.tar", [("f%02d" % i, b"") for i in range(20)])
    result = file_ops._inspect_tar(archive)
    assert result["truncated"] is True
    assert result["members"] == 5


def test_small_tar_preview_is_complete(tmp_path):
    archive = _tar(tmp_path / "small.tar", [("a", b"1"), ("b", b"22")])
    result = file_ops._inspect_tar(archive)
    assert result["truncated"] is False
    assert result["members"] == 2
    assert result["expanded_bytes"] == 3
