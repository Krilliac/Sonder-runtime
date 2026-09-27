"""Guarded capture access: sniffing, exact reads, identity, refusals, staging."""
from __future__ import annotations

import os
import stat
import struct

import pytest

from sonder_runtime.adapters.debugging import capture_source as cs
from sonder_runtime.adapters.debugging.capture_source import (
    FileByteReader,
    GuardedCaptureSource,
    IdentityCache,
    sniff_kind,
    stream_sha256,
)
from sonder_runtime.domain.common.errors import SonderError

pytestmark = pytest.mark.unit


@pytest.fixture
def root(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(allowed))
    return allowed


def _code(excinfo) -> str:
    return getattr(excinfo.value, "code", "")


def minidump_header(stream_types) -> bytes:
    count = len(stream_types)
    header = struct.pack("<4sIIIIIQ", b"MDMP", 0xA793, count, 32, 0, 0, 0)
    directory = b"".join(struct.pack("<III", kind, 0, 0) for kind in stream_types)
    return header + directory + b"\x00" * 64


def elf_header(e_type: int) -> bytes:
    ident = b"\x7fELF" + bytes([2, 1, 1]) + b"\x00" * 9
    return ident + struct.pack("<HHI", e_type, 62, 1) + b"\x00" * 48


class _Reader:
    def __init__(self, data):
        self.data = data

    @property
    def size(self):
        return len(self.data)

    def read(self, offset, length):
        if offset < 0 or offset + length > len(self.data):
            raise ValueError("range")
        return self.data[offset:offset + length]


@pytest.mark.parametrize("data,name,kind", [
    (minidump_header([3, 4, 6]), "a.dmp", "windows_minidump"),
    (minidump_header([3, 4, 0x43500001]), "a.dmp", "crashpad_minidump"),
    (minidump_header([3, 0x47670001]), "a.dmp", "breakpad_minidump"),
    (minidump_header([3, 0x47670005]), "a.dmp", "breakpad_minidump"),
    (elf_header(4), "core", "elf_core"),
    (elf_header(2), "app", "unknown"),
    (b"PERFILE2" + b"\x00" * 100, "perf.data", "perf_data"),
    (b"tlZ\x04" + b"\x00" * 20, "x.bin", "tracy_capture"),
    (b"\x00\x01", "capture.tracy", "tracy_capture"),
    (b"\x00" * 64, "trace.etl", "etw_etl"),
    (b"\x28\xb5\x2f\xfd" + b"\x00" * 30, "heaptrack.game.1234.zst", "heaptrack_capture"),
    (b"\x1f\x8b\x08" + b"\x00" * 30, "heaptrack.game.1234.gz", "heaptrack_capture"),
    (b"\x28\xb5\x2f\xfd" + b"\x00" * 30, "other.zst", "unknown"),
    (b"\x0a\x80\x01" + bytes(range(1, 30)) * 3, "trace.pftrace", "perfetto_protobuf"),
    (b'<?xml version="1.0"?>\n<valgrindoutput>\n', "vg.xml", "valgrind_xml"),
    (b'{"app_name":"Game","bug_type":"309"}\n{"x":1}', "Game.ips", "apple_ips"),
    (b'{"traceEvents":[{"ph":"X","name":"Frame"}]}', "t.json", "chrome_trace"),
    (b'[{"ph":"B","name":"a","ts":1}]', "t.json", "chrome_trace"),
    (b'{"hello": 1}', "x.json", "unknown"),
    (b"==123==ERROR: AddressSanitizer: heap-use-after-free on address", "asan.log", "sanitizer_report"),
    (b"a.cpp:3:5: runtime error: signed integer overflow", "ubsan.log", "sanitizer_report"),
    (b"WARNING: ThreadSanitizer: data race (pid=1)", "tsan.log", "sanitizer_report"),
    (b"# callgrind format\nversion: 1\ncreator: callgrind-3.22\n", "callgrind.out.1", "callgrind"),
    (b"version: 1\ncreator: callgrind-3.22\nevents: Ir\n", "cg", "callgrind"),
    (b"reading file ...\nMOST CALLS TO ALLOCATION FUNCTIONS\n", "ht.txt", "heaptrack_text"),
    (b"# To display the perf.data header info, please use --header\n# Samples: 1K\n", "r.txt",
     "perf_text"),
    (b"main;Physics::Integrate;dot 120\nmain;Render 30\n", "stacks.folded", "perf_text"),
    (b"name,src_file,src_line,total_ns,total_perc,counts\n", "zones.csv", "tracy_csv"),
    (b"Function,Weight,% Weight\n", "wpa.csv", "profile_csv"),
    (b"hello world\n", "notes.txt", "unknown"),
    (b"\x00\x01\x02\x03" * 40, "blob.bin", "unknown"),
])
def test_sniff_kind(data, name, kind):
    assert sniff_kind(data[:65536], name, _Reader(data)) == kind


