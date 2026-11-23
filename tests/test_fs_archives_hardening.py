"""Regression tests for Wave 2 filesystem, archive, inspection, and artifact_fetch hardening."""
from __future__ import annotations

import io
import os
import struct
import tarfile
import zipfile

import pytest

import sonder_runtime.adapters.archive_create as archive_create
import sonder_runtime.adapters.artifact_fetch as artifact_fetch
import sonder_runtime.adapters.filesystem.file_ops as file_ops
import sonder_runtime.adapters.inspection.archive_tools as archive_tools
import sonder_runtime.adapters.inspection.data_query as data_query
import sonder_runtime.adapters.inspection.schema_validate as schema_validate


# --- 1. file_ops.py hardening & smoothness --------------------------------


@pytest.mark.parametrize(
    "bad_name",
    [
        "CON",
        "NUL",
        "AUX",
        "PRN",
        "COM1",
        "COM9",
        "LPT1",
        "LPT9",
        "CON.txt",
        "nul.json",
        "sub/PRN.md",
        "sub/COM1 .log",
        "CONIN$",
        "CONOUT$",
        "COM\u00b9.txt",
        "LPT\u00b3",
    ],
)
def test_file_ops_rejects_windows_reserved_device_names(monkeypatch, tmp_path, bad_name):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    if os.name != "nt":
        monkeypatch.setattr(file_ops.os, "name", "nt")

    with pytest.raises(PermissionError, match="reserved device"):
        file_ops.resolve_path(bad_name)

    with pytest.raises(PermissionError, match="reserved device"):
        file_ops.resolve_mutation_path(bad_name)

    with pytest.raises(PermissionError, match="reserved device"):
        file_ops.write_file(bad_name, "hello", mode="create")

    with pytest.raises(PermissionError, match="reserved device"):
        file_ops.read_file(bad_name)


@pytest.mark.parametrize(
    "good_name",
    [
        "config.toml",
        "notes/nullable.py",
        "auxiliary.txt",
        "notes/printer.py",
        "com10.txt",
        "lpt10.log",
        "notes/console.py",
        "notes/con_summary.md",
    ],
)
def test_file_ops_allows_ordinary_names_resembling_device_prefixes(
    monkeypatch, tmp_path, good_name,
):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    result = file_ops.write_file(good_name, "ok\n", mode="create")
    assert result["action"] == "create"
    assert file_ops.read_file(good_name)["text"] == "ok\n"


@pytest.mark.parametrize(
    "foreign_path",
    [
        "C:foo.txt",
        "D:sub/bar.py",
        "/etc/passwd" if os.name == "nt" else "C:\\Windows\\win.ini",
        "\\Windows\\System32\\drivers\\etc\\hosts" if os.name == "nt" else "Z:/outside.txt",
    ],
)
def test_resolve_path_rejects_foreign_absolute_and_drive_relative(
    monkeypatch, tmp_path, foreign_path,
):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    with pytest.raises(PermissionError, match="foreign absolute"):
        file_ops.resolve_path(foreign_path)
    with pytest.raises(PermissionError, match="foreign absolute"):
        file_ops.read_file(foreign_path)


def test_inspect_toml_enforces_max_bytes(monkeypatch, tmp_path):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    small = tmp_path / "small.toml"
    small.write_text("[app]\nname = 'ok'\n", encoding="utf-8")
    ok = file_ops.inspect_data(str(small), max_bytes=128)
    assert ok["kind"] == "toml"
    assert ok["table_count"] == 1

    big = tmp_path / "big.toml"
    big.write_text("[app]\npayload = '" + ("x" * 2048) + "'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="inspection parses at most 256"):
        file_ops.inspect_data(str(big), max_bytes=256)


def test_read_write_edit_give_actionable_directory_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    folder = tmp_path / "subdir"
    folder.mkdir()

    with pytest.raises(ValueError, match="is a directory; use directory_tree or file_find"):
        file_ops.read_file(str(folder))

    with pytest.raises(ValueError, match="is a directory; use directory_tree or file_find"):
        file_ops.edit_file(str(folder), "a", "b")

    for mode in ("create", "overwrite", "append"):
        with pytest.raises(ValueError, match="is a directory"):
            file_ops.write_file(str(folder), "hello", mode=mode)


# --- 2. archive_tools.py & archive_create.py hardening --------------------


def test_archive_extract_rejects_dangling_symlink_destination(monkeypatch, tmp_path):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))

    src_zip = tmp_path / "bundle.zip"
    with zipfile.ZipFile(src_zip, "w") as zf:
        zf.writestr("hello.txt", "world")

    real_target = tmp_path / "real_target"
    dangling = tmp_path / "dangling_out"
    try:
        dangling.symlink_to(real_target, target_is_directory=True)
    except OSError as exc:
        pytest.skip("symlink creation unavailable: %s" % exc)

    with pytest.raises(PermissionError, match="symlink or junction"):
        archive_tools.extract_archive(str(src_zip), str(dangling))
    assert not real_target.exists()


