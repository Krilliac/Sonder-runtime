"""Byte-exact synthetic minidump builder for the pure crash-reader tests.

Builds tiny MDMP files (Windows, Breakpad-flavoured and Crashpad-flavoured)
without a Windows host: header, stream directory, thread/module/memory/
exception/system-info/memory64/unloaded/misc/thread-name streams, Breakpad
info and Crashpad simple annotations. Layout follows the documented
MINIDUMP_* structures (all little-endian, packed):

- MINIDUMP_HEADER 32 bytes; MINIDUMP_DIRECTORY 12 bytes per stream;
- MINIDUMP_THREAD 48, MINIDUMP_MODULE 108, MINIDUMP_EXCEPTION_STREAM 168;
- CONTEXT records with PC/SP at AMD64 0xF8/0x98, ARM64 0x108/0x100 and
  x86 0xB8/0xC4.

The builder never writes vendor or secret-shaped strings; module names are
neutral (``spark_game.exe``, ``render.dll``).
"""
from __future__ import annotations

import struct
import uuid


DIRECTORY_SLOTS = 20
HEADER_SIZE = 32
DATA_START = HEADER_SIZE + 12 * DIRECTORY_SLOTS

AMD64, ARM64, X86 = 9, 12, 0
WIN32NT, LINUX, MACOS = 2, 0x8201, 0x8101


def amd64_context(pc: int, sp: int) -> bytes:
    ctx = bytearray(0x4D0)
    struct.pack_into("<I", ctx, 0x30, 0x10001F)  # ContextFlags
    struct.pack_into("<Q", ctx, 0x98, sp)
    struct.pack_into("<Q", ctx, 0xF8, pc)
    return bytes(ctx)


def arm64_context(pc: int, sp: int) -> bytes:
    ctx = bytearray(0x390)
    struct.pack_into("<I", ctx, 0, 0x400003)
    struct.pack_into("<Q", ctx, 0x100, sp)
    struct.pack_into("<Q", ctx, 0x108, pc)
    return bytes(ctx)


def x86_context(pc: int, sp: int) -> bytes:
    ctx = bytearray(0x2CC)
    struct.pack_into("<I", ctx, 0, 0x1003F)
    struct.pack_into("<I", ctx, 0xB8, pc)
    struct.pack_into("<I", ctx, 0xC4, sp)
    return bytes(ctx)


CONTEXTS = {AMD64: amd64_context, ARM64: arm64_context, X86: x86_context}


def rsds_record(guid: str, age: int, pdb_path: str) -> bytes:
    return b"RSDS" + uuid.UUID(guid).bytes_le + struct.pack("<I", age) + pdb_path.encode() + b"\x00"


def bpel_record(build_id: bytes) -> bytes:
    return b"BpEL" + bytes(build_id)