def test_a_minidump_with_a_lying_directory_still_sniffs_as_windows():
    data = struct.pack("<4sIIIIIQ", b"MDMP", 0xA793, 10_000_000, 0xFFFFFFF0, 0, 0, 0)
    assert sniff_kind(data, "x.dmp", _Reader(data)) == "windows_minidump"


# -- exact reads -----------------------------------------------------------------


@pytest.mark.parametrize("use_pread", [True, False])
def test_file_reader_returns_exact_ranges_or_raises(tmp_path, use_pread):
    path = tmp_path / "blob"
    path.write_bytes(bytes(range(256)) * 4)
    with open(path, "rb") as handle:
        reader = FileByteReader(handle, 1024, use_pread=use_pread and hasattr(os, "pread"))
        assert reader.read(10, 4) == bytes([10, 11, 12, 13])
        assert reader.read(1020, 4) == bytes([252, 253, 254, 255])
        assert reader.read(0, 0) == b""
        for offset, length in ((1021, 4), (-1, 2), (0, 1025), (2000, 1), (0, -1)):
            with pytest.raises((ValueError, SonderError)):
                reader.read(offset, length)
        assert reader.bytes_read == 8
        reader.close()
        with pytest.raises((ValueError, SonderError)):
            reader.read(0, 1)


def test_file_reader_short_read_is_an_error(tmp_path):
    path = tmp_path / "blob"
    path.write_bytes(b"x" * 10)
    with open(path, "rb") as handle:
        reader = FileByteReader(handle, 100)  # claims more than the file holds
        with pytest.raises((ValueError, SonderError)):
            reader.read(5, 20)


# -- identity -----------------------------------------------------------------------


def test_identity_is_hashed_once_per_file_version(root):
    capture = root / "core.1"
    capture.write_bytes(elf_header(4) + b"\x00" * 4096)
    source = GuardedCaptureSource()
    reader, first = source.open_reader(str(capture))
    reader.close()
    reader, second = source.open_reader(str(capture))
    reader.close()
    assert first.sha256 == second.sha256 and len(first.sha256) == 64
    assert source.identity_cache.hits == 1 and source.identity_cache.misses == 1
    assert first.kind == "elf_core" and first.label.endswith("core.1")
    capture.write_bytes(elf_header(4) + b"\x01" * 4096)
    os.utime(capture, ns=(first.mtime_ns + 5_000_000, first.mtime_ns + 5_000_000))
    reader, third = source.open_reader(str(capture))
    reader.close()
    assert third.sha256 != first.sha256


def test_identity_cache_expires():
    now = [0.0]
    cache = IdentityCache(clock=lambda: now[0], ttl_seconds=600)
    cache.put((1, 2, 3, 4), "sha")
    now[0] = 599
    assert cache.get((1, 2, 3, 4)) == "sha"
    now[0] = 1300
    assert cache.get((1, 2, 3, 4)) is None


def test_hashing_past_the_budget_gives_a_partial_identity(tmp_path):
    path = tmp_path / "big"
    path.write_bytes(b"a" * (3 << 20))
    ticks = iter(range(0, 1000, 3))
    with open(path, "rb") as handle:
        value = stream_sha256(handle, 3 << 20, clock=lambda: float(next(ticks)), max_seconds=5.0)
    assert value.startswith("partial:") and value.endswith("+%d" % (3 << 20))
    with open(path, "rb") as handle:
        assert len(stream_sha256(handle, 3 << 20)) == 64


# -- refusals -------------------------------------------------------------------------


def test_symlinks_are_refused(root, tmp_path):
    target = root / "real.dmp"
    target.write_bytes(minidump_header([3]))
    link = root / "link.dmp"
    link.symlink_to(target)
    with pytest.raises(SonderError) as caught:
        GuardedCaptureSource().open_reader(str(link))
    assert _code(caught) == "CAPTURE_REJECTED"
    linked_dir = root / "dir-link"
    linked_dir.symlink_to(root)
    with pytest.raises(SonderError):
        GuardedCaptureSource().open_reader(str(linked_dir / "real.dmp"))


def test_paths_outside_the_roots_are_refused(root, tmp_path):
    outside = tmp_path / "outside.dmp"
    outside.write_bytes(minidump_header([3]))
    with pytest.raises(SonderError) as caught:
        GuardedCaptureSource().open_reader(str(outside))
    assert _code(caught) == "CAPTURE_REJECTED"
    with pytest.raises(SonderError):
        GuardedCaptureSource().open_reader(str(root / ".." / "outside.dmp"))


