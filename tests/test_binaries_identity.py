"""domain/binaries: PE/RSDS, PDB MSF 7.0 and ELF build-id identity readers."""
from __future__ import annotations

import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from sonder_runtime.domain.binaries.elf_ids import read_elf_identity
from sonder_runtime.domain.binaries.pdb_info import (
    MSF7_MAGIC, pdb_match_basis, pdb_matches, read_pdb_identity,
)
from sonder_runtime.domain.binaries.pe_debug import read_pe_identity
from sonder_runtime.domain.binaries.reader import (
    BinaryFormatError, BudgetedReader, ByteRangeError, BytesReader, ReadBudgetExceeded, check_range,
)
from sonder_runtime.domain.binaries.symstore import (
    breakpad_debug_id, elf_debug_path, format_guid, guid_hex, pe_symbol_key,
)
from sonder_runtime.domain.common.errors import InvalidInput


PE = Path(__file__).parent / "fixtures" / "crash" / "pe"
GUID_A = "EC756AB6-D819-44D7-4C4C-44205044422E"
GUID_B = "07C7C727-1A90-3F7C-4C4C-44205044422E"


def _reader(path: Path) -> BytesReader:
    return BytesReader(path.read_bytes())


# ------------------------------------------------------------------ reader

def test_check_range_explicit_bounds():
    check_range(0, 4, 4)
    check_range(4, 0, 4)
    for offset, length, size in ((1, 4, 4), (-1, 1, 4), (0, -1, 4), (5, 0, 4), (2**63, 2**63, 10)):
        with pytest.raises(ByteRangeError):
            check_range(offset, length, size)


def test_bytes_reader_exact_and_budget():
    reader = BytesReader(b"abcdef")
    assert reader.read(2, 3) == b"cde"
    with pytest.raises(ByteRangeError):
        reader.read(4, 3)
    budget = BudgetedReader(reader, 4)
    assert budget.read(0, 4) == b"abcd"
    with pytest.raises(ReadBudgetExceeded) as info:
        budget.read(0, 1)
    assert info.value.code == "LIMIT_EXCEEDED"


# ------------------------------------------------------------------ PE / PDB

def test_pe_identity_from_clang_cl_fixture():
    identity = read_pe_identity(_reader(PE / "a" / "spark_tiny.exe"))
    assert identity.machine == "AMD64" and identity.machine_code == 0x8664
    assert identity.pe32_plus
    assert identity.rsds_guid == GUID_A
    assert identity.rsds_age == 1
    assert identity.pdb_path == "C:\\build\\out\\spark_tiny.pdb"
    assert identity.pdb_basename == "spark_tiny.pdb"
    # Leaf-only program: lld emits no .pdata, so the exception directory is empty.
    assert identity.size_of_image == 0x3000 and not identity.has_pdata and identity.sections == 2


def test_pdb_identity_matches_llvm_pdbutil():
    a = read_pdb_identity(_reader(PE / "a" / "spark_tiny.pdb"))
    b = read_pdb_identity(_reader(PE / "b" / "spark_tiny.pdb"))
    assert a.guid == GUID_A and a.pdb_age == 1 and a.dbi_age == 1 and a.block_size == 4096
    assert b.guid == GUID_B


def test_pdb_matches_pair_and_rejects_cross_built_pair():
    exe_a = read_pe_identity(_reader(PE / "a" / "spark_tiny.exe"))
    exe_b = read_pe_identity(_reader(PE / "b" / "spark_tiny.exe"))
    pdb_a = read_pdb_identity(_reader(PE / "a" / "spark_tiny.pdb"))
    pdb_b = read_pdb_identity(_reader(PE / "b" / "spark_tiny.pdb"))
    assert pdb_matches(exe_a, pdb_a) and pdb_matches(exe_b, pdb_b)
    assert not pdb_matches(exe_a, pdb_b) and not pdb_matches(exe_b, pdb_a)
    assert pdb_match_basis(exe_a, pdb_a) == "dbi_age"


def test_pdb_age_rule_uses_dbi_age_and_pdb_age_only_without_dbi():
    from dataclasses import replace

    exe = read_pe_identity(_reader(PE / "a" / "spark_tiny.exe"))
    pdb = read_pdb_identity(_reader(PE / "a" / "spark_tiny.pdb"))
    assert pdb_match_basis(exe, replace(pdb, dbi_age=None)) == "pdb_age"
    # A DBI age that disagrees with RSDS is a mismatch even when the
    # PDB-stream age happens to equal it (stale PDB after an incremental link).
    assert pdb_match_basis(exe, replace(pdb, dbi_age=7)) is None
    assert not pdb_matches(exe, replace(pdb, dbi_age=7))
    assert pdb_match_basis(exe, replace(pdb, dbi_age=7, pdb_age=9)) is None
    assert pdb_match_basis(replace(exe, rsds_age=2), replace(pdb, dbi_age=2, pdb_age=1)) == "dbi_age"