def test_archive_create_rejects_dangling_symlink_destination(monkeypatch, tmp_path):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))

    sample = tmp_path / "hello.txt"
    sample.write_text("world", encoding="utf-8")

    real_target = tmp_path / "real_out.zip"
    dangling = tmp_path / "dangling.zip"
    try:
        dangling.symlink_to(real_target)
    except OSError as exc:
        pytest.skip("symlink creation unavailable: %s" % exc)

    with pytest.raises(PermissionError, match="symlink or junction"):
        archive_create.create_archive(str(tmp_path), ["hello.txt"], str(dangling))
    assert not real_target.exists()


def test_compressed_tar_enforces_max_ratio_per_entry_in_plan_entries(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))

    # Build a tiny compressed .tar.gz with 6 zero-filled 1 MiB entries (~6 KB compressed).
    bomb = tmp_path / "bomb.tar.gz"
    chunk = b"\x00" * (1024 * 1024)
    with tarfile.open(bomb, "w:gz") as tf:
        for index in range(6):
            info = tarfile.TarInfo(name=f"zero_{index}.bin")
            info.size = len(chunk)
            tf.addfile(info, io.BytesIO(chunk))

    assert bomb.stat().st_size < 32_768

    seen_entries = []
    original_tar_entry = archive_tools._tar_entry

    def _spy_tar_entry(info, limits):
        seen_entries.append(info.name)
        return original_tar_entry(info, limits)

    monkeypatch.setattr(archive_tools, "_tar_entry", _spy_tar_entry)

    listing = archive_tools.list_archive(str(bomb), max_ratio=50.0)
    assert listing["valid"] is False
    assert any("compression-ratio ceiling" in err for err in listing["errors"])
    # Stopped on the very first entry rather than walking all 6 members.
    assert len(seen_entries) == 1

    seen_entries.clear()
    with pytest.raises(archive_tools.ArchiveRejected, match="compression-ratio ceiling"):
        archive_tools.extract_archive(str(bomb), str(tmp_path / "out"), max_ratio=50.0)
    assert len(seen_entries) == 1


