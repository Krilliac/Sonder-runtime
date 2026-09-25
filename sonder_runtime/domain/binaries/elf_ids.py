"""ELF identity: GNU build-id, ``.gnu_debuglink``, machine and type.

The build-id is read from PT_NOTE segments first, which works on stripped
binaries and on ELF images found in core-file memory, then from SHT_NOTE
sections. Extended numbering is honoured: ``e_phnum == PN_XNUM`` takes the
real count from section 0's ``sh_info``, ``e_shnum == 0`` from its
``sh_size`` and ``e_shstrndx == SHN_XINDEX`` from its ``sh_link``.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from ..diagnostics.model import clean_text
from .reader import BinaryFormatError, BudgetedReader, ByteRangeError, ByteReader, WallClock, c_string


ET_CORE = 4
PT_LOAD = 1
PT_NOTE = 4
SHT_NOTE = 7
PN_XNUM = 0xFFFF
SHN_XINDEX = 0xFFFF
NT_GNU_BUILD_ID = 3
MAX_PHDRS = 65_535
MAX_SHDRS = 65_535
MAX_NOTE_BYTES = 1 << 20
MAX_IDENTITY_BYTES_READ = 16 << 20
MACHINES = {3: "x86", 40: "arm", 62: "x86_64", 183: "aarch64", 243: "riscv", 8: "mips", 21: "ppc64"}
TYPES = {1: "rel", 2: "exec", 3: "dyn", 4: "core"}


@dataclass(frozen=True, slots=True)
class ElfIdentity:
    build_id: str
    debuglink: str
    machine: str
    type: str
    machine_code: int = 0
    type_code: int = 0
    bits: int = 64
    endian: str = "little"


@dataclass(frozen=True, slots=True)
class ElfHeader:
    bits: int
    endian: str          # struct prefix "<" or ">"
    type: int
    machine: int
    entry: int
    phoff: int
    shoff: int
    phentsize: int
    phnum: int
    shentsize: int
    shnum: int
    shstrndx: int


@dataclass(frozen=True, slots=True)
class ProgramHeader:
    type: int
    flags: int
    offset: int
    vaddr: int
    filesz: int
    memsz: int
    align: int


def read_elf_header(reader: ByteReader) -> ElfHeader:
    if reader.size < 52:
        raise BinaryFormatError("NOT_ELF", "too small for an ELF header")
    ident = reader.read(0, 16)
    if ident[:4] != b"\x7fELF":
        raise BinaryFormatError("NOT_ELF", "missing ELF magic")
    bits = {1: 32, 2: 64}.get(ident[4])
    endian = {1: "<", 2: ">"}.get(ident[5])
    if bits is None or endian is None:
        raise BinaryFormatError("NOT_ELF", "unsupported ELF class or byte order")
    if bits == 64:
        if reader.size < 64:
            raise BinaryFormatError("TRUNCATED", "short ELF64 header")
        (e_type, machine, _version, entry, phoff, shoff, _flags, _ehsize, phentsize, phnum,
         shentsize, shnum, shstrndx) = struct.unpack(endian + "HHIQQQIHHHHHH", reader.read(16, 48))
    else:
        (e_type, machine, _version, entry, phoff, shoff, _flags, _ehsize, phentsize, phnum,
         shentsize, shnum, shstrndx) = struct.unpack(endian + "HHIIIIIHHHHHH", reader.read(16, 36))
    header = ElfHeader(bits, endian, e_type, machine, entry, phoff, shoff, phentsize, phnum,
                       shentsize, shnum, shstrndx)
    if phnum == PN_XNUM or shnum == 0 or shstrndx == SHN_XINDEX:
        header = _extended_numbering(reader, header)
    return header


def _extended_numbering(reader: ByteReader, header: ElfHeader) -> ElfHeader:
    if not header.shoff:
        if header.phnum == PN_XNUM:
            raise BinaryFormatError("TRUNCATED", "PN_XNUM without a section header")
        return header
    try:
        section0 = _section_header(reader, header, 0)
    except ByteRangeError:
        raise BinaryFormatError("TRUNCATED", "extended numbering section 0 beyond the file") from None
    _name, _type, _flags, _addr, _offset, size, link, info = section0
    phnum = info if header.phnum == PN_XNUM else header.phnum
    shnum = size if header.shnum == 0 else header.shnum
    shstrndx = link if header.shstrndx == SHN_XINDEX else header.shstrndx
    return ElfHeader(header.bits, header.endian, header.type, header.machine, header.entry,
                     header.phoff, header.shoff, header.phentsize, phnum, header.shentsize,
                     shnum, shstrndx)


def program_headers(reader: ByteReader, header: ElfHeader, *, limit: int = MAX_PHDRS,
                    clock: WallClock | None = None) -> list[ProgramHeader]:
    if header.phnum == 0:
        return []
    if header.phnum > limit:
        raise BinaryFormatError("LIMIT_EXCEEDED", "too many program headers")
    minimum = 56 if header.bits == 64 else 32
    if header.phentsize < minimum:
        raise BinaryFormatError("OUT_OF_BOUNDS", "program header entry too small")
    out = []
    for index in range(header.phnum):
        if clock is not None:
            clock.tick(256)
        try:
            raw = reader.read(header.phoff + index * header.phentsize, minimum)
        except ByteRangeError:
            raise BinaryFormatError("TRUNCATED", "program header table beyond the file") from None
        if header.bits == 64:
            p_type, flags, offset, vaddr, _paddr, filesz, memsz, align = struct.unpack(
                header.endian + "IIQQQQQQ", raw)
        else:
            p_type, offset, vaddr, _paddr, filesz, memsz, flags, align = struct.unpack(
                header.endian + "IIIIIIII", raw)
        out.append(ProgramHeader(p_type, flags, offset, vaddr, filesz, memsz, align))
    return out


def _section_header(reader: ByteReader, header: ElfHeader, index: int) -> tuple:
    if header.bits == 64:
        raw = reader.read(header.shoff + index * max(header.shentsize, 64), 64)
        name, s_type, flags, addr, offset, size, link, info = struct.unpack_from(
            header.endian + "IIQQQQII", raw)
    else:
        raw = reader.read(header.shoff + index * max(header.shentsize, 40), 40)
        name, s_type, flags, addr, offset, size, link, info = struct.unpack_from(
            header.endian + "IIIIIIII", raw)
    return name, s_type, flags, addr, offset, size, link, info


def iter_notes(data: bytes, endian: str, align: int):
    """Yield (name, type, desc) from a note blob; stops at the first bad entry."""
    step = 8 if align == 8 else 4
    offset = 0
    size = len(data)
    count = 0
    while offset + 12 <= size and count < 65_536:
        count += 1
        namesz, descsz, n_type = struct.unpack_from(endian + "III", data, offset)
        offset += 12
        name_end = offset + namesz
        if namesz > size or name_end > size:
            return
        name = data[offset:name_end].rstrip(b"\x00")
        offset = name_end + (-namesz % step)
        desc_end = offset + descsz
        if descsz > size or desc_end > size:
            return
        desc = data[offset:desc_end]
        offset = desc_end + (-descsz % step)
        yield name, n_type, desc


def build_id_from_notes(data: bytes, endian: str, align: int) -> str:
    for name, n_type, desc in iter_notes(data, endian, align):
        if name == b"GNU" and n_type == NT_GNU_BUILD_ID and 0 < len(desc) <= 64:
            return desc.hex()
    return ""


def read_elf_identity(reader: ByteReader, *, max_seconds: float = 2.0,
                      max_bytes_read: int = MAX_IDENTITY_BYTES_READ) -> ElfIdentity:
    """Build-id, debuglink, machine and type of an ELF file.

    Every read is charged to a ``max_bytes_read`` budget and the wall clock is
    checked before each note blob: header counts and note sizes are attacker
    controlled (65535 section headers each naming the same 1 MiB note).
    """
    clock = WallClock(max_seconds)
    reader = BudgetedReader(reader, max_bytes_read)
    header = read_elf_header(reader)
    build_id = ""
    for phdr in program_headers(reader, header, clock=clock):
        if phdr.type != PT_NOTE or not phdr.filesz:
            continue
        if phdr.filesz > MAX_NOTE_BYTES:
            continue
        clock.check()
        try:
            blob = reader.read(phdr.offset, phdr.filesz)
        except ByteRangeError:
            continue
        build_id = build_id_from_notes(blob, header.endian, phdr.align)
        if build_id:
            break
    debuglink = ""
    if header.shoff and header.shnum and header.type != ET_CORE:
        if header.shnum > MAX_SHDRS:
            raise BinaryFormatError("LIMIT_EXCEEDED", "too many section headers")
        names = b""
        try:
            if header.shstrndx < header.shnum:
                strtab = _section_header(reader, header, header.shstrndx)
                if strtab[5] <= MAX_NOTE_BYTES:
                    names = reader.read(strtab[4], strtab[5])
        except ByteRangeError:
            names = b""
        for index in range(header.shnum):
            clock.tick(256)
            try:
                name_off, s_type, _f, _a, offset, size, _l, _i = _section_header(reader, header, index)
            except ByteRangeError:
                raise BinaryFormatError("TRUNCATED", "section header table beyond the file") from None
            name = c_string(names[name_off:], 64) if name_off < len(names) else b""
            if not build_id and s_type == SHT_NOTE and 0 < size <= MAX_NOTE_BYTES:
                clock.check()
                try:
                    build_id = build_id_from_notes(reader.read(offset, size), header.endian, 4)
                except ByteRangeError:
                    pass
            if name == b".gnu_debuglink" and 0 < size <= 4096 and not debuglink:
                try:
                    debuglink = clean_text(
                        c_string(reader.read(offset, size), 255).decode("utf-8", "replace"), 255)
                except ByteRangeError:
                    pass
    return ElfIdentity(
        build_id=build_id,
        debuglink=debuglink,
        machine=MACHINES.get(header.machine, str(header.machine)),
        type=TYPES.get(header.type, str(header.type)),
        machine_code=header.machine,
        type_code=header.type,
        bits=header.bits,
        endian="little" if header.endian == "<" else "big",
    )


__all__ = [
    "ET_CORE", "ElfHeader", "ElfIdentity", "PN_XNUM", "PT_LOAD", "PT_NOTE", "ProgramHeader",
    "build_id_from_notes", "iter_notes", "program_headers", "read_elf_header", "read_elf_identity",
]