class MinidumpBuilder:
    def __init__(self) -> None:
        self._data = bytearray()
        self._streams: list[tuple[int, bytes | None, callable]] = []
        self._threads: list[bytes] = []
        self._modules: list[bytes] = []
        self._memory: list[bytes] = []
        self._memory64: list[tuple[int, bytes]] = []
        self._names: list[tuple[int, int]] = []
        self._unloaded: list[bytes] = []
        self.extra_directory: list[tuple[int, int, int]] = []

    # -- raw data ------------------------------------------------------------
    def put(self, blob: bytes, align: int = 4) -> int:
        while len(self._data) % align:
            self._data.append(0)
        rva = DATA_START + len(self._data)
        self._data += blob
        return rva

    def put_string(self, text: str) -> int:
        raw = text.encode("utf-16-le")
        return self.put(struct.pack("<I", len(raw)) + raw + b"\x00\x00")

    def put_utf8(self, text: str) -> int:
        raw = text.encode("utf-8")
        return self.put(struct.pack("<I", len(raw)) + raw + b"\x00")

    def raw_stream(self, stream_type: int, blob: bytes) -> "MinidumpBuilder":
        self._streams.append((stream_type, blob, None))
        return self

    # -- streams ---------------------------------------------------------------
    def system_info(self, arch: int = AMD64, platform: int = WIN32NT, major: int = 10,
                    minor: int = 0, build: int = 19045, csd: str = "") -> "MinidumpBuilder":
        csd_rva = self.put_string(csd) if csd else 0
        blob = struct.pack("<HHHBBIIIII", arch, 6, 0, 8, 1, major, minor, build, platform, csd_rva)
        blob += b"\x00" * (56 - len(blob))
        return self.raw_stream(7, blob)

    def module(self, path: str, base: int, size: int, *, version=(1, 2, 3, 4),
               timestamp: int = 0x65000000, cv: bytes | None = None) -> "MinidumpBuilder":
        name_rva = self.put_string(path)
        cv_rva = self.put(cv) if cv else 0
        ms = (version[0] << 16) | version[1]
        ls = (version[2] << 16) | version[3]
        fixed = struct.pack("<13I", 0xFEEF04BD, 0x10000, ms, ls, ms, ls, 0x3F, 0, 4, 1, 0, 0, 0)
        entry = struct.pack("<QIIII", base, size, 0, timestamp, name_rva) + fixed
        entry += struct.pack("<IIII", len(cv) if cv else 0, cv_rva, 0, 0) + b"\x00" * 16
        assert len(entry) == 108
        self._modules.append(entry)
        return self

    def thread(self, tid: int, *, arch: int = AMD64, pc: int, sp: int, stack_start: int | None = None,
               stack: bytes = b"", in_memory64: bool = False, context: bytes | None = None) -> "MinidumpBuilder":
        ctx = context if context is not None else CONTEXTS[arch](pc, sp)
        ctx_rva = self.put(ctx, 16)
        start = sp if stack_start is None else stack_start
        if stack and not in_memory64:
            stack_rva = self.put(stack, 16)
            stack_size = len(stack)
        else:
            stack_rva, stack_size = 0, len(stack)
            if stack:
                self._memory64.append((start, stack))
        self._threads.append(struct.pack("<IIIIQQIIII", tid, 0, 0x20, 0, 0x7000_0000 + tid,
                                         start, stack_size, stack_rva, len(ctx), ctx_rva))
        return self

    def memory(self, start: int, blob: bytes) -> "MinidumpBuilder":
        rva = self.put(blob, 16)
        self._memory.append(struct.pack("<QII", start, len(blob), rva))
        return self

    def memory64(self, start: int, blob: bytes) -> "MinidumpBuilder":
        self._memory64.append((start, blob))
        return self

    def exception(self, tid: int, code: int, *, address: int = 0, params=(), flags: int = 0,
                  arch: int = AMD64, pc: int | None = None, sp: int | None = None,
                  context: bytes | None = None) -> "MinidumpBuilder":
        ctx = context
        if ctx is None and pc is not None:
            ctx = CONTEXTS[arch](pc, sp or 0)
        ctx_rva = self.put(ctx, 16) if ctx else 0
        info = list(params)[:15] + [0] * (15 - len(list(params)[:15]))
        blob = struct.pack("<IIIIQQII", tid, 0, code, flags, 0, address, len(list(params)), 0)
        blob += struct.pack("<15Q", *info) + struct.pack("<II", len(ctx) if ctx else 0, ctx_rva)
        assert len(blob) == 168
        return self.raw_stream(6, blob)

    def thread_name(self, tid: int, name: str) -> "MinidumpBuilder":
        self._names.append((tid, self.put_string(name)))
        return self

    def unloaded(self, path: str, base: int, size: int) -> "MinidumpBuilder":
        self._unloaded.append(struct.pack("<QIIII", base, size, 0, 0, self.put_string(path)))
        return self

    def misc(self, pid: int, create_time: int = 1_700_000_000) -> "MinidumpBuilder":
        return self.raw_stream(15, struct.pack("<IIIIII", 24, 3, pid, create_time, 0, 0))

    def breakpad_info(self, dump_tid: int, requesting_tid: int) -> "MinidumpBuilder":
        return self.raw_stream(0x47670001, struct.pack("<III", 3, dump_tid, requesting_tid))

    def crashpad_annotations(self, pairs) -> "MinidumpBuilder":
        entries = b"".join(struct.pack("<II", self.put_utf8(k), self.put_utf8(v)) for k, v in pairs)
        dict_blob = struct.pack("<I", len(pairs)) + entries
        dict_rva = self.put(dict_blob)
        blob = struct.pack("<I", 1) + b"\x11" * 16 + b"\x22" * 16 + struct.pack("<IIII", len(dict_blob), dict_rva, 0, 0)
        return self.raw_stream(0x43500001, blob)

    # -- assembly --------------------------------------------------------------
    def build(self, *, n_streams_override: int | None = None) -> bytes:
        streams = list(self._streams)
        if self._threads:
            streams.append((3, struct.pack("<I", len(self._threads)) + b"".join(self._threads), None))
        if self._modules:
            streams.append((4, struct.pack("<I", len(self._modules)) + b"".join(self._modules), None))
        if self._memory:
            streams.append((5, struct.pack("<I", len(self._memory)) + b"".join(self._memory), None))
        if self._unloaded:
            streams.append((14, struct.pack("<III", 12, 24, len(self._unloaded)) + b"".join(self._unloaded), None))
        if self._names:
            streams.append((24, struct.pack("<I", len(self._names)) + b"".join(
                struct.pack("<IQ", tid, rva) for tid, rva in self._names), None))
        directory = []
        for stream_type, blob, _ in streams:
            directory.append((stream_type, len(blob), self.put(blob)))
        if self._memory64:
            descriptors = b"".join(struct.pack("<QQ", start, len(blob)) for start, blob in self._memory64)
            header_len = 16 + len(descriptors)
            list_rva = self.put(b"\x00" * header_len, 16)
            base_rva = self.put(b"".join(blob for _, blob in self._memory64), 16)
            offset = list_rva - DATA_START
            self._data[offset:offset + header_len] = struct.pack("<QQ", len(self._memory64), base_rva) + descriptors
            directory.append((9, header_len, list_rva))
        directory += self.extra_directory
        assert len(directory) <= DIRECTORY_SLOTS
        count = len(directory) if n_streams_override is None else n_streams_override
        header = struct.pack("<4sIIIIIQ", b"MDMP", 0xA793, count, HEADER_SIZE, 0, 0x65000000, 0)
        dir_blob = b"".join(struct.pack("<III", *entry) for entry in directory)
        dir_blob += b"\x00" * (12 * DIRECTORY_SLOTS - len(dir_blob))
        return header + dir_blob + bytes(self._data)