def _msf(block_size=512, num_blocks=8, dir_bytes=8, block_map=3, dir_block=4, stream_count=0,
         total_blocks=8) -> bytearray:
    data = bytearray(block_size * total_blocks)
    data[:32] = MSF7_MAGIC
    struct.pack_into("<IIIIII", data, 32, block_size, 1, num_blocks, dir_bytes, 0, block_map)
    if block_map:
        struct.pack_into("<I", data, block_map * block_size, dir_block)
    if dir_block < total_blocks:
        struct.pack_into("<I", data, dir_block * block_size, stream_count)
    return data


@pytest.mark.parametrize("mutate,code", [
    (dict(block_size=3000), "OUT_OF_BOUNDS"),
    (dict(num_blocks=0x7FFFFFFF), "TRUNCATED"),
    (dict(dir_block=99), "OUT_OF_BOUNDS"),
    (dict(block_map=0), "OUT_OF_BOUNDS"),
    (dict(dir_bytes=0), "TRUNCATED"),
    (dict(dir_bytes=512 * 5000), "LIMIT_EXCEEDED"),
    (dict(stream_count=0xFFFFFFFF), "LIMIT_EXCEEDED"),
])
def test_malformed_pdb_codes(mutate, code):
    params = dict(block_size=512, num_blocks=8, dir_bytes=8, block_map=3, dir_block=4, stream_count=0)
    params.update(mutate)
    data = _msf(**params) if params["block_size"] != 3000 else _msf_raw_bs(3000)
    with pytest.raises(BinaryFormatError) as info:
        read_pdb_identity(BytesReader(bytes(data)))
    assert info.value.code == code


def _msf_raw_bs(block_size: int) -> bytearray:
    data = bytearray(4096 * 4)
    data[:32] = MSF7_MAGIC
    struct.pack_into("<IIIIII", data, 32, block_size, 1, 4, 8, 0, 1)
    return data


