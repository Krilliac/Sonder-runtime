"""Range-checked, budgeted random access over untrusted binary files.

Every structural read in the pure crash and binary-identity readers goes
through a ``ByteReader``: ``read(offset, length)`` returns exactly ``length``
bytes or raises ``ByteRangeError``, so a lying offset or size in a hostile
file can never produce a short read, a negative slice or an unbounded
allocation. ``BudgetedReader`` caps the total bytes a reader may pull, and
``WallClock`` bounds the time spent in loops over attacker-chosen counts.

Adapters provide file-backed readers (``os.pread``/seek+read); this module
only defines the contract and an in-memory implementation. No I/O.
"""
from __future__ import annotations

import struct
import time
from typing import Callable, Protocol, runtime_checkable

from ..common.errors import InvalidInput


class BinaryFormatError(InvalidInput):
    """A binary input is malformed or exceeds a limit.

    ``code`` is one of NOT_PE, NOT_PDB, NOT_ELF, NOT_MINIDUMP, NOT_CORE,
    TRUNCATED, OUT_OF_BOUNDS, LIMIT_EXCEEDED, UNSUPPORTED_ARCH or
    TIME_EXCEEDED.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code)
        self.detail = str(detail)[:200]
        super().__init__("%s: %s" % (self.code, self.detail) if self.detail else self.code)


class ByteRangeError(BinaryFormatError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("OUT_OF_BOUNDS", detail)


class ReadBudgetExceeded(BinaryFormatError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("LIMIT_EXCEEDED", detail)


@runtime_checkable
class ByteReader(Protocol):
    @property
    def size(self) -> int: ...

    def read(self, offset: int, length: int) -> bytes: ...


def check_range(offset: int, length: int, size: int) -> None:
    """Raise ``ByteRangeError`` unless ``0 <= offset <= size - length``."""
    if not (isinstance(offset, int) and isinstance(length, int) and isinstance(size, int)):
        raise ByteRangeError("non-integer range")
    if length < 0 or offset < 0 or size < 0 or length > size or not 0 <= offset <= size - length:
        raise ByteRangeError("range %d+%d outside %d bytes" % (offset, length, size))


class BytesReader:
    """A ``ByteReader`` over an in-memory buffer."""

    __slots__ = ("_data",)

    def __init__(self, data: bytes) -> None:
        self._data = bytes(data)

    @property
    def size(self) -> int:
        return len(self._data)

    def read(self, offset: int, length: int) -> bytes:
        check_range(offset, length, len(self._data))
        return self._data[offset:offset + length]


class BudgetedReader:
    """Wraps a reader and refuses once ``max_bytes_read`` would be exceeded."""

    __slots__ = ("_inner", "_max", "bytes_read")

    def __init__(self, inner: ByteReader, max_bytes_read: int) -> None:
        self._inner = inner
        self._max = max(0, int(max_bytes_read))
        self.bytes_read = 0

    @property
    def size(self) -> int:
        return int(self._inner.size)

    def read(self, offset: int, length: int) -> bytes:
        check_range(offset, length, self.size)
        if self.bytes_read + length > self._max:
            raise ReadBudgetExceeded("read budget of %d bytes exhausted" % self._max)
        self.bytes_read += length
        data = self._inner.read(offset, length)
        if len(data) != length:
            raise ByteRangeError("short read at %d" % offset)
        return data


class WallClock:
    """A wall-clock budget checked from loops over untrusted counts."""

    __slots__ = ("_clock", "_deadline", "_ticks")

    def __init__(self, max_seconds: float, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic
        self._deadline = self._clock() + max(0.0, float(max_seconds))
        self._ticks = 0

    def check(self) -> None:
        if self._clock() > self._deadline:
            raise BinaryFormatError("TIME_EXCEEDED", "wall-clock budget exhausted")

    def tick(self, every: int = 1000) -> None:
        """Cheap per-iteration hook: checks the clock every ``every`` calls."""
        self._ticks += 1
        if self._ticks % max(1, int(every)) == 0:
            self.check()


def unpack(reader: ByteReader, offset: int, fmt: str) -> tuple:
    """``struct.unpack`` of ``fmt`` (explicit byte order) read at ``offset``."""
    size = struct.calcsize(fmt)
    return struct.unpack(fmt, reader.read(offset, size))


def u16(reader: ByteReader, offset: int, endian: str = "<") -> int:
    return unpack(reader, offset, endian + "H")[0]


def u32(reader: ByteReader, offset: int, endian: str = "<") -> int:
    return unpack(reader, offset, endian + "I")[0]


def u64(reader: ByteReader, offset: int, endian: str = "<") -> int:
    return unpack(reader, offset, endian + "Q")[0]


def c_string(data: bytes, limit: int) -> bytes:
    """Bytes up to the first NUL (or ``limit``), never beyond ``data``."""
    view = data[: max(0, int(limit))]
    end = view.find(b"\x00")
    return view if end < 0 else view[:end]


__all__ = [
    "BinaryFormatError", "BudgetedReader", "ByteRangeError", "ByteReader",
    "BytesReader", "ReadBudgetExceeded", "WallClock", "c_string", "check_range",
    "u16", "u32", "u64", "unpack",
]
