"""PE (PE32/PE32+) identity: machine, timestamp, image size and CodeView RSDS.

Reads only headers, the section table, the debug data directory (index 6)
and the exception directory (index 3, for ``has_pdata``). The RSDS record's
PDB path is binary-controlled (it may be a UNC path); it is reported,
clipped to 260 characters, and never used to open anything.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from ..diagnostics.model import clean_text
from .reader import BinaryFormatError, ByteRangeError, ByteReader, WallClock, c_string, unpack
from .symstore import format_guid


MACHINES = {
    0x014C: "I386",
    0x8664: "AMD64",
    0xAA64: "ARM64",
    0xA641: "ARM64EC",
    0xA64E: "ARM64X",
    0x01C4: "ARMNT",
}
MAX_SECTIONS = 96
MAX_DEBUG_ENTRIES = 32
MAX_PDB_PATH_CHARS = 260
IMAGE_DEBUG_TYPE_CODEVIEW = 2


@dataclass(frozen=True, slots=True)
class PeIdentity:
    machine: str
    timestamp: int
    size_of_image: int
    rsds_guid: str | None
    rsds_age: int | None
    pdb_path: str
    pdb_basename: str
    has_pdata: bool
    machine_code: int = 0
    pe32_plus: bool = False
    image_base: int = 0
    sections: int = 0
    characteristics: int = 0


def _rva_to_offset(rva: int, sections: list[tuple[int, int, int, int]]) -> int | None:
    for virtual_address, virtual_size, raw_offset, raw_size in sections:
        span = max(virtual_size, raw_size)
        if virtual_address <= rva < virtual_address + span:
            delta = rva - virtual_address
            if delta >= raw_size:
                return None
            return raw_offset + delta
    return None


def pdb_basename(path: str) -> str:
    return str(path or "").replace("\\", "/").rsplit("/", 1)[-1]


def parse_rsds(record: bytes) -> tuple[str, int, str] | None:
    """(guid, age, pdb_path) from a CodeView RSDS record, else None."""
    if len(record) < 24 or record[:4] != b"RSDS":
        return None
    guid = format_guid(record[4:20])
    age = struct.unpack_from("<I", record, 20)[0]
    path = c_string(record[24:], MAX_PDB_PATH_CHARS * 4).decode("utf-8", errors="replace")
    return guid, age, clean_text(path, MAX_PDB_PATH_CHARS)


def read_pe_identity(reader: ByteReader, *, max_seconds: float = 2.0) -> PeIdentity:
    """Parse a PE image's identity. Raises ``BinaryFormatError``."""
    clock = WallClock(max_seconds)
    if reader.size < 0x40:
        raise BinaryFormatError("NOT_PE", "too small for a DOS header")
    if reader.read(0, 2) != b"MZ":
        raise BinaryFormatError("NOT_PE", "missing MZ")
    (pe_offset,) = unpack(reader, 0x3C, "<I")
    try:
        signature = reader.read(pe_offset, 4)
    except ByteRangeError:
        raise BinaryFormatError("TRUNCATED", "PE header offset beyond the file") from None
    if signature != b"PE\x00\x00":
        raise BinaryFormatError("NOT_PE", "missing PE signature")
    machine, n_sections, timestamp, _symtab, _nsyms, opt_size, characteristics = unpack(
        reader, pe_offset + 4, "<HHIIIHH")
    if n_sections > MAX_SECTIONS:
        raise BinaryFormatError("LIMIT_EXCEEDED", "too many sections")
    optional = pe_offset + 24
    if opt_size < 2:
        raise BinaryFormatError("TRUNCATED", "no optional header")
    (magic,) = unpack(reader, optional, "<H")
    if magic == 0x10B:
        pe32_plus = False
        if opt_size < 96:
            raise BinaryFormatError("TRUNCATED", "PE32 optional header too small")
        (image_base,) = unpack(reader, optional + 28, "<I")
        dir_count_off, dirs = optional + 92, optional + 96
    elif magic == 0x20B:
        pe32_plus = True
        if opt_size < 112:
            raise BinaryFormatError("TRUNCATED", "PE32+ optional header too small")
        (image_base,) = unpack(reader, optional + 24, "<Q")
        dir_count_off, dirs = optional + 108, optional + 112
    else:
        raise BinaryFormatError("NOT_PE", "unknown optional-header magic")
    (size_of_image,) = unpack(reader, optional + 56, "<I")
    (dir_count,) = unpack(reader, dir_count_off, "<I")
    dir_count = min(dir_count, 16, max(0, (optional + opt_size - dirs) // 8))

    def directory(index: int) -> tuple[int, int]:
        if index >= dir_count:
            return 0, 0
        return unpack(reader, dirs + index * 8, "<II")

    table = optional + opt_size
    sections: list[tuple[int, int, int, int]] = []
    has_pdata_section = False
    for index in range(n_sections):
        clock.tick(64)
        raw = reader.read(table + index * 40, 40)
        name = raw[:8].rstrip(b"\x00")
        virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from("<IIII", raw, 8)
        if name == b".pdata" and raw_size:
            has_pdata_section = True
        sections.append((virtual_address, virtual_size, raw_offset, raw_size))

    exception_rva, exception_size = directory(3)
    has_pdata = bool(exception_rva and exception_size) or has_pdata_section

    guid = None
    age = None
    pdb_path = ""
    debug_rva, debug_size = directory(6)
    if debug_rva and debug_size:
        start = _rva_to_offset(debug_rva, sections)
        if start is None:
            # Headers-only images (and some linkers) keep it inside the headers.
            start = debug_rva if debug_rva < reader.size else None
        if start is not None:
            count = min(debug_size // 28, MAX_DEBUG_ENTRIES)
            for index in range(count):
                clock.tick(8)
                entry = reader.read(start + index * 28, 28)
                (_chars, _stamp, _major, _minor, dtype, data_size, data_rva,
                 data_ptr) = struct.unpack_from("<IIHHIIII", entry, 0)
                if dtype != IMAGE_DEBUG_TYPE_CODEVIEW or data_size < 24:
                    continue
                offset = data_ptr or _rva_to_offset(data_rva, sections)
                if offset is None:
                    continue
                length = min(data_size, 24 + MAX_PDB_PATH_CHARS * 4)
                try:
                    record = reader.read(offset, length)
                except ByteRangeError:
                    raise BinaryFormatError("OUT_OF_BOUNDS", "CodeView record beyond the file") from None
                parsed = parse_rsds(record)
                if parsed is not None:
                    guid, age, pdb_path = parsed
                    break

    return PeIdentity(
        machine=MACHINES.get(machine, "0x%04X" % machine),
        timestamp=timestamp,
        size_of_image=size_of_image,
        rsds_guid=guid,
        rsds_age=age,
        pdb_path=pdb_path,
        pdb_basename=clean_text(pdb_basename(pdb_path), MAX_PDB_PATH_CHARS),
        has_pdata=has_pdata,
        machine_code=machine,
        pe32_plus=pe32_plus,
        image_base=image_base,
        sections=n_sections,
        characteristics=characteristics,
    )


__all__ = ["MACHINES", "PeIdentity", "parse_rsds", "pdb_basename", "read_pe_identity"]