@pytest.mark.parametrize("relative", [".env", ".env.local", "server.pem", ".ssh/crash.dmp",
                                      ".aws/credentials", ".git/config"])
def test_secret_and_credential_files_are_refused(root, relative):
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"==1==ERROR: AddressSanitizer: x\n")
    with pytest.raises(SonderError) as caught:
        GuardedCaptureSource().open_reader(str(target))
    assert _code(caught) == "CAPTURE_REJECTED"


def test_fifos_and_devices_are_refused_without_blocking(root):
    fifo = root / "trap.dmp"
    os.mkfifo(fifo)
    with pytest.raises(SonderError) as caught:
        GuardedCaptureSource().open_reader(str(fifo))
    assert _code(caught) == "CAPTURE_REJECTED"
    if os.geteuid() == 0:
        device = root / "null.dmp"
        os.mknod(device, stat.S_IFCHR | 0o600, os.makedev(1, 3))
        with pytest.raises(SonderError):
            GuardedCaptureSource().open_reader(str(device))


def test_oversized_captures_are_refused_by_kind(root, monkeypatch):
    log = root / "asan.log"
    log.write_bytes(b"==1==ERROR: AddressSanitizer: x\n" + b"y" * 5000)
    monkeypatch.setattr(cs, "TEXT_CAP_BYTES", 1000)
    with pytest.raises(SonderError) as caught:
        GuardedCaptureSource().open_reader(str(log))
    assert _code(caught) == "CAPTURE_TOO_LARGE"
    dump = root / "a.dmp"
    dump.write_bytes(minidump_header([3]) + b"\x00" * 5000)
    reader, ident = GuardedCaptureSource().open_reader(str(dump))  # 8 GiB cap for dumps
    reader.close()
    with pytest.raises(SonderError):
        GuardedCaptureSource().open_reader(str(dump), max_bytes=100)
    assert cs.size_cap("elf_core") == 8 << 30 and cs.size_cap("perf_data") == 4 << 30
    assert cs.size_cap("chrome_trace") == 1 << 30


def test_a_capture_changed_while_it_is_read_is_input_changed(root):
    log = root / "asan.log"
    log.write_bytes(b"==1==ERROR: AddressSanitizer: x\n")
    reader, _ = GuardedCaptureSource().open_reader(str(log))
    with open(log, "ab") as handle:
        handle.write(b"appended by a writer\n")
    with pytest.raises(SonderError) as caught:
        reader.close()
    assert _code(caught) == "INPUT_CHANGED"


# -- directories ----------------------------------------------------------------------------


def test_directory_listing_is_capped_flat_and_regular_files_only(root):
    folder = root / "dumps"
    folder.mkdir()
    for index in range(65):
        (folder / ("crash_%02d.dmp" % index)).write_bytes(minidump_header([3]))
    (folder / "nested").mkdir()
    (folder / "nested" / "deep.dmp").write_bytes(minidump_header([3]))
    (folder / "zz-link.dmp").symlink_to(folder / "crash_00.dmp")
    (folder / ".env").write_text("SECRET=1")
    os.mkfifo(folder / "zz-fifo.dmp")
    source = GuardedCaptureSource()
    assert source.is_dir(str(folder)) and not source.is_dir(str(folder / "crash_00.dmp"))
    listed = source.list_dir(str(folder), max_files=64)
    assert len(listed) == 64
    names = [os.path.basename(item.path) for item in listed]
    assert names == sorted(names) and names[0] == "crash_00.dmp"
    assert not any(name in ("nested", "zz-link.dmp", ".env", "zz-fifo.dmp") for name in names)
    assert len(source.list_dir(str(folder), max_files=500)) == 64


