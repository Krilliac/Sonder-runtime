"""PDB (MSF 7.0) identity: GUID, PDB-stream age and DBI age.

Only the superblock, the stream directory prefix, stream 1 (PDB info) and
the DBI header of stream 3 are read. Every block index is range-checked
against ``num_blocks`` and the file size, the directory is limited to 4096
blocks, and nothing follows cycles because block lists are indexed, never
chased.

``pdb_matches`` is the gate that decides whether a PDB may be handed to a
host symbolizer next to its PE: the GUIDs must be equal and the RSDS age
must equal the DBI age; the PDB-stream age is used only when the PDB has no
DBI stream (``pdb_match_basis`` says which age matched).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from .pe_debug import PeIdentity
from .reader import BinaryFormatError, ByteRangeError, ByteReader, WallClock
from .symstore import format_guid


MSF7_MAGIC = b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\x00\x00\x00"
BLOCK_SIZES = (512, 1024, 2048, 4096)
MAX_DIRECTORY_BLOCKS = 4096
MAX_STREAMS = 1 << 20
NIL_STREAM = 0xFFFFFFFF


@dataclass(frozen=True, slots=True)
class PdbIdentity:
    guid: str
    pdb_age: int
    dbi_age: int | None
    version: int = 0
    signature: int = 0
    block_size: int = 0
    streams: int = 0


class _Msf:
    def __init__(self, reader: ByteReader, clock: WallClock) -> None:
        self.reader = reader
        self.clock = clock
        if reader.size < 56:
            raise BinaryFormatError("NOT_PDB", "too small for an MSF superblock")
        if reader.read(0, 32) != MSF7_MAGIC:
            raise BinaryFormatError("NOT_PDB", "missing MSF 7.00 magic")
        (self.block_size, _fpm, self.num_blocks, self.dir_bytes, _unknown,
         self.block_map_addr) = struct.unpack("<IIIIII", reader.read(32, 24))
        if self.block_size not in BLOCK_SIZES:
            raise BinaryFormatError("OUT_OF_BOUNDS", "invalid MSF block size")
        if self.num_blocks == 0 or self.num_blocks * self.block_size > reader.size:
            raise BinaryFormatError("TRUNCATED", "MSF claims more blocks than the file holds")
        dir_blocks = -(-self.dir_bytes // self.block_size)
        if dir_blocks == 0:
            raise BinaryFormatError("TRUNCATED", "empty MSF stream directory")
        if dir_blocks > MAX_DIRECTORY_BLOCKS:
            raise BinaryFormatError("LIMIT_EXCEEDED", "MSF directory too large")
        if dir_blocks * 4 > self.block_size:
            # The block map itself must fit in one block (MSF 7.0 layout).
            raise BinaryFormatError("LIMIT_EXCEEDED", "MSF block map spans blocks")
        self._check_block(self.block_map_addr)
        raw = self._block_bytes(self.block_map_addr, 0, dir_blocks * 4)
        self.dir_block_list = struct.unpack("<%dI" % dir_blocks, raw)
        for block in self.dir_block_list:
            self._check_block(block)

    def _check_block(self, index: int) -> None:
        if not 0 < index < self.num_blocks:
            raise BinaryFormatError("OUT_OF_BOUNDS", "block index %d out of range" % index)

    def _block_bytes(self, block: int, offset: int, length: int) -> bytes:
        try:
            return self.reader.read(block * self.block_size + offset, length)
        except ByteRangeError:
            raise BinaryFormatError("OUT_OF_BOUNDS", "block %d beyond the file" % block) from None

    def _read_blocks(self, blocks, total: int, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0 or offset + length > total:
            raise BinaryFormatError("OUT_OF_BOUNDS", "stream read beyond stream size")
        out = bytearray()
        position = offset
        end = offset + length
        while position < end:
            self.clock.tick(256)
            index, within = divmod(position, self.block_size)
            if index >= len(blocks):
                raise BinaryFormatError("OUT_OF_BOUNDS", "stream block list too short")
            block = blocks[index]
            self._check_block(block)
            take = min(self.block_size - within, end - position)
            out += self._block_bytes(block, within, take)
            position += take
        return bytes(out)

    def directory(self, offset: int, length: int) -> bytes:
        return self._read_blocks(self.dir_block_list, self.dir_bytes, offset, length)

    def stream_layout(self, wanted: tuple[int, ...]) -> tuple[int, dict[int, tuple[int, tuple[int, ...]]]]:
        (count,) = struct.unpack("<I", self.directory(0, 4))
        if count > MAX_STREAMS or 4 + count * 4 > self.dir_bytes:
            raise BinaryFormatError("LIMIT_EXCEEDED", "MSF stream count exceeds the directory")
        highest = max(wanted)
        prefix = min(count, highest + 1)
        sizes = struct.unpack("<%dI" % prefix, self.directory(4, prefix * 4))
        # Block lists start after *all* sizes; skip the lists of earlier streams.
        cursor = 4 + count * 4
        layout: dict[int, tuple[int, tuple[int, ...]]] = {}
        for index, size in enumerate(sizes):
            self.clock.tick(256)
            n_blocks = 0 if size == NIL_STREAM else -(-size // self.block_size)
            if n_blocks > self.num_blocks:
                raise BinaryFormatError("OUT_OF_BOUNDS", "stream larger than the file")
            if index in wanted and size != NIL_STREAM:
                raw = self.directory(cursor, n_blocks * 4)
                blocks = struct.unpack("<%dI" % n_blocks, raw)
                for block in blocks:
                    self._check_block(block)
                layout[index] = (size, blocks)
            cursor += n_blocks * 4
            if cursor > self.dir_bytes:
                raise BinaryFormatError("OUT_OF_BOUNDS", "stream block lists exceed the directory")
        return count, layout

    def stream(self, layout, index: int, offset: int, length: int) -> bytes | None:
        entry = layout.get(index)
        if entry is None:
            return None
        size, blocks = entry
        if offset + length > size:
            return None
        return self._read_blocks(blocks, size, offset, length)


def read_pdb_identity(reader: ByteReader, *, max_seconds: float = 2.0) -> PdbIdentity:
    """GUID and ages of a PDB. Raises ``BinaryFormatError``."""
    msf = _Msf(reader, WallClock(max_seconds))
    count, layout = msf.stream_layout((1, 3))
    info = msf.stream(layout, 1, 0, 28)
    if info is None:
        raise BinaryFormatError("TRUNCATED", "PDB info stream missing")
    version, signature, age = struct.unpack_from("<III", info, 0)
    guid = format_guid(info[12:28])
    dbi_age = None
    dbi = msf.stream(layout, 3, 0, 12)
    if dbi is not None:
        dbi_signature, _dbi_version, candidate = struct.unpack_from("<iII", dbi, 0)
        if dbi_signature == -1:
            dbi_age = candidate
    return PdbIdentity(guid=guid, pdb_age=age, dbi_age=dbi_age, version=version,
                       signature=signature, block_size=msf.block_size, streams=count)


def pdb_match_basis(pe: PeIdentity, pdb: PdbIdentity) -> str | None:
    """"dbi_age" or "pdb_age" when the pair matches, else None."""
    if not pe.rsds_guid or pe.rsds_age is None or pe.rsds_guid.upper() != pdb.guid.upper():
        return None
    if pdb.dbi_age is not None:
        # The DBI age is the one the linker writes into the PE's RSDS record;
        # the PDB-stream age may run ahead of it after incremental links, so
        # it is never a fallback when a DBI age exists (spec: pdb_matches).
        return "dbi_age" if pe.rsds_age == pdb.dbi_age else None
    if pe.rsds_age == pdb.pdb_age:
        return "pdb_age"
    return None


def pdb_matches(pe: PeIdentity, pdb: PdbIdentity) -> bool:
    """True when ``pdb`` is the PDB the linker recorded in ``pe``'s RSDS."""
    return pdb_match_basis(pe, pdb) is not None


__all__ = ["MSF7_MAGIC", "PdbIdentity", "pdb_match_basis", "pdb_matches", "read_pdb_identity"]
