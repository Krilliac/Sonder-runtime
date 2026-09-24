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


# ---------------------------------------------------------------------------
# Review round 1 (PR #548): header metadata, device names, normalization
# ---------------------------------------------------------------------------

import gzip
import tracemalloc
import unicodedata

LONG_METADATA_BYTES = 48 * 1024 * 1024
PEAK_BUDGET_BYTES = 8 * 1024 * 1024


def _raw_tar_header(name: bytes, size: int, typeflag: bytes) -> bytes:
    header = bytearray(512)
    header[0:len(name)] = name
    header[100:108] = b"0000644\0"
    header[108:116] = b"0000000\0"
    header[116:124] = b"0000000\0"
    header[124:136] = b"%011o\0" % size
    header[136:148] = b"00000000000\0"
    header[148:156] = b" " * 8
    header[156:157] = typeflag
    header[257:265] = b"ustar  \0"
    checksum = sum(header)
    header[148:156] = b"%06o\0 " % checksum
    return bytes(header)


def _oversized_metadata_tar_gz(path, typeflag: bytes):
    # A GNU long-name ("L") or PAX ("x") record whose declared payload is
    # tens of MiB of one repeated byte: a few tens of KiB on disk.  Written
    # in chunks so building the fixture allocates almost nothing.
    chunk = b"a" * (1024 * 1024)
    with gzip.open(path, "wb", compresslevel=9) as stream:
        stream.write(_raw_tar_header(b"././@LongLink", LONG_METADATA_BYTES, typeflag))
        remaining = LONG_METADATA_BYTES
        while remaining:
            step = min(len(chunk), remaining)
            stream.write(chunk[:step])
            remaining -= step
        stream.write(b"\0" * ((-LONG_METADATA_BYTES) % 512))
        stream.write(_raw_tar_header(b"payload.txt", 1, b"0"))
        stream.write(b"x" + b"\0" * 511)
        stream.write(b"\0" * 1024)
    assert path.stat().st_size < 512 * 1024
    return path


def _peak(callable_):
    tracemalloc.start()
    try:
        try:
            callable_()
        except Exception:
            pass
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize("typeflag", [b"L", b"K", b"x"])
def test_oversized_tar_header_metadata_is_bounded_in_every_reader(
    workspace, tmp_path, typeflag,
):
    source = _oversized_metadata_tar_gz(workspace / "meta.tar.gz", typeflag)

    peak = _peak(lambda: archive_tools.list_archive("meta.tar.gz"))
    assert peak < PEAK_BUDGET_BYTES, "archive_list peak %d" % peak
    assert archive_tools.list_archive("meta.tar.gz")["valid"] is False

    peak = _peak(lambda: safe_extract(
        source, tmp_path / "stage", max_expanded_bytes=1 << 30,
    ))
    assert peak < PEAK_BUDGET_BYTES, "safe_extract peak %d" % peak
    with pytest.raises(ExtractionError):
        safe_extract(source, tmp_path / "stage2", max_expanded_bytes=1 << 30)

    peak = _peak(lambda: file_ops._inspect_tar(source))
    assert peak < PEAK_BUDGET_BYTES, "inspect preview peak %d" % peak


@pytest.mark.parametrize("name", [
    "CONIN$", "conout$.log", "COM¹.txt", "lpt³", "CON .txt",
])
def test_additional_windows_device_names_are_rejected_by_both_extractors(
    workspace, tmp_path, name,
):
    _zip(workspace / "dev.zip", [(name, b"x")])
    assert archive_tools.list_archive("dev.zip")["valid"] is False
    archive = _tar(tmp_path / "dev.tar", [(name, b"x")])
    with pytest.raises(ExtractionError):
        safe_extract(archive, tmp_path / "out", max_expanded_bytes=1024)