def stack_with_returns(base: int, returns, *, pointer_size: int = 8, filler: int = 0x41414141,
                       words: int = 64) -> bytes:
    """A stack blob starting at ``base``: return addresses spaced by filler words."""
    fmt = "<Q" if pointer_size == 8 else "<I"
    out = bytearray()
    returns = list(returns)
    for index in range(words):
        if index % 4 == 2 and returns:
            out += struct.pack(fmt, returns.pop(0))
        else:
            out += struct.pack(fmt, filler & ((1 << (8 * pointer_size)) - 1))
    return bytes(out)


GAME_GUID = "1B4E28BA-2FA1-11D2-883F-B9A761BDE3FB"
GAME_BASE = 0x7FF6_1000_0000
RENDER_BASE = 0x7FFA_2000_0000
NTDLL_BASE = 0x7FFB_3000_0000
STACK_BASE = 0x0000_00C1_2F8F_0000


def amd64_access_violation(*, read_address: int = 0, write: bool = False, crash_offset: int = 0x1234,
                           crash_module: str = "spark_game.exe", with_name: str = "MainThread",
                           extra_modules=()) -> bytes:
    """AMD64 Windows AV in ``crash_module`` with two scanned return addresses."""
    builder = MinidumpBuilder().system_info(AMD64, WIN32NT)
    builder.module("C:\\build\\spark_game.exe", GAME_BASE, 0x20000,
                   cv=rsds_record(GAME_GUID, 3, "C:\\agent\\_work\\3\\s\\out\\spark_game.pdb"))
    builder.module("C:\\build\\render.dll", RENDER_BASE, 0x10000,
                   cv=rsds_record("2B4E28BA-2FA1-11D2-883F-B9A761BDE3FB", 1, "render.pdb"))
    builder.module("C:\\Windows\\System32\\ntdll.dll", NTDLL_BASE, 0x1F0000, version=(10, 0, 19041, 1))
    for path, base, size in extra_modules:
        builder.module(path, base, size)
    crash_base = GAME_BASE if crash_module == "spark_game.exe" else RENDER_BASE
    pc = crash_base + crash_offset
    sp = STACK_BASE + 0x100
    stack = stack_with_returns(sp, [GAME_BASE + 0x2000, GAME_BASE + 0x3000, NTDLL_BASE + 0x500])
    builder.thread(0x1B3C, pc=pc, sp=sp, stack=stack)
    builder.thread(0x2200, pc=NTDLL_BASE + 0x9F000, sp=STACK_BASE + 0x80000,
                   stack=stack_with_returns(STACK_BASE + 0x80000, [NTDLL_BASE + 0x100]))
    if with_name:
        builder.thread_name(0x1B3C, with_name)
    builder.exception(0x1B3C, 0xC0000005, address=pc, params=(1 if write else 0, read_address),
                      pc=pc, sp=sp)
    builder.misc(4242)
    return builder.build()