def test_directories_outside_the_roots_are_refused(root, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    source = GuardedCaptureSource()
    assert not source.is_dir(str(outside))
    with pytest.raises(SonderError):
        source.list_dir(str(outside))
    link = root / "linked"
    link.symlink_to(outside)
    assert not source.is_dir(str(link))


# -- staging ----------------------------------------------------------------------------------


def _open(source, path):
    reader, ident = source.open_reader(str(path))
    reader.close()
    return ident


def test_copy_staging_is_private_and_exact(root, tmp_path):
    capture = root / "core.1"
    capture.write_bytes(elf_header(4) + os.urandom(2048))
    source = GuardedCaptureSource()
    ident = _open(source, capture)
    rundir = tmp_path / "run"
    (rundir / "in").mkdir(parents=True)
    staged = source.stage(ident, str(rundir), strategy="copy")
    assert open(staged, "rb").read() == capture.read_bytes()
    assert stat.S_IMODE(os.stat(staged).st_mode) == 0o600
    assert os.stat(staged).st_ino != ident.ino


def test_a_capture_replaced_after_planning_is_input_changed(root, tmp_path):
    capture = root / "core.1"
    capture.write_bytes(elf_header(4) + b"a" * 100)
    source = GuardedCaptureSource()
    ident = _open(source, capture)
    replacement = root / "core.new"
    replacement.write_bytes(elf_header(4) + b"b" * 100)
    os.replace(replacement, capture)
    rundir = tmp_path / "run"
    (rundir / "in").mkdir(parents=True)
    for strategy in ("copy", "path", "hardlink"):
        with pytest.raises(SonderError) as caught:
            source.stage(ident, str(rundir), strategy=strategy, dest_name="c-" + strategy)
        assert _code(caught) == "INPUT_CHANGED", strategy
    assert os.listdir(rundir / "in") == []


def test_hardlink_staging_pins_the_inode_against_a_rename_swap(root, tmp_path):
    capture = root / "core.1"
    capture.write_bytes(elf_header(4) + b"a" * 100)
    source = GuardedCaptureSource()
    ident = _open(source, capture)
    rundir = tmp_path / "run"
    (rundir / "in").mkdir(parents=True)
    staged = source.stage(ident, str(rundir), strategy="hardlink")
    assert os.stat(staged).st_ino == ident.ino
    swap = root / "evil"
    swap.write_bytes(b"evil")
    os.replace(swap, capture)  # the debugger still reads the pinned inode
    assert open(staged, "rb").read().startswith(b"\x7fELF")
    current = source.current(ident)
    assert current is not None and not current.same_file(ident)


def test_path_staging_passes_the_canonical_path_when_unchanged(root, tmp_path):
    capture = root / "core.1"
    capture.write_bytes(elf_header(4))
    source = GuardedCaptureSource()
    ident = _open(source, capture)
    assert source.stage(ident, str(tmp_path), strategy="path") == ident.path


def test_staging_strategy_by_size_and_device(root, tmp_path):
    ident = cs.CaptureIdentity("/x", "x", 10, os.stat(tmp_path).st_dev, 1, 1, "s", "elf_core")
    assert GuardedCaptureSource.staging_for(ident, str(tmp_path)) == "copy"
    big = cs.CaptureIdentity("/x", "x", cs.COPY_STAGING_MAX_BYTES + 1, os.stat(tmp_path).st_dev,
                             1, 1, "s", "elf_core")
    assert GuardedCaptureSource.staging_for(big, str(tmp_path)) == "hardlink"
    other = cs.CaptureIdentity("/x", "x", cs.COPY_STAGING_MAX_BYTES + 1, -1, 1, 1, "s", "elf_core")
    assert GuardedCaptureSource.staging_for(other, str(tmp_path)) == "path"


def test_symbol_files_are_staged_through_the_same_guard(root, tmp_path):
    pdb = root / "game.pdb"
    pdb.write_bytes(b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\x00\x00\x00" + b"\x00" * 64)
    source = GuardedCaptureSource()
    dest = tmp_path / "sym" / "game" / "game.pdb"
    dest.parent.mkdir(parents=True)
    source.stage_file(str(pdb), str(dest))
    assert dest.read_bytes() == pdb.read_bytes()
    link = root / "link.pdb"
    link.symlink_to(pdb)
    with pytest.raises((SonderError, OSError, PermissionError)):
        source.stage_file(str(link), str(tmp_path / "other.pdb"))


@pytest.mark.parametrize("name,staged", [
    ("core.1", "capture.1"),
    ("game.dmp", "capture.dmp"),
    ("core.{nonce}", "capture.bin"),
    ("core.-ex shell touch x", "capture.bin"),
    ("core.$(id)", "capture.bin"),
    ("core", "capture.bin"),
])
def test_the_staged_name_keeps_only_a_plain_suffix(root, tmp_path, name, staged):
    """The staged path is bound into debugger argv; a hostile suffix must not ride along."""
    capture = root / name
    capture.write_bytes(elf_header(4) + b"a" * 64)
    source = GuardedCaptureSource()
    ident = _open(source, capture)
    rundir = tmp_path / "run"
    (rundir / "in").mkdir(parents=True)
    assert os.path.basename(source.stage(ident, str(rundir), strategy="copy")) == staged


@pytest.mark.parametrize("path", ["{rundir}", "sym/{nonce}/x", "a{input}"])
def test_symbol_dirs_spelling_a_placeholder_are_rejected(root, path):
    from sonder_runtime.adapters.debugging.symbol_dirs import contain_symbol_dir

    target = root / path
    target.mkdir(parents=True)
    with pytest.raises(SonderError) as caught:
        contain_symbol_dir(str(target), system="Linux")
    assert _code(caught) == "SYMBOL_PATH_REJECTED"