def _build_zip_with_duplicate_header_offset() -> bytes:
    """Create a ZIP whose central directory has two distinct names at header_offset 0."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("a.txt", b"hello")
        zf.writestr("b.txt", b"world")
    raw = bytearray(buf.getvalue())

    # Locate End of Central Directory (PK\x05\x06).
    eocd_pos = raw.rfind(b"PK\x05\x06")
    assert eocd_pos > 0
    cd_offset = struct.unpack_from("<I", raw, eocd_pos + 16)[0]

    # Walk the two Central Directory headers (PK\x01\x02) and patch the second's
    # relative offset of local header (offset +42) to equal the first's (0).
    pos = cd_offset
    assert raw[pos:pos + 4] == b"PK\x01\x02"
    name_len1, extra_len1, comment_len1 = struct.unpack_from("<HHH", raw, pos + 28)
    first_local_offset = struct.unpack_from("<I", raw, pos + 42)[0]
    pos2 = pos + 46 + name_len1 + extra_len1 + comment_len1
    assert raw[pos2:pos2 + 4] == b"PK\x01\x02"
    struct.pack_into("<I", raw, pos2 + 42, first_local_offset)
    return bytes(raw)


def test_archive_rejects_duplicate_source_index_header_offset(monkeypatch, tmp_path):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))

    # Direct unit test of _validate_entries
    limits = archive_tools._limits(100, 10000, 100000, 100.0, 10, 100, 10.0)
    entries = [
        {"path": "a.txt", "type": "file", "bytes": 5, "source_index": 0},
        {"path": "b.txt", "type": "file", "bytes": 5, "source_index": 0},
    ]
    with pytest.raises(archive_tools.ArchiveRejected, match="duplicate archive entry header offset"):
        archive_tools._validate_entries(entries, limits, 1024)

    # End-to-end crafted ZIP binary test
    crafted = tmp_path / "dup_offset.zip"
    crafted.write_bytes(_build_zip_with_duplicate_header_offset())

    listing = archive_tools.list_archive(str(crafted))
    assert listing["valid"] is False
    assert any("header offset" in err for err in listing["errors"])

    with pytest.raises(archive_tools.ArchiveRejected, match="header offset"):
        archive_tools.extract_archive(str(crafted), str(tmp_path / "out"))
    assert not (tmp_path / "out").exists()


# --- 3. schema_validate.py & data_query.py ancestor reparse checks --------


def test_schema_validate_and_data_query_reject_ancestor_symlink_dir(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))

    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    data_file = real_dir / "items.json"
    data_file.write_text('{"name": "ok"}', encoding="utf-8")
    schema_file = real_dir / "schema.json"
    schema_file.write_text('{"type": "object"}', encoding="utf-8")

    link_dir = tmp_path / "link_dir"
    try:
        link_dir.symlink_to(real_dir, target_is_directory=True)
    except OSError as exc:
        pytest.skip("symlink creation unavailable: %s" % exc)

    # Direct paths work fine.
    res = schema_validate.validate_schema(str(data_file), schema_path=str(schema_file))
    assert res["ok"] is True and res["valid"] is True
    q = data_query.query_data(str(data_file))
    assert q["ok"] is True and q["rows"] == [{"name": "ok"}]

    # Ancestor symlink directory rejected on data path, schema path, and data_query.
    with pytest.raises(schema_validate.SchemaValidateError, match="symlink or junction"):
        schema_validate.validate_schema(
            str(link_dir / "items.json"), schema_json='{"type": "object"}',
        )

    with pytest.raises(schema_validate.SchemaValidateError, match="symlink or junction"):
        schema_validate.validate_schema(
            str(data_file), schema_path=str(link_dir / "schema.json"),
        )

    with pytest.raises(data_query.DataQueryError, match="symlink or junction"):
        data_query.query_data(str(link_dir / "items.json"))


# --- 4. artifact_fetch.py redirect downgrade & URL-bound resume -----------


class _FakeResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes = b""):
        self.status = status
        self.headers = headers
        self._stream = io.BytesIO(body)

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.mark.parametrize("status_code", [301, 302, 303, 307, 308])
def test_artifact_fetch_rejects_https_to_http_redirect_downgrade(
    monkeypatch, tmp_path, status_code,
):
    monkeypatch.setenv("SONDER_WEB_TOOLS", "1")
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))

    seen_urls = []

    def _fake_open(url, headers, timeout, *, deadline=None):
        seen_urls.append(url)
        if url.startswith("https://"):
            return _FakeResponse(
                status_code,
                {"Location": "http://mirror.example.invalid/payload.zip"},
            )
        raise AssertionError("HTTP downgrade target must never be requested: %s" % url)

    monkeypatch.setattr(artifact_fetch, "_open_request", _fake_open)

    dest = tmp_path / "payload.zip"
    result = artifact_fetch.fetch_artifact(
        "https://secure.example.invalid/payload.zip",
        str(dest),
        expect_type="zip",
    )

    assert result["ok"] is False
    assert result["verdict"] == "rejected"
    assert not dest.exists()
    assert seen_urls == ["https://secure.example.invalid/payload.zip"]
    redirect_failures = [r for r in result["failures"] if r["check"] == "redirect"]
    assert redirect_failures
    assert "HTTPS-to-HTTP" in redirect_failures[0]["detail"]


def test_artifact_fetch_allows_https_to_https_redirect(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_WEB_TOOLS", "1")
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))

    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("ok.bin", bytes(256))
    zip_body = zbuf.getvalue()

    def _fake_open(url, headers, timeout, *, deadline=None):
        assert deadline is not None
        if url == "https://secure.example.invalid/start.zip":
            return _FakeResponse(
                302, {"Location": "https://cdn.example.invalid/final.zip"},
            )
        if url == "https://cdn.example.invalid/final.zip":
            return _FakeResponse(
                200,
                {
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(zip_body)),
                },
                zip_body,
            )
        raise AssertionError("unexpected URL: %s" % url)

    monkeypatch.setattr(artifact_fetch, "_open_request", _fake_open)

    dest = tmp_path / "start.zip"
    result = artifact_fetch.fetch_artifact(
        "https://secure.example.invalid/start.zip",
        str(dest),
        expect_type="zip",
    )
    assert result["ok"] is True, result["failures"]
    assert result["final_url"] == "https://cdn.example.invalid/final.zip"
    assert dest.read_bytes() == zip_body


def test_artifact_fetch_resume_binds_part_to_origin_url_digest(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_WEB_TOOLS", "1")
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))

    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("clean.bin", b"A" * 512)
    zip_body = zbuf.getvalue()

    url_a = "https://origin-a.example.invalid/pkg.zip"
    url_b = "https://origin-b.example.invalid/pkg.zip"
    dest = tmp_path / "pkg.zip"

    # Seed a partial from URL A and an unbound legacy partial.
    part_a = artifact_fetch._part_path(dest, url_a)
    part_b = artifact_fetch._part_path(dest, url_b)
    legacy_part = artifact_fetch._part_path(dest)
    assert part_a != part_b
    assert part_a != legacy_part
    assert part_b != legacy_part

    part_a.write_bytes(b"POISON_FROM_URL_A" * 10)
    legacy_part.write_bytes(b"POISON_LEGACY" * 10)

    seen_ranges = []

    def _fake_open(url, headers, timeout, *, deadline=None):
        seen_ranges.append((url, headers.get("Range")))
        assert url == url_b
        assert "Range" not in headers, "must not resume a partial belonging to a different URL"
        return _FakeResponse(
            200,
            {
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(zip_body)),
            },
            zip_body,
        )

    monkeypatch.setattr(artifact_fetch, "_open_request", _fake_open)

    result = artifact_fetch.fetch_artifact(url_b, str(dest), expect_type="zip", resume=True)
    assert result["ok"] is True, result["failures"]
    assert result["resumed_from"] == 0
    assert dest.read_bytes() == zip_body
    assert seen_ranges == [(url_b, None)]