def test_unicode_normalization_collisions_are_rejected_by_both_extractors(
    workspace, tmp_path,
):
    composed = unicodedata.normalize("NFC", "café.txt")
    decomposed = unicodedata.normalize("NFD", "café.txt")
    assert composed != decomposed
    _zip(workspace / "nfc.zip", [(composed, b"a"), (decomposed, b"b")])
    listed = archive_tools.list_archive("nfc.zip")
    assert listed["valid"] is False
    assert "collid" in listed["errors"][0]
    archive = _tar(tmp_path / "nfc.tar", [(composed, b"a"), (decomposed, b"b")])
    with pytest.raises(ExtractionError, match="collid"):
        safe_extract(archive, tmp_path / "out", max_expanded_bytes=1024)


def test_update_staging_rejects_case_folded_component_collisions(tmp_path):
    archive = _tar(tmp_path / "cf.tar", [("Dir/a", b"1"), ("dir/b", b"2")])
    with pytest.raises(ExtractionError, match="collid"):
        safe_extract(archive, tmp_path / "out", max_expanded_bytes=1024)
    assert _tree(tmp_path / "out") == []


@pytest.mark.parametrize("rows", [
    [("a", b"1"), ("a/b", b"2")],
    [("a/b", b"2"), ("a", b"1")],
])
def test_update_staging_rejects_file_that_is_an_ancestor(tmp_path, rows):
    archive = _tar(tmp_path / "anc.tar", rows)
    with pytest.raises(ExtractionError, match="ancestor|collid"):
        safe_extract(archive, tmp_path / "out", max_expanded_bytes=1024)
    assert _tree(tmp_path / "out") == []


# ---------------------------------------------------------------------------
# Review round 2 (PR #548 @ eca9f597): GNU sparse, metadata chains, ZIP CD
# ---------------------------------------------------------------------------

import time

WALL_BUDGET_SECONDS = 10.0


def _gz_stream(path, blocks):
    with gzip.open(path, "wb", compresslevel=9) as stream:
        for block in blocks:
            stream.write(block)
        stream.write(b"\0" * 1024)
    return path


def _old_gnu_sparse_tar_gz(path, extension_blocks: int):
    # Type "S" member whose header sets isextended; each following 512-byte
    # extension block carries 21 (offset, numbytes) pairs and sets
    # isextended again, so tarfile keeps reading and appending to the map.
    header = bytearray(_raw_tar_header(b"sparse.bin", 0, b"S"))
    header[482] = 1  # isextended
    header[483:495] = b"%011o\0" % (1 << 30)  # realsize
    header[148:156] = b" " * 8
    header[148:156] = b"%06o\0 " % sum(header)

    def extension(last: bool) -> bytes:
        block = bytearray(512)
        for index in range(21):
            base = index * 24
            block[base:base + 12] = b"%011o\0" % (index + 1)
            block[base + 12:base + 24] = b"%011o\0" % 1
        block[504] = 0 if last else 1
        return bytes(block)

    middle = extension(False)

    def blocks():
        yield bytes(header)
        for _ in range(extension_blocks - 1):
            yield middle
        yield extension(True)

    return _gz_stream(path, blocks())


def _pax_record(key: str, value: str) -> bytes:
    body = " %s=%s\n" % (key, value)
    length = len(body) + 1
    while len(str(length)) + len(body) != length:
        length = len(str(length)) + len(body)
    return (str(length) + body).encode()


def _pax_block(records: bytes, typeflag: bytes = b"x") -> list:
    padded = records + b"\0" * ((-len(records)) % 512)
    return [_raw_tar_header(b"././@PaxHeader", len(records), typeflag), padded]