def test_pdb_not_msf_and_truncated():
    with pytest.raises(BinaryFormatError) as info:
        read_pdb_identity(BytesReader(b"Microsoft C/C++ program database 2.00\r\n" + b"\0" * 64))
    assert info.value.code == "NOT_PDB"
    real = (PE / "a" / "spark_tiny.pdb").read_bytes()
    with pytest.raises(BinaryFormatError) as info:
        read_pdb_identity(BytesReader(real[: len(real) // 2]))
    assert info.value.code in {"TRUNCATED", "OUT_OF_BOUNDS"}


def test_pe_malformed_codes():
    real = bytearray((PE / "a" / "spark_tiny.exe").read_bytes())
    with pytest.raises(BinaryFormatError) as info:
        read_pe_identity(BytesReader(b"ZZ" + bytes(real[2:])))
    assert info.value.code == "NOT_PE"
    broken = bytearray(real)
    struct.pack_into("<I", broken, 0x3C, 0xFFFFFF00)
    with pytest.raises(BinaryFormatError) as info:
        read_pe_identity(BytesReader(bytes(broken)))
    assert info.value.code == "TRUNCATED"
    with pytest.raises(BinaryFormatError):
        read_pe_identity(BytesReader(bytes(real[:0x90])))


# ------------------------------------------------------------------ ELF

def _elf_with_note_only(build_id: bytes, *, machine: int = 62) -> bytes:
    """A stripped-style ELF64: program headers only (PT_NOTE), no sections."""
    note = struct.pack("<III", 4, len(build_id), 3) + b"GNU\0" + build_id
    note += b"\0" * (-len(note) % 4)
    ehdr = bytearray(64)
    ehdr[:16] = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    struct.pack_into("<HHIQQQIHHHHHH", ehdr, 16, 3, machine, 1, 0, 64, 0, 0, 64, 56, 1, 64, 0, 0)
    phdr = struct.pack("<IIQQQQQQ", 4, 4, 120, 120, 120, len(note), len(note), 4)
    return bytes(ehdr) + phdr + note


def test_elf_build_id_from_pt_note_without_sections():
    identity = read_elf_identity(BytesReader(_elf_with_note_only(bytes(range(20)))))
    assert identity.build_id == bytes(range(20)).hex()
    assert identity.machine == "x86_64" and identity.type == "dyn" and identity.debuglink == ""


def test_elf_pn_xnum_header_uses_section_zero():
    note = struct.pack("<III", 4, 8, 3) + b"GNU\0" + b"\xaa" * 8
    ehdr = bytearray(64)
    ehdr[:16] = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    phoff, shoff = 64, 64 + 56
    struct.pack_into("<HHIQQQIHHHHHH", ehdr, 16, 3, 183, 1, 0, phoff, shoff, 0, 64, 56, 0xFFFF, 64, 1, 0)
    note_off = shoff + 64
    phdr = struct.pack("<IIQQQQQQ", 4, 4, note_off, 0, 0, len(note), len(note), 4)
    section0 = struct.pack("<IIQQQQIIQQ", 0, 0, 0, 0, 0, 0, 0, 1, 0, 0)
    identity = read_elf_identity(BytesReader(bytes(ehdr) + phdr + section0 + note))
    assert identity.build_id == "aa" * 8 and identity.machine == "aarch64"


def test_elf_rejects_garbage():
    with pytest.raises(BinaryFormatError) as info:
        read_elf_identity(BytesReader(b"\x7fELF\x09" + b"\0" * 80))
    assert info.value.code == "NOT_ELF"


@pytest.mark.skipif(shutil.which("g++") is None or shutil.which("strip") is None
                    or shutil.which("readelf") is None, reason="needs g++, strip and readelf")
def test_elf_build_id_of_real_stripped_binary(tmp_path):
    source = tmp_path / "t.cpp"
    source.write_text("int main() { return 0; }\n")
    binary = tmp_path / "t"
    subprocess.run(["g++", "-Wl,--build-id", "-o", str(binary), str(source)], check=True, timeout=120)
    subprocess.run(["strip", "--strip-all", "--remove-section=.note.gnu.build-id", str(binary)],
                   check=True, timeout=60)
    identity = read_elf_identity(BytesReader(binary.read_bytes()))
    readelf = subprocess.run(["readelf", "-n", str(binary)], capture_output=True, text=True, timeout=60).stdout
    if "Build ID" in readelf:
        assert identity.build_id in readelf
    assert identity.type in {"dyn", "exec"}


# ------------------------------------------------------------------ symstore

def test_symstore_keys():
    raw = bytes.fromhex("b66a75ec19d8d7444c4c44205044422e")
    assert format_guid(raw) == GUID_A
    assert guid_hex("{%s}" % GUID_A) == "EC756AB6D81944D74C4C44205044422E"
    assert pe_symbol_key("spark_tiny.pdb", GUID_A, 1) == \
        "spark_tiny.pdb/EC756AB6D81944D74C4C44205044422E1/spark_tiny.pdb"
    assert pe_symbol_key("C:\\build\\out\\spark_tiny.pdb", GUID_A, 0x1A).split("/")[1].endswith("1A")
    assert breakpad_debug_id(GUID_A, 1) == "EC756AB6D81944D74C4C44205044422E1"
    assert elf_debug_path("7F88EDF6") == ".build-id/7f/88edf6.debug"
    for bad in ("..", ".pdb", "a;b.pdb", "", "C:\\x\\"):
        with pytest.raises(InvalidInput):
            pe_symbol_key(bad, GUID_A, 1)
    with pytest.raises(InvalidInput):
        elf_debug_path("abc")


def test_elf_identity_charges_repeated_note_reads_to_a_budget():
    # 65535 section headers that all name the same 1 MiB SHT_NOTE (one big
    # non-GNU note): without a read budget this is 64 GiB of reads.
    note_size = 1 << 20
    note = struct.pack("<III", 0, note_size - 12, 1) + b"\0" * (note_size - 12)
    shnum = 65535
    phoff, note_off = 64, 64
    shoff = note_off + note_size
    header = b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\0" * 8 + struct.pack(
        "<HHIQQQIHHHHHH", 2, 62, 1, 0, 0, shoff, 0, 64, 56, 0, 64, shnum, 0)
    section = struct.pack("<IIQQQQIIQQ", 0, 7, 0, 0, note_off, note_size, 0, 0, 4, 0)
    data = header + note + section * shnum
    assert len(header) == phoff
    with pytest.raises(BinaryFormatError) as info:
        read_elf_identity(BytesReader(data))
    assert info.value.code == "LIMIT_EXCEEDED"