def _pax_sparse_10_tar_gz(path, map_numbers: int):
    # GNU sparse 1.0: the map lives at the start of the member's data and
    # its declared length drives an unbounded read-and-parse loop.
    records = b"".join((
        _pax_record("GNU.sparse.major", "1"),
        _pax_record("GNU.sparse.minor", "0"),
        _pax_record("GNU.sparse.name", "sparse.bin"),
        _pax_record("GNU.sparse.realsize", "1"),
    ))
    map_bytes = b"%d\n" % (map_numbers // 2) + b"0\n" * map_numbers
    map_bytes += b"\0" * ((-len(map_bytes)) % 512)

    def blocks():
        yield from _pax_block(records)
        yield _raw_tar_header(b"GNUSparseFile.0/sparse.bin", len(map_bytes), b"0")
        for start in range(0, len(map_bytes), 1 << 20):
            yield map_bytes[start:start + (1 << 20)]

    return _gz_stream(path, blocks())


def _chained_metadata_tar_gz(path, typeflag: bytes, links: int):
    if typeflag == b"g":
        one = b"".join(_pax_block(_pax_record("comment", "x" * 32), b"g"))
    else:
        payload = b"n" * 60 + b"\0"
        one = _raw_tar_header(b"././@LongLink", len(payload), typeflag)
        one += payload + b"\0" * ((-len(payload)) % 512)

    def blocks():
        for _ in range(links):
            yield one
        yield _raw_tar_header(b"payload.txt", 1, b"0")
        yield b"x" + b"\0" * 511

    return _gz_stream(path, blocks())


def _measure(callable_):
    """Return (peak traced bytes, seconds, exception or None)."""
    tracemalloc.start()
    started = time.perf_counter()
    error = None
    try:
        callable_()
    except BaseException as exc:  # RecursionError must be observed, not hidden
        error = exc
    elapsed = time.perf_counter() - started
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    return peak, elapsed, error


def _within_budget(peak, elapsed):
    return peak < PEAK_BUDGET_BYTES and elapsed < WALL_BUDGET_SECONDS


def _assert_all_readers_reject(workspace, tmp_path, source):
    name = source.name
    peak, elapsed, error = _measure(lambda: archive_tools.list_archive(name))
    assert error is None, repr(error)
    assert _within_budget(peak, elapsed), ("archive_list", peak, elapsed)
    assert archive_tools.list_archive(name)["valid"] is False

    peak, elapsed, error = _measure(lambda: archive_tools.extract_archive(
        name, "out", developer_authorized=True,
    ))
    assert isinstance(error, archive_tools.ArchiveRejected), repr(error)
    assert _within_budget(peak, elapsed), ("archive_extract", peak, elapsed)
    assert not (workspace / "out").exists()

    peak, elapsed, error = _measure(lambda: safe_extract(
        source, tmp_path / "stage", max_expanded_bytes=1 << 30,
    ))
    assert isinstance(error, ExtractionError), repr(error)
    assert _within_budget(peak, elapsed), ("safe_extract", peak, elapsed)

    peak, elapsed, error = _measure(lambda: file_ops._inspect_tar(source))
    assert isinstance(error, tarfile.TarError), repr(error)
    assert _within_budget(peak, elapsed), ("inspect", peak, elapsed)


def test_old_gnu_sparse_members_are_rejected_before_the_map_is_read(workspace, tmp_path):
    source = _old_gnu_sparse_tar_gz(workspace / "oldsparse.tar.gz", 4_000)
    assert source.stat().st_size < 64 * 1024
    _assert_all_readers_reject(workspace, tmp_path, source)


def test_pax_gnu_sparse_10_map_is_rejected_before_it_is_read(workspace, tmp_path):
    source = _pax_sparse_10_tar_gz(workspace / "paxsparse.tar.gz", 2_000_000)
    assert source.stat().st_size < 64 * 1024
    _assert_all_readers_reject(workspace, tmp_path, source)


@pytest.mark.parametrize("key", ["GNU.sparse.map", "GNU.sparse.size", "GNU.sparse.offset"])
def test_any_pax_gnu_sparse_key_is_rejected(workspace, tmp_path, key):
    records = _pax_record(key, "0,1") + _pax_record("GNU.sparse.name", "s.bin")
    source = _gz_stream(workspace / "paxkey.tar.gz", [
        *_pax_block(records),
        _raw_tar_header(b"s.bin", 1, b"0"),
        b"x" + b"\0" * 511,
    ])
    _assert_all_readers_reject(workspace, tmp_path, source)


def test_global_pax_gnu_sparse_key_is_rejected(workspace, tmp_path):
    source = _gz_stream(workspace / "globalsparse.tar.gz", [
        *_pax_block(_pax_record("GNU.sparse.major", "1"), b"g"),
        _raw_tar_header(b"s.bin", 1, b"0"),
        b"x" + b"\0" * 511,
    ])
    _assert_all_readers_reject(workspace, tmp_path, source)


@pytest.mark.parametrize("typeflag", [b"L", b"K", b"g"])
def test_chained_metadata_records_are_capped_not_recursed(workspace, tmp_path, typeflag):
    source = _chained_metadata_tar_gz(workspace / "chain.tar.gz", typeflag, 3_000)
    _assert_all_readers_reject(workspace, tmp_path, source)


def test_short_metadata_chains_still_parse(workspace, tmp_path):
    # A PAX header describing a long-named member is the normal shape; it
    # must keep working under the chain cap.
    name = "d/" + "n" * 150 + ".txt"
    source = workspace / "normal.tar"
    with tarfile.open(source, "w", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo(name)
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    gnu = workspace / "gnu.tar"
    with tarfile.open(gnu, "w", format=tarfile.GNU_FORMAT) as tar:
        info = tarfile.TarInfo(name)
        info.linkname = ""
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    # archive_tools rejects every PAX header by policy; GNU long names are
    # the shape it must keep accepting.
    assert archive_tools.list_archive(gnu.name)["valid"] is True
    for path in (source, gnu):
        assert safe_extract(path, tmp_path / path.stem, max_expanded_bytes=16) == 1
        assert file_ops._inspect_tar(path)["members"] == 1


def _zip_with_entries(path, count):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for index in range(count):
            archive.writestr("e%05d" % index, b"")
    return path


def test_zip_readers_check_central_directory_count_before_materializing(
    workspace, monkeypatch,
):
    source = _zip_with_entries(workspace / "many.zip", 300)
    parsed = []
    original = zipfile.ZipFile._RealGetContents

    def counting(self):
        parsed.append(self.filename)
        return original(self)

    monkeypatch.setattr(zipfile.ZipFile, "_RealGetContents", counting)

    result = archive_tools.list_archive("many.zip", max_entries=100)
    assert result["valid"] is False and "entry ceiling" in result["errors"][0]

    monkeypatch.setattr(file_ops, "INSPECT_MAX_ARCHIVE_MEMBERS", 100)
    preview = file_ops._inspect_zip(source)
    assert preview["truncated"] is True and preview["members"] == 300

    from sonder_runtime.adapters import artifact_grounding

    monkeypatch.setattr(artifact_grounding, "MAX_OOXML_ENTRIES", 100)
    checks = []
    artifact_grounding._validate_ooxml(source, "docx", {}, checks)
    assert checks and checks[-1]["name"] == "ooxml-entry-limit", checks
    assert checks[-1]["ok"] is False

    assert parsed == [], "a ZIP reader parsed the central directory before the count check"


def test_path_archive_safety_inspect_tar_uses_bounded_reader(tmp_path):
    from sonder_runtime.application.security import path_archive_safety

    for source in (
        _chained_metadata_tar_gz(tmp_path / "chain.tar.gz", b"L", 3_000),
        _pax_sparse_10_tar_gz(tmp_path / "sparse.tar.gz", 2_000_000),
    ):
        peak, elapsed, error = _measure(lambda: path_archive_safety.inspect_tar(source))
        assert isinstance(
            error, (tarfile.TarError, path_archive_safety.ArchiveLimitError),
        ), repr(error)
        assert _within_budget(peak, elapsed), (source.name, peak, elapsed)
