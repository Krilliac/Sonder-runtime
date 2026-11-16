"""Seeded mutation fuzzing of the build-manifest, build-output and bounded-input parsers.

Contract: hostile input yields either the function's documented typed error
(``BinaryFormatError``, ``JsonBoundsExceeded``, ``UnsafeXml``,
``BuildDomainError``/``TemplateRejected``, ``InvalidInput``, ``ValueError``)
or a result that satisfies the security invariants checked here, inside the
time budget (p99 < 50 ms per input, hard cap 2 s). Nothing else may escape.

Groups, in priority order (the seed is ``FUZZ_SEED``, default below; every
failure message carries it so a CI failure replays with the same value):

1. ``domain.binaries.elf_ids``: ``read_elf_header``, ``read_elf_identity``,
   ``iter_notes``, ``build_id_from_notes`` (build id is hex and bounded, note
   iteration never invents or over-reads, 2**32/2**64 offsets and sizes).
2. ``domain.common.bounded_json`` (``loads_bounded``, ``check_depth``,
   ``iter_array_objects``) and ``domain.common.safe_xml.parse_guarded_xml``
   (limits hold exactly at the limit and limit+1; XML bombs always refused).
3. ``domain.build.output`` (``parse_build_diagnostics``, ``parse_ninja_segments``,
   ``parse_make_failures``, ``parse_cmake_configure_errors``,
   ``parse_msvc_codeless``, ``parse_msbuild_projects``, ``parse_dash_H``,
   ``parse_show_includes``): lenient, never raise, bounded, derived from input.
4. ``domain.build.msbuild`` (``parse_solution``, ``parse_vcxproj``,
   ``resolve_import_path``) and ``domain.build.compile_db``
   (``parse_compile_commands``).
5. ``domain.build.templates`` (``validate_target_name``,
   ``validate_placeholder``, ``validate_define``).
6. Lower priority: ``parse_port_range``, ``parse_watched_task``,
   ``parse_schtasks_csv``, ``reference_lookup.sanitize.sanitize``,
   ``parse_update_object``, ``clangd.uri_to_path``.

Expected values come from the repo's captured/spec fixtures
(``tests/fixtures/cpp_build``) or from the ELF/PE specification layout; the
ELF builder below follows the System V gABI struct layouts.
"""
# ruff: noqa: UP031, B023
# UP031: the house style for these parser tests is %-formatting. B023: every closure handed to _run() is
# called inside the same loop iteration, so late binding of the loop variable cannot occur.
from __future__ import annotations

import json
import os
import random
import re
import struct
import time
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from sonder_runtime.adapters.build.clangd import uri_to_path
from sonder_runtime.domain.binaries.elf_ids import (
    ET_CORE,
    PT_NOTE,
    build_id_from_notes,
    iter_notes,
    read_elf_header,
    read_elf_identity,
)
from sonder_runtime.domain.binaries.reader import BinaryFormatError, BytesReader
from sonder_runtime.domain.build import output as build_output
from sonder_runtime.domain.build.compile_db import MAX_ENTRIES, parse_compile_commands
from sonder_runtime.domain.build.model import (
    PLATFORM_RE,
    BuildDomainError,
    BuildSystem,
    is_absolute,
)
from sonder_runtime.domain.build.msbuild import (
    parse_solution,
    parse_vcxproj,
    resolve_import_path,
)
from sonder_runtime.domain.build.templates import (
    PLACEHOLDERS,
    TEMPLATES,
    TemplateRejected,
    validate_define,
    validate_placeholder,
    validate_target_name,
)
from sonder_runtime.domain.common.bounded_json import (
    JsonBoundsExceeded,
    check_depth,
    iter_array_objects,
    loads_bounded,
)
from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.common.safe_xml import UnsafeXml, parse_guarded_xml
from sonder_runtime.domain.health_alerts import (
    DateOrder,
    TaskParseError,
    parse_schtasks_csv,
    parse_watched_task,
)
from sonder_runtime.domain.reference_lookup.sanitize import sanitize
from sonder_runtime.domain.resource_leases import parse_port_range
from sonder_runtime.domain.runtime_update_parsing import parse_update_object

SEED = int(os.environ.get("FUZZ_SEED", "20261116"))
FIXTURES = Path(__file__).parent / "fixtures" / "cpp_build"
_BOUNDARY32 = (0, 1, 0x7F, 0x80, 0xFFFF, 0x10000, 0x7FFF_FFFF, 0x8000_0000, 0xFFFF_FFFF)
_BOUNDARY64 = (2 ** 32, 2 ** 32 + 1, 2 ** 40, 2 ** 63 - 1, 2 ** 63, 2 ** 64 - 1)


def _rng(salt: int) -> random.Random:
    return random.Random(SEED * 1_000_003 + salt)


def _show(data: object) -> str:
    return "%r" % (data,) if len(repr(data)) <= 600 else repr(data)[:600] + "...(%d chars)" % len(repr(data))


def _fail(label: str, data: object, detail: str) -> None:
    pytest.fail("[FUZZ_SEED=%d] %s: %s; input=%s" % (SEED, label, detail, _show(data)), pytrace=False)


def _check_timings(label: str, timings: list[float]) -> None:
    assert timings, "[FUZZ_SEED=%d] %s: no inputs ran" % (SEED, label)
    ordered = sorted(timings)
    p99 = ordered[max(0, int(len(ordered) * 0.99) - 1)]
    assert p99 < 0.05, "[FUZZ_SEED=%d] %s: p99 %.1f ms" % (SEED, label, p99 * 1000)
    assert ordered[-1] < 2.0, "[FUZZ_SEED=%d] %s: slowest input %.1f ms" % (SEED, label, ordered[-1] * 1000)


def _run(label: str, fn, data, allowed: tuple, timings: list[float]):
    """Call ``fn(data)``; return (result, error). Any other exception fails with the input."""
    started = time.perf_counter()
    try:
        result, error = fn(data), None
    except allowed as exc:
        result, error = None, exc
    except BaseException as exc:  # noqa: BLE001 - the point is to catch the unexpected
        _fail(label, data, "unexpected %s: %s" % (type(exc).__name__, str(exc)[:200]))
    timings.append(time.perf_counter() - started)
    return result, error


def _mutate_bytes(rng: random.Random, seed: bytes, donors: list[bytes], *, hot: int = 0) -> bytes:
    data = bytearray(seed)
    for _ in range(rng.randint(1, 3)):
        op = rng.randrange(7)
        if op == 0 and data:
            for _ in range(rng.randint(1, 6)):
                data[rng.randrange(len(data))] = rng.randrange(256)
        elif op == 1 and data:
            del data[rng.randrange(len(data) + 1):]
        elif op == 2:
            donor = rng.choice(donors)
            start = rng.randrange(len(donor) + 1)
            chunk = donor[start:start + rng.randint(1, 128)]
            at = rng.randrange(len(data) + 1)
            data[at:at + len(chunk)] = chunk
        elif op in (3, 4) and len(data) >= 8:
            width = 4 if op == 3 else 8
            limit = min(len(data), hot) if hot and rng.random() < 0.7 else len(data)
            at = rng.randrange(0, max(1, limit - width + 1)) & ~1
            values = _BOUNDARY32 if width == 4 else _BOUNDARY32 + _BOUNDARY64
            endian = "little" if rng.random() < 0.5 else "big"
            data[at:at + width] = rng.choice(values).to_bytes(width, endian)
        elif op == 5 and data:
            start = rng.randrange(len(data))
            data[start:start] = bytes(data[start:start + rng.randint(1, 64)]) * rng.randint(1, 4)
        else:
            at = rng.randrange(len(data) + 1)
            data[at:at] = bytes(rng.randrange(256) for _ in range(rng.randint(1, 16)))
    return bytes(data)


_HOSTILE_FRAGMENTS = (
    "\x1b[31m", "\x1b[", "\r", "\x00", "(", ")", "[", "]", ":" * 40, "9" * 30, " [x.vcxproj]", "../", "C:\\",
    "\u202e", "FAILED: ", "[1/2] ", "CMake Error at ", " error ", "*** [", ".... ", "Note: including file: ",
    "\t", " " * 50, "'", '"', "$(", "%(", "\\\\", "\u2028", "-D", ";", "\n",
)


def _mutate_text(rng: random.Random, text: str, donors: list[str]) -> str:
    for _ in range(rng.randint(1, 3)):
        lines = text.splitlines(keepends=True) or [""]
        op = rng.randrange(8)
        if op == 0 and text:
            chars = list(text)
            for _ in range(rng.randint(1, 8)):
                chars[rng.randrange(len(chars))] = rng.choice("0x#:()[]{}\"'\\\n\r\x1b\x00 .-/,;=%$@")
            text = "".join(chars)
        elif op == 1:
            text = text[: rng.randrange(len(text) + 1)]
        elif op == 2:
            index = rng.randrange(len(lines))
            text = "".join(lines[:index] + [lines[index]] * rng.randint(2, 40) + lines[index:])
        elif op == 3:
            rng.shuffle(lines)
            text = "".join(lines)
        elif op == 4:
            at = rng.randrange(len(text) + 1)
            text = text[:at] + rng.choice(_HOSTILE_FRAGMENTS) * rng.randint(1, 30) + text[at:]
        elif op == 5:
            donor = rng.choice(donors)
            start = rng.randrange(len(donor) + 1)
            at = rng.randrange(len(text) + 1)
            text = text[:at] + donor[start:start + rng.randint(1, 400)] + text[at:]
        elif op == 6:
            text = "".join(line for line in lines if rng.random() > 0.25)
        else:
            index = rng.randrange(len(lines))
            lines[index] = lines[index][:1] + "9" * rng.randint(10, 3000) + lines[index][1:]
            text = "".join(lines)
    return text


# ===================================================================================
# 1. ELF identity
# ===================================================================================

def _encode_notes(notes: list[tuple[bytes, int, bytes]], endian: str, step: int) -> bytes:
    out = bytearray()
    for name, n_type, desc in notes:
        raw_name = name + b"\x00"
        out += struct.pack(endian + "III", len(raw_name), len(desc), n_type)
        out += raw_name + b"\x00" * (-len(raw_name) % step)
        out += desc + b"\x00" * (-len(desc) % step)
    return bytes(out)


def build_elf(bits: int, endian: str, notes: list[tuple[bytes, int, bytes]], *, via: str = "phdr",
              align: int = 4, debuglink: bytes = b"", machine: int = 62, e_type: int = 3) -> bytes:
    """ELF32/ELF64 following the gABI layouts, with notes in PT_NOTE or SHT_NOTE."""
    ident = b"\x7fELF" + bytes([1 if bits == 32 else 2, 1 if endian == "<" else 2, 1]) + b"\x00" * 9
    ehsize = 52 if bits == 32 else 64
    phentsize = 32 if bits == 32 else 56
    shentsize = 40 if bits == 32 else 64
    step = align if via == "phdr" else 4
    blob = _encode_notes(notes, endian, step)
    if via == "phdr":
        note_off = ehsize + phentsize
        body = blob
        phdr_fields = (PT_NOTE, 4, note_off, 0, 0, len(blob), len(blob), align)
        if bits == 64:
            phdr = struct.pack(endian + "IIQQQQQQ", phdr_fields[0], phdr_fields[1], *phdr_fields[2:])
            ehdr = struct.pack(endian + "HHIQQQIHHHHHH", e_type, machine, 1, 0, ehsize, 0, 0, ehsize,
                               phentsize, 1, shentsize, 0, 0)
        else:
            phdr = struct.pack(endian + "IIIIIIII", PT_NOTE, note_off, 0, 0, len(blob), len(blob), 4, align)
            ehdr = struct.pack(endian + "HHIIIIIHHHHHH", e_type, machine, 1, 0, ehsize, 0, 0, ehsize,
                               phentsize, 1, shentsize, 0, 0)
        return ident + ehdr + phdr + body
    strtab = b"\x00.note\x00.gnu_debuglink\x00.shstrtab\x00"
    note_off = ehsize
    link_off = note_off + len(blob)
    link_data = debuglink + b"\x00" if debuglink else b"\x00\x00\x00\x00"
    str_off = link_off + len(link_data)
    shoff = str_off + len(strtab)
    sections = [
        (0, 0, 0, 0, 0, 0),
        (1, 7, note_off, len(blob), 0, 0),
        (7, 1, link_off, len(link_data), 0, 0),
        (22, 3, str_off, len(strtab), 0, 0),
    ]
    table = b""
    for name, s_type, offset, size, link, info in sections:
        if bits == 64:
            table += struct.pack(endian + "IIQQQQIIQQ", name, s_type, 0, 0, offset, size, link, info, 4, 0)
        else:
            table += struct.pack(endian + "IIIIIIIIII", name, s_type, 0, 0, offset, size, link, info, 4, 0)
    if bits == 64:
        ehdr = struct.pack(endian + "HHIQQQIHHHHHH", e_type, machine, 1, 0, 0, shoff, 0, ehsize, 0, 0,
                           shentsize, len(sections), 3)
    else:
        ehdr = struct.pack(endian + "HHIIIIIHHHHHH", e_type, machine, 1, 0, 0, shoff, 0, ehsize, 0, 0,
                           shentsize, len(sections), 3)
    return ident + ehdr + blob + link_data + strtab + table


_HEX_ID = re.compile(r"^(?:[0-9a-f]{2}){0,64}$")


def _elf_seeds() -> list[tuple[bytes, str, str]]:
    """(bytes, expected build id hex, expected debuglink)."""
    rng = random.Random(7)
    out = []
    for bits, endian, via, align in (
            (64, "<", "phdr", 4), (64, ">", "phdr", 8), (32, "<", "phdr", 4), (32, ">", "phdr", 4),
            (64, "<", "shdr", 4), (32, ">", "shdr", 4)):
        build_id = bytes(rng.randrange(256) for _ in range(20))
        notes = [(b"GNU", 1, struct.pack(endian + "IIII", 0, 3, 2, 0)), (b"GNU", 3, build_id)]
        link = b"game.debug" if via == "shdr" else b""
        out.append((build_elf(bits, endian, notes, via=via, align=align, debuglink=link),
                    build_id.hex(), link.decode()))
    return out


def _check_identity(ident) -> None:
    assert _HEX_ID.fullmatch(ident.build_id), "build id %r is not bounded lowercase hex" % ident.build_id
    assert len(ident.debuglink) <= 255
    assert all(ord(ch) >= 32 and ord(ch) != 127 for ch in ident.debuglink), "control char in debuglink"
    assert ident.bits in (32, 64) and ident.endian in ("little", "big")
    assert isinstance(ident.machine, str) and isinstance(ident.type, str)


def test_elf_seed_corpus_yields_expected_identity():
    for data, build_id, debuglink in _elf_seeds():
        ident = read_elf_identity(BytesReader(data))
        assert ident.build_id == build_id
        assert ident.debuglink == debuglink
        assert ident.machine == "x86_64" and ident.type == "dyn"


def test_fuzz_elf_identity_mutations():
    rng = _rng(1)
    seeds = [item[0] for item in _elf_seeds()]
    timings: list[float] = []
    outcomes = {"ok": 0, "err": 0}
    for _ in range(9000):
        data = _mutate_bytes(rng, rng.choice(seeds), seeds, hot=200)

        def parse(blob: bytes):
            reader = BytesReader(blob)
            header = read_elf_header(reader)
            assert header.bits in (32, 64) and header.endian in "<>"
            for field in (header.phoff, header.shoff, header.phnum, header.shnum, header.shstrndx):
                assert isinstance(field, int) and field >= 0
            return read_elf_identity(reader)

        ident, error = _run("elf identity", parse, data, (BinaryFormatError,), timings)
        if error is None:
            _check_identity(ident)
            outcomes["ok"] += 1
        else:
            outcomes["err"] += 1
    _check_timings("elf identity", timings)
    assert outcomes["ok"] and outcomes["err"], outcomes


def test_fuzz_elf_field_boundary_sweep_2_32_and_2_64():
    """Every 2-byte-aligned header position overwritten with 32/64-bit boundary integers."""
    timings: list[float] = []
    for data, _build_id, _link in _elf_seeds():
        for at in range(0, min(len(data) - 8, 260), 2):
            for width, values in ((4, _BOUNDARY32[-4:]), (8, _BOUNDARY64)):
                for value in values:
                    for order in ("little", "big"):
                        if width == 4 and order == "big" and value in (0, 1):
                            continue
                        patched = bytearray(data)
                        patched[at:at + width] = value.to_bytes(width, order)
                        ident, error = _run("elf boundary sweep", lambda b: read_elf_identity(BytesReader(b)),
                                            bytes(patched), (BinaryFormatError,), timings)
                        if error is None:
                            _check_identity(ident)
    _check_timings("elf boundary sweep", timings)


def test_fuzz_elf_known_positive_build_id_found_among_decoys():
    rng = _rng(2)
    timings: list[float] = []
    for _ in range(1500):
        bits, endian = rng.choice((32, 64)), rng.choice("<>")
        via = rng.choice(("phdr", "shdr"))
        align = 8 if (via == "phdr" and bits == 64 and rng.random() < 0.5) else 4
        want = bytes(rng.randrange(256) for _ in range(rng.choice((1, 8, 16, 20, 32, 64))))
        decoys = []
        for _ in range(rng.randint(0, 5)):
            kind = rng.randrange(5)
            if kind == 0:
                decoys.append((b"GNU", 1, bytes(rng.randrange(256) for _ in range(16))))
            elif kind == 1:
                decoys.append((b"GNU", 3, b""))                      # empty id: skipped
            elif kind == 2:
                decoys.append((b"GNU", 3, bytes(65)))                # over 64 bytes: skipped
            elif kind == 3:
                decoys.append((b"GN" + bytes([rng.choice((0x56, 0x55 ^ 0x20, 0x4E))]), 3, bytes(range(20))))
            else:
                name = bytes(rng.randrange(1, 256) for _ in range(rng.randint(0, 9)))
                n_type = rng.randrange(0, 2 ** 32)
                if name == b"GNU" and n_type == 3:
                    n_type = 4
                decoys.append((name, n_type, bytes(rng.randrange(256) for _ in range(rng.randint(0, 40)))))
        notes = decoys + [(b"GNU", 3, want)]
        data = build_elf(bits, endian, notes, via=via, align=align, e_type=rng.choice((2, 3)))
        ident, error = _run("elf known-positive", lambda b: read_elf_identity(BytesReader(b)), data,
                            (), timings)
        assert error is None
        if ident.build_id != want.hex():
            _fail("elf known-positive", data, "build id %r != planted %r" % (ident.build_id, want.hex()))
    _check_timings("elf known-positive", timings)


def test_fuzz_iter_notes_roundtrip_and_truncation_prefix():
    rng = _rng(3)
    timings: list[float] = []
    for _ in range(4000):
        endian = rng.choice("<>")
        align = rng.choice((4, 8))
        step = 8 if align == 8 else 4
        notes = [(bytes(rng.randrange(1, 256) for _ in range(rng.randint(0, 12))), rng.randrange(0, 2 ** 32),
                  bytes(rng.randrange(256) for _ in range(rng.randint(0, 40)))) for _ in range(rng.randint(0, 8))]
        blob = _encode_notes(notes, endian, step)
        got, error = _run("iter_notes roundtrip", lambda b: list(iter_notes(b, endian, align)), blob, (), timings)
        assert error is None
        if got != notes:
            _fail("iter_notes roundtrip", blob, "yielded %r != encoded %r" % (got, notes))
        cut = blob[: rng.randrange(len(blob) + 1)]
        got, _ = _run("iter_notes truncated", lambda b: list(iter_notes(b, endian, align)), cut, (), timings)
        if got != notes[: len(got)]:
            _fail("iter_notes truncated", cut, "yielded %r is not a prefix of %r" % (got, notes))
        consumed = sum(len(n) + len(d) for n, _t, d in got)
        if consumed > len(cut):
            _fail("iter_notes truncated", cut, "returned %d payload bytes from %d" % (consumed, len(cut)))
    _check_timings("iter_notes", timings)


def test_fuzz_iter_notes_hostile_sizes_never_overread_or_loop():
    rng = _rng(4)
    timings: list[float] = []
    sizes = _BOUNDARY32 + _BOUNDARY64 + (11, 12, 13, 2 ** 31 - 4)
    for _ in range(4000):
        endian = rng.choice("<>")
        align = rng.choice((0, 1, 4, 8, 16, 2 ** 64 - 1))
        chunks = bytearray()
        for _ in range(rng.randint(0, 6)):
            namesz, descsz = rng.choice(sizes) % 2 ** 32, rng.choice(sizes) % 2 ** 32
            chunks += struct.pack(endian + "III", namesz, descsz, rng.choice(sizes) % 2 ** 32)
            chunks += bytes(rng.randrange(256) for _ in range(rng.randint(0, 24)))
        data = bytes(chunks)
        got, _ = _run("iter_notes hostile", lambda b: list(iter_notes(b, endian, align)), data, (), timings)
        assert len(got) <= 65_536
        assert sum(len(n) + len(d) for n, _t, d in got) <= len(data)
        _run("build_id_from_notes", lambda b: build_id_from_notes(b, endian, align), data, (), timings)
    _check_timings("iter_notes hostile", timings)


def test_iter_notes_zero_length_notes_stop_at_the_65536_note_cap():
    blob = b"\x00" * (12 * 70_000)
    started = time.perf_counter()
    count = len(list(iter_notes(blob, "<", 4)))
    assert time.perf_counter() - started < 2.0
    assert count == 65_536


def test_elf_core_type_skips_section_scan_and_notes_still_bounded():
    data = build_elf(64, "<", [(b"GNU", 3, bytes(range(20)))], via="phdr", e_type=ET_CORE)
    assert read_elf_identity(BytesReader(data)).build_id == bytes(range(20)).hex()


# ===================================================================================
# 2. bounded JSON and guarded XML
# ===================================================================================

def _nested(depth: int, rng: random.Random) -> str:
    """Valid JSON with exact bracket depth ``depth`` and bracket-laden strings at random places."""
    noise = ['"]]]}}"', '"[{"', '"\\"[[["', '"\\\\"', '"x"', "1", "null"]
    text = "1"
    for level in range(depth):
        if level % 2 == 0 or rng.random() < 0.5:
            text = "[" + rng.choice(noise) + "," + text + "]"
        else:
            text = '{"k' + rng.choice(("]", "[", "{", "")) + '":' + text + ',"n":' + rng.choice(noise) + "}"
    return text


def _true_depth(value: object) -> int:
    deepest, stack = 0, [(value, 1)]
    while stack:
        item, depth = stack.pop()
        children = item.values() if isinstance(item, dict) else item if isinstance(item, list) else None
        if children is not None:
            deepest = max(deepest, depth)
            stack.extend((child, depth + 1) for child in children)
    return deepest


def test_fuzz_check_depth_exact_at_limit_and_limit_plus_one():
    rng = _rng(10)
    timings: list[float] = []
    for _ in range(1500):
        limit = rng.randint(1, 70)
        depth = max(1, limit + rng.choice((-1, 0, 0, 1, 1, 2)))
        text = _nested(depth, rng)
        assert _true_depth(json.loads(text)) == depth
        result, error = _run("check_depth", lambda t: check_depth(t, limit), text, (JsonBoundsExceeded,), timings)
        if depth <= limit:
            if error is not None or result != depth:
                _fail("check_depth", (limit, text), "limit %d depth %d gave %r %r" % (limit, depth, result, error))
        elif error is None:
            _fail("check_depth", (limit, text), "depth %d accepted at limit %d" % (depth, limit))
        # loads_bounded applies the same bound and returns exactly json.loads
        value, error = _run("loads_bounded depth", lambda t: loads_bounded(t, max_bytes=10 ** 7, max_depth=limit),
                            text, (JsonBoundsExceeded,), timings)
        if depth <= limit:
            assert error is None and value == json.loads(text)
        else:
            assert error is not None
    _check_timings("check_depth", timings)


def test_fuzz_loads_bounded_byte_limit_counts_utf8_bytes_exactly():
    rng = _rng(11)
    for _ in range(600):
        body = "".join(rng.choice("aé€😀\\u00e9 ") for _ in range(rng.randint(0, 40)))
        text = json.dumps(body, ensure_ascii=False)
        size = len(text.encode("utf-8"))
        assert loads_bounded(text, max_bytes=size) == body
        assert loads_bounded(text.encode("utf-8"), max_bytes=size) == body
        for blob in (text, text.encode("utf-8")):
            with pytest.raises(JsonBoundsExceeded):
                loads_bounded(blob, max_bytes=size - 1)
    for bad in (123, None, 1.5, ["[]"]):
        with pytest.raises(JsonBoundsExceeded):
            loads_bounded(bad, max_bytes=100)
    with pytest.raises(JsonBoundsExceeded):
        loads_bounded(b"\xff\xfe[1]\x80", max_bytes=100)


_JSON_SEEDS = [
    '{"traceEvents":[{"name":"a","ph":"X","ts":1,"dur":2,"args":{"s":"]]}}"}},{"name":"b"}],"meta":[1,2,{"x":null}]}',
    '[{"a":1.5e3,"b":[true,false,null],"c":"\\u00e9\\ud83d\\ude00"},{"d":{"e":{"f":[[[]]]}}}]',
    '{"k":"v","n":12345678901234567890,"neg":-0.0,"arr":[1,2,3]}',
    '[1,2,3,"x"]', '"str"', "null", '{"a":{"a":{"a":{}}}}',
]


def test_fuzz_loads_bounded_matches_json_loads_or_raises_only_bounds_error():
    rng = _rng(12)
    timings: list[float] = []
    accepted = refused = 0
    for _ in range(6000):
        text = _mutate_text(rng, rng.choice(_JSON_SEEDS), _JSON_SEEDS)
        limit = rng.choice((1, 2, 3, 5, 64))
        max_bytes = rng.choice((10, 60, 200, 10 ** 6))
        value, error = _run("loads_bounded", lambda t: loads_bounded(t, max_bytes=max_bytes, max_depth=limit),
                            text, (JsonBoundsExceeded,), timings)
        try:
            reference = json.loads(text)
        except (ValueError, RecursionError):
            reference = None
            decodable = False
        else:
            decodable = True
        if error is None:
            accepted += 1
            if not decodable or repr(value) != repr(reference):
                _fail("loads_bounded", text, "accepted %r but json.loads gave %r" % (value, reference))
            if _true_depth(value) > limit or len(text.encode("utf-8", "surrogatepass")) > max_bytes:
                _fail("loads_bounded", text, "accepted beyond depth %d / %d bytes" % (limit, max_bytes))
        else:
            refused += 1
            if decodable and _true_depth(reference) <= limit and len(text.encode("utf-8", "surrogatepass")) <= max_bytes:
                _fail("loads_bounded", text, "refused a decodable in-bounds document: %s" % error)
    _check_timings("loads_bounded", timings)
    assert accepted and refused


def test_loads_bounded_hostile_shapes_are_bounded_errors_and_fast():
    started = time.perf_counter()
    cases = ["[" * 200_000, "[" * 100_000 + "]" * 100_000, '"' + "\\" * 100_000, '{"a":' * 50_000,
             "1" * 100_000, '["' + "[" * 100_000 + '"]', "[" * 63 + "]" * 63]
    for text in cases:
        try:
            loads_bounded(text, max_bytes=10 ** 7)
        except JsonBoundsExceeded:
            pass
    assert time.perf_counter() - started < 2.0 * len(cases)
    assert loads_bounded("[" * 64 + "]" * 64, max_bytes=1000) == json.loads("[" * 64 + "]" * 64)
    with pytest.raises(JsonBoundsExceeded):
        loads_bounded("[" * 65 + "]" * 65, max_bytes=1000)


def _scan_doc(rng: random.Random):
    """(text, expected objects under traceEvents) with decoy keys, strings and non-objects."""
    events = []
    for _ in range(rng.randint(0, 12)):
        kind = rng.randrange(6)
        if kind == 0:
            events.append(rng.choice((1, "s", None, True, [1, {"z": 1}])))
        else:
            events.append({"n": rng.choice(("a", 'q"]}', "\\", "é😀")), "i": rng.randrange(100),
                           "args": {"deep": [{"x": rng.choice(("]", "}", "{"))}]}})
    members = [("decoy", '"traceEvents":[{"fake":1}]'), ("other", [{"o": 1}, 2]),
               ("traceEvents", events), ("tail", {"traceEvents": [{"t": 1}]})]
    rng.shuffle(members)
    parts = []
    for key, value in members:
        parts.append(json.dumps(key) + ":" + json.dumps(value))
    text = "{" + ",".join(parts) + "}"
    return text, [item for item in events if isinstance(item, dict)]


def test_fuzz_iter_array_objects_is_chunking_invariant_and_ignores_decoys():
    rng = _rng(13)
    timings: list[float] = []
    for _ in range(1500):
        text, expected = _scan_doc(rng)
        raw = text.encode("utf-8")
        cuts = sorted(rng.sample(range(len(raw) + 1), min(len(raw) + 1, rng.randint(0, 12))))
        chunks = [raw[a:b] for a, b in zip([0] + cuts, cuts + [len(raw)], strict=True)]

        def scan(parts):
            scanner = iter_array_objects(parts, max_items=10_000)
            return list(scanner), scanner

        (items, scanner), error = _run("iter_array_objects", scan, chunks, (JsonBoundsExceeded,), timings)
        if error is not None:
            _fail("iter_array_objects", text, "refused a valid document: %s" % error)
        if items != expected:
            _fail("iter_array_objects", (text, [len(c) for c in chunks]), "got %r expected %r" % (items, expected))
        assert scanner.found and scanner.complete and scanner.items == len(expected)
        cap = rng.randint(0, max(1, len(expected)))
        capped = list(iter_array_objects([text], max_items=cap))
        assert capped == expected[:cap]
    _check_timings("iter_array_objects", timings)


def test_fuzz_iter_array_objects_mutations_only_bounds_error_and_respect_limits():
    rng = _rng(14)
    timings: list[float] = []
    seeds = [_scan_doc(random.Random(i))[0] for i in range(6)] + _JSON_SEEDS
    for _ in range(3000):
        text = _mutate_text(rng, rng.choice(seeds), seeds)
        max_items = rng.choice((0, 1, 3, 1000))
        item_bytes = rng.choice((2, 16, 64, 65_536))
        depth = rng.choice((2, 4, 64))

        def scan(blob: str):
            scanner = iter_array_objects([blob[i:i + 37] for i in range(0, len(blob), 37)] or [""], max_items=max_items,
                                         max_item_bytes=item_bytes, max_depth=depth)
            return list(scanner), scanner

        out, error = _run("iter_array_objects mutated", scan, text, (JsonBoundsExceeded,), timings)
        if error is not None:
            continue
        items = out[0]
        if len(items) > max_items:
            _fail("iter_array_objects mutated", text, "%d items over max_items %d" % (len(items), max_items))
        for item in items:
            if not isinstance(item, dict) or len(json.dumps(item)) > item_bytes * 4 + 64:
                _fail("iter_array_objects mutated", text, "item %r not a bounded dict" % (item,))
            if _true_depth(item) > depth:
                _fail("iter_array_objects mutated", text, "item deeper than %d" % depth)
    _check_timings("iter_array_objects mutated", timings)


_XML_BOMB = (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
             b'<!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
             b'<!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">'
             b'<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]>'
             b'<lolz>&lol3;</lolz>')
_XML_XXE = (b'<?xml version="1.0"?><!DOCTYPE foo [<!ELEMENT foo ANY>'
            b'<!ENTITY xxe SYSTEM "file:///etc/passwd">]><foo>&xxe;</foo>')
_XML_DTD = b'<?xml version="1.0"?><!DOCTYPE note SYSTEM "http://example.invalid/note.dtd"><note/>'
_XML_PARAM = (b'<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY % remote SYSTEM "http://example.invalid/e.dtd">'
              b'%remote;]><foo/>')
_XML_HOSTILE = (_XML_BOMB, _XML_XXE, _XML_DTD, _XML_PARAM)
_XML_BENIGN = b'<?xml version="1.0"?><testsuite name="a &amp; b" tests="2"><testcase name="t"/></testsuite>'


def _xml_variants(rng: random.Random, doc: bytes) -> list[bytes]:
    """Transform a hostile document the way an attacker would to dodge a byte regex."""
    text = doc.decode("ascii")
    keyword = rng.choice(("DOCTYPE", "ENTITY"))
    out = [doc]
    swapped = re.sub(keyword, lambda m: "".join(c.lower() if rng.random() < 0.5 else c for c in m.group()), text)
    out.append(swapped.encode())
    out.append(text.replace("<!" + keyword, "<!" + rng.choice((" ", "\t", "\n", "  ")) + keyword).encode())
    out.append(b"\xef\xbb\xbf" + doc)
    out.append(rng.choice((b" ", b"\n", b"<!-- c -->", b"<?pi x?>")) * rng.randint(1, 5) + doc.replace(b'<?xml version="1.0"?>', b""))
    body = text.replace('<?xml version="1.0"?>', "")
    out.append(("\ufeff" + body).encode("utf-16"))
    out.append(("\ufeff" + body).encode("utf-16-be"))
    out.append(('<?xml version="1.0" encoding="UTF-16"?>' + body).encode("utf-16-le"))
    out.append(('<?xml version="1.0" encoding="UTF-16"?>' + body).encode("utf-16-be"))
    out.append(('<?xml version="1.0" encoding="UTF-32"?>' + body).encode("utf-32"))
    out.append(('<?xml version="1.0" encoding="ISO-8859-1"?>' + body).encode("latin-1"))
    out.append(doc.replace(b"<!", b"<!" + b"-" * 0))
    return out


def test_fuzz_xml_hostile_documents_always_refused():
    rng = _rng(15)
    timings: list[float] = []
    count = 0
    for _ in range(250):
        for variant in _xml_variants(rng, rng.choice(_XML_HOSTILE)):
            count += 1
            _tree, error = _run("parse_guarded_xml", parse_guarded_xml, variant, (UnsafeXml,), timings)
            if error is None:
                _fail("parse_guarded_xml", variant, "hostile document was accepted")
    assert count >= 3000
    _check_timings("parse_guarded_xml hostile", timings)


def test_fuzz_xml_mutated_hostile_documents_never_amplify():
    rng = _rng(16)
    timings: list[float] = []
    accepted = 0
    for _ in range(3000):
        seed = rng.choice(_XML_HOSTILE + (_XML_BENIGN,))
        data = _mutate_bytes(rng, seed, list(_XML_HOSTILE))
        tree, error = _run("parse_guarded_xml mutated", parse_guarded_xml, data, (UnsafeXml,), timings)
        if error is None:
            accepted += 1
            rendered = ET.tostring(tree)
            if len(rendered) > 4 * len(data) + 256:
                _fail("parse_guarded_xml mutated", data, "output %d bytes from %d: expansion" % (len(rendered), len(data)))
            if b"<!" in data and re.search(rb"<!\s*(?:DOCTYPE|ENTITY)", data, re.IGNORECASE):
                _fail("parse_guarded_xml mutated", data, "document with a DOCTYPE/ENTITY was accepted")
    _check_timings("parse_guarded_xml mutated", timings)


def test_xml_size_limit_exact_and_benign_documents_equal_et():
    rng = _rng(17)
    for _ in range(200):
        pad = b"<!--" + b"x" * rng.randint(0, 300) + b"-->"
        doc = b"<r a='1'>" + pad + b"<c>t&amp;u</c></r>"
        tree = parse_guarded_xml(doc, max_bytes=len(doc))
        assert ET.tostring(tree) == ET.tostring(ET.fromstring(doc))
        with pytest.raises(UnsafeXml):
            parse_guarded_xml(doc, max_bytes=len(doc) - 1)
    with pytest.raises(UnsafeXml):
        parse_guarded_xml("<r/>", max_bytes=100)            # type: ignore[arg-type]
    started = time.perf_counter()
    for deep in (b"<a>" * 100_000 + b"</a>" * 100_000, b"<a>" * 100_000, b"<a " * 50_000):
        try:
            parse_guarded_xml(deep)
        except UnsafeXml:
            pass
    assert time.perf_counter() - started < 6.0


# ===================================================================================
# 3. build output parsers
# ===================================================================================

def _read_logs() -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8", errors="replace")
            for path in sorted((FIXTURES / "logs").glob("*.log"))}


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _squash(text: str) -> str:
    """Slash-normalized, ANSI-free text with every space/control character removed."""
    text = re.sub(r"\x1b\[?", "", _ANSI.sub("", text)).replace("\\", "/")
    return "".join(ch for ch in text if not ch.isspace() and not unicodedata.category(ch).startswith("C"))


def _derived(value: str, haystack: str | None) -> bool:
    return haystack is None or _squash(value) in haystack


def _check_output_invariants(text: str, max_diagnostics: int) -> None:
    lines = text.splitlines()
    # ANSI stripping has its own grammar in the parser; derivation is checked on ESC-free input only.
    squashed = _squash(text) if "\x1b" not in text else None
    dset = build_output.parse_build_diagnostics(text, max_diagnostics=max_diagnostics, configure=True)
    limit = max(1, min(max_diagnostics, 200))
    assert len(dset.diagnostics) <= limit, "%d diagnostics over max %d" % (len(dset.diagnostics), limit)
    for diag in dset.diagnostics:
        assert 1 <= diag.raw_line_no <= len(lines), "raw_line_no %d outside %d lines" % (diag.raw_line_no, len(lines))
        if diag.file and not _derived(diag.file, squashed):
            raise AssertionError("diagnostic file %r not in input" % diag.file)
        if diag.line is not None and str(diag.line) not in text:
            raise AssertionError("diagnostic line %r not in input" % diag.line)
    for attribution in build_output.parse_msbuild_projects(text, dset):
        assert 1 <= attribution.raw_line_no <= len(lines)
        for value in (attribution.project, attribution.tu_label):
            if value and not _derived(value, squashed):
                raise AssertionError("msbuild attribution %r not in input" % value)
    for segments in (build_output.parse_ninja_segments(text), build_output.parse_make_failures(text)):
        assert len(segments) <= build_output.MAX_SEGMENTS
        for seg in segments:
            assert 1 <= seg.first_line <= seg.last_line <= max(1, len(lines)), seg
            for value in (seg.step_label, seg.tu_label):
                if value and not _derived(value, squashed):
                    raise AssertionError("segment label %r not in input" % value)
    assert len(build_output.parse_cmake_configure_errors(text)) <= 200
    assert len(build_output.parse_msvc_codeless(text)) <= 800
    for trace in (build_output.parse_dash_H(text, root_file="src/root.cpp"),
                  build_output.parse_show_includes(text, root_file="src/root.cpp")):
        assert len(trace.edges) <= build_output.MAX_TRACE_EDGES
        known = {"src/root.cpp"}
        for parent, child, depth in trace.edges:
            assert 1 <= depth <= 256
            if parent not in known:
                raise AssertionError("edge parent %r never appeared" % parent)
            if not _derived(child, squashed):
                raise AssertionError("include %r not in input" % child)
            known.add(child)
        assert trace.unique_headers == len({edge[1] for edge in trace.edges})
        assert trace.max_depth == max([edge[2] for edge in trace.edges] or [0])


def test_output_parsers_on_captured_logs_satisfy_invariants_unmutated():
    logs = _read_logs()
    assert len(logs) >= 10
    for name, text in logs.items():
        try:
            _check_output_invariants(text, 200)
        except AssertionError as exc:
            pytest.fail("%s: %s" % (name, exc), pytrace=False)
    ninja = build_output.parse_ninja_segments(logs["ninja-gxx.log"])
    assert any(seg.failed for seg in ninja)
    assert build_output.parse_dash_H(logs["trace-gxx-H.log"], root_file="x.cpp").edges


def test_fuzz_build_output_parsers_never_raise_and_stay_derived():
    rng = _rng(20)
    logs = list(_read_logs().values())
    timings: list[float] = []
    for _ in range(900):
        text = _mutate_text(rng, rng.choice(logs), logs)
        max_diag = rng.choice((1, 2, 5, 200, 10 ** 9, 0, -3))
        started = time.perf_counter()
        try:
            _check_output_invariants(text, max_diag)
        except AssertionError as exc:
            _fail("build output", text, "%s (max_diagnostics=%d)" % (exc, max_diag))
        except BaseException as exc:  # noqa: BLE001
            _fail("build output", text, "raised %s: %s" % (type(exc).__name__, str(exc)[:200]))
        timings.append(time.perf_counter() - started)
    ordered = sorted(timings)
    # Each input runs all eight parsers, so the per-input p99 budget is 8 x 50 ms.
    assert ordered[int(len(ordered) * 0.99) - 1] < 0.4, "[FUZZ_SEED=%d] p99 %.0f ms" % (SEED, ordered[int(len(ordered) * 0.99) - 1] * 1000)
    assert ordered[-1] < 2.0


_ADVERSARIAL_LINES = [
    "[1/2] " + "a " * 1500, "[" + "9" * 4000, "FAILED: " + "x " * 1500, "[1/9] " + "A " * 20 + " object " * 300,
    "make[1]: *** [" + "a:" * 1500, "make: *** [" + "x" * 4000, "[ 10%] Building CXX object " + "a" * 4000,
    "CMake Error at " + "a:" * 1500, "CMake Error at " + "a" * 1500 + ":" + "1" * 30,
    "a(" * 1500 + "1,2): error: x", "a" * 1000 + "(" + "1" * 20, "C:" + "x(" * 1500 + ") : error : m",
    "(1,2): error: " + " " * 3000 + ":", " [" * 1500 + "a" * 100, "x " * 400 + " [" + "a" * 1000 + ".vcxproj]x",
    "\x1b[" * 1000, "\x1b[" + "0;" * 1500, ". " * 1500, "." * 4000, "Note: including file:" + " " * 4000 + "x",
    "Note: including file: " * 150, "    1>" + " " * 3000 + "a.cpp", " " * 4000 + "x.cpp",
    "a.cpp:" + "1:" * 1500 + " error: " + "x" * 100, "/a/b.cpp:1:1: error: " + "[" * 1500, "error " * 600 + "C" + "1" * 30,
    "src/" + "../" * 1300 + "a.c:1:2: warning: w", "In file included from " + "a.h:1:" * 600,
]


def test_build_output_parsers_have_no_redos_on_long_lines_and_10k_line_inputs():
    funcs = (
        lambda t: build_output.parse_build_diagnostics(t, configure=True),
        build_output.parse_ninja_segments, build_output.parse_make_failures,
        build_output.parse_cmake_configure_errors, build_output.parse_msvc_codeless,
        lambda t: build_output.parse_dash_H(t, root_file="r.cpp"),
        lambda t: build_output.parse_show_includes(t, root_file="r.cpp"),
    )
    worst = (0.0, "")
    for line in _ADVERSARIAL_LINES:
        for text in ((line + "\n") * min(10_000, max(8, 500_000 // max(1, len(line)))), line):
            for index, func in enumerate(funcs):
                started = time.perf_counter()
                func(text)
                spent = time.perf_counter() - started
                worst = max(worst, (spent, "func %d on %r x%d" % (index, line[:60], text.count("\n"))))
    assert worst[0] < 2.0, "[FUZZ_SEED=%d] slowest %.2fs: %s" % (SEED, worst[0], worst[1])
    dset = build_output.parse_build_diagnostics("\n".join("a.c:%d:1: error: x%d" % (i, i) for i in range(10_000)), max_diagnostics=7)
    assert len(dset.diagnostics) == 7 and dset.truncated


def test_build_output_known_positive_diagnostics_survive_padding_and_case():
    rng = _rng(21)
    timings: list[float] = []
    for _ in range(600):
        junk = "\n".join(rng.choice(("[3/9] Building CXX object x.o", "  note: noise", "", "\x1b[0m", "ok " * 5))
                         for _ in range(rng.randint(0, 6)))
        name = "src/%s%d.cpp" % (rng.choice(("a", "Zed", "core_")), rng.randrange(1000))
        sev = rng.choice(("error", "warning"))
        line_no = rng.randint(1, 9999)
        planted = "%s:%d:%d: %s: planted failure here" % (name, line_no, rng.randint(1, 80), sev)
        text = junk + ("\n" if junk else "") + planted + "\n" + junk
        dset, error = _run("parse_build_diagnostics", lambda t: build_output.parse_build_diagnostics(t), text, (), timings)
        assert error is None
        found = [d for d in dset.diagnostics if d.file == name and d.line == line_no and d.severity == sev]
        if len(found) != 1:
            _fail("parse_build_diagnostics", text, "planted diagnostic found %d times" % len(found))
        codeless = "C:/p/%s(%d,%d): %s: planted codeless" % (name.split("/")[1], line_no, rng.randint(1, 80), sev)
        got, _ = _run("parse_msvc_codeless", build_output.parse_msvc_codeless, junk + "\n" + codeless, (), timings)
        if not any(d.line == line_no and d.severity == sev for d in got):
            _fail("parse_msvc_codeless", codeless, "planted codeless diagnostic missed")
    _check_timings("build output known-positive", timings)


# ===================================================================================
# 4. msbuild and compile database
# ===================================================================================

def _msbuild_fixture(rel: str) -> bytes:
    return (FIXTURES / "msbuild" / rel).read_bytes()


def test_fuzz_parse_solution_error_contract_and_bounds():
    rng = _rng(30)
    base = _msbuild_fixture("SparkLite.sln").decode("utf-8-sig")
    donors = [base, "Project(\"{A}\") = \"n\", \"p.vcxproj\", \"{B}\"\nEndProject\n" * 3]
    timings: list[float] = []
    outcomes = {"ok": 0, "err": 0}
    for _ in range(2500):
        text = _mutate_text(rng, rng.choice(donors), donors)
        data = text.encode("utf-8", "replace") if rng.random() < 0.9 else text.encode(rng.choice(("utf-16", "utf-16-be")), "replace")
        info, error = _run("parse_solution", parse_solution, data, (BuildDomainError,), timings)
        if error is not None:
            outcomes["err"] += 1
            continue
        outcomes["ok"] += 1
        assert len(info.projects) <= 512 and len(info.configs) <= 32 and len(info.platforms) <= 64
        assert all(PLATFORM_RE.fullmatch(p) for p in info.platforms)
        assert all(item.is_folder == (item.path == "") for item in info.projects if item.path or item.is_folder)
    _check_timings("parse_solution", timings)
    assert outcomes["ok"] and outcomes["err"], outcomes


def test_parse_solution_limits_exact():
    one = 'Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "p%d", "p%d.vcxproj", "{00000000-0000-0000-0000-%012d}"\r\nEndProject\r\n'
    text = "".join(one % (i, i, i) for i in range(513)).encode()
    info = parse_solution(text)
    assert len(info.projects) == 512 and info.truncated
    assert not parse_solution("".join(one % (i, i, i) for i in range(512)).encode()).truncated
    with pytest.raises(BuildDomainError):
        parse_solution(b"x" * (4 * 1024 * 1024 + 1))
    with pytest.raises(BuildDomainError):
        parse_solution(b"Project(\x00")


def test_fuzz_parse_vcxproj_error_contract_and_xml_bombs_refused():
    rng = _rng(31)
    seeds = [_msbuild_fixture(rel) for rel in ("src/core/core.vcxproj", "src/game/game.vcxproj",
                                               "tools/tools.vcxproj", "props/common.props")]
    timings: list[float] = []
    outcomes = {"ok": 0, "err": 0}
    for _ in range(1500):
        data = _mutate_bytes(rng, rng.choice(seeds), seeds)
        info, error = _run("parse_vcxproj", lambda b: parse_vcxproj(b, label="p.vcxproj"), data, (BuildDomainError,), timings)
        if error is None:
            outcomes["ok"] += 1
            for ref in info.imports:
                resolved = resolve_import_path("src/core/core.vcxproj", ref.project)
                assert resolved is None or _safe_relative(resolved)
        else:
            outcomes["err"] += 1
    base = seeds[0].decode("utf-8-sig")
    for _ in range(300):
        for hostile in _XML_HOSTILE:
            doctype = hostile.split(b"?>")[-1].split(b"]>")[0] + (b"]>" if b"[" in hostile else b"")
            variants = _xml_variants(rng, hostile)
            for variant in variants:
                _tree, error = _run("parse_vcxproj hostile", lambda b: parse_vcxproj(b, label="x"), variant,
                                    (BuildDomainError,), timings)
                if error is None:
                    _fail("parse_vcxproj", variant, "hostile document accepted")
            doc = base.replace("<Project", doctype.decode("latin-1") + "<Project", 1).encode("utf-8")
            _tree, error = _run("parse_vcxproj doctype", lambda b: parse_vcxproj(b, label="x"), doc,
                                (BuildDomainError,), timings)
            if error is None:
                _fail("parse_vcxproj", doc, "project with a DOCTYPE accepted")
        break
    _check_timings("parse_vcxproj", timings)
    assert outcomes["ok"] and outcomes["err"], outcomes


def _contained(path: str) -> bool:
    """Not absolute, no NUL, no '..' component, no drive-relative first component."""
    if not path or "\x00" in path or is_absolute(path) or path.startswith(("/", "\\")):
        return False
    parts = path.replace("\\", "/").split("/")
    if any(part == ".." for part in parts):
        return False
    return not re.match(r"^[A-Za-z]:", parts[0])     # drive-relative ('C:x') leaves the root on a Windows host


def _safe_relative(path: str) -> bool:
    """A resolved import must also carry no unevaluated MSBuild macro or wildcard."""
    return _contained(path) and not any(token in path for token in ("$(", "%(", "@(", "*"))


_IMPORT_PIECES = ("..", ".", "a", "b.props", "$(Foo)", "%(Bar)", "@(Baz)", "C:", "C:x", "", "\\", "/", "//", "*",
                  "$(MSBuildThisFileDirectory)", "$(MSBuildProjectDirectory)", "..\\", "\x00", "\u202e", "%2e%2e", "~", "a:b")


def test_fuzz_resolve_import_path_never_escapes_importer_root():
    rng = _rng(32)
    timings: list[float] = []
    resolved_count = 0
    for _ in range(12000):
        importer = "/".join(rng.choice(("src", "core", "..", ".", "p.vcxproj", "", "C:", "a")) for _ in range(rng.randint(0, 4)))
        importer = rng.choice(("", "x.vcxproj", "src/core/core.vcxproj", "a\\b\\c.props", importer))
        raw = "".join(rng.choice(_IMPORT_PIECES) + rng.choice(("/", "\\", "")) for _ in range(rng.randint(0, 7)))
        path, error = _run("resolve_import_path", lambda r: resolve_import_path(importer, r), raw, (), timings)
        assert error is None
        if path is None:
            continue
        resolved_count += 1
        if not _safe_relative(path):
            _fail("resolve_import_path", (importer, raw), "returned %r, which can leave the source root" % path)
    _check_timings("resolve_import_path", timings)
    assert resolved_count > 500


def test_resolve_import_path_known_escapes_are_refused():
    for raw in ("..\\..\\..\\evil.props", "../../../x.props", "/etc/x", "\\\\host\\share\\x.props",
                "$(VCTargetsPath)\\x.props", "a/../../../../x", "$(MSBuildThisFileDirectory)..\\..\\..\\x", "%(Y)", "@(Z)", "*.props", ""):
        for importer in ("src/core/core.vcxproj", "core.vcxproj"):
            got = resolve_import_path(importer, raw)
            assert got is None, "%r from %r resolved to %r" % (raw, importer, got)
    assert resolve_import_path("src/core/core.vcxproj", "..\\..\\props\\common.props") == "props/common.props"
    assert resolve_import_path("src/core/core.vcxproj", "$(MSBuildThisFileDirectory)x.props") == "src/core/x.props"
    assert resolve_import_path("src/core/core.vcxproj", "../../evil.props") == "evil.props"   # lands at the root, inside it


def test_resolve_import_path_drive_relative_is_not_returned_as_relative():
    """SUSPECT regression: 'C:x' is drive-relative on Windows (os.path.join(root, 'C:x') == 'C:x')."""
    assert resolve_import_path("core.vcxproj", "C:evil.props") is None


def _entry_corpus() -> list[str]:
    return [(FIXTURES / "compile_db" / name).read_text(encoding="utf-8")
            for name in ("linux_compile_commands.json", "windows_compile_commands.json")]


def test_fuzz_parse_compile_commands_error_contract_and_path_containment():
    rng = _rng(33)
    corpus = _entry_corpus()
    timings: list[float] = []
    outcomes = {"ok": 0, "err": 0}
    for _ in range(2500):
        text = _mutate_text(rng, rng.choice(corpus), corpus)
        cap = rng.choice((1, 3, 10, 10 ** 9, 0, -1))
        root, build = rng.choice((("/work/sparklite", "/work/sparklite/build"), ("C:/src/SparkLite", "C:/src/SparkLite/build")))
        db, error = _run("parse_compile_commands",
                         lambda t: parse_compile_commands(t.encode("utf-8", "replace"), source_root=root, build_dir=build, max_entries=cap),
                         text, (BuildDomainError,), timings)
        if error is not None:
            outcomes["err"] += 1
            continue
        outcomes["ok"] += 1
        assert len(db.entries) <= max(1, min(cap, MAX_ENTRIES))
        for entry in db.entries:
            if entry.file_rel and not _contained(entry.file_rel):
                _fail("parse_compile_commands", text, "file_rel %r can escape the source root" % entry.file_rel)
            assert "\x00" not in entry.file and all("\x00" not in arg for arg in entry.argv)
    _check_timings("parse_compile_commands", timings)
    assert outcomes["ok"] and outcomes["err"], outcomes


def test_fuzz_parse_compile_commands_known_positive_escape_is_not_a_relative_path():
    rng = _rng(34)
    for _ in range(400):
        depth = rng.randint(1, 8)
        file = rng.choice(("", "./", "sub/")) + "../" * depth + "etc/passwd"
        entries = [{"directory": "/work/sparklite/build", "file": file, "arguments": ["g++", "-c", file]}]
        db = parse_compile_commands(json.dumps(entries).encode(), source_root="/work/sparklite", build_dir="/work/sparklite/build")
        for entry in db.entries:
            if entry.file_rel and not _contained(entry.file_rel):
                pytest.fail("[FUZZ_SEED=%d] %r produced file_rel %r" % (SEED, file, entry.file_rel), pytrace=False)


def test_parse_compile_commands_limits_exact_and_hostile_documents():
    entries = [{"directory": "/p/b", "file": "/p/a%d.c" % i, "arguments": ["cc", "-c", "/p/a%d.c" % i]} for i in range(5)]
    data = json.dumps(entries).encode()
    assert len(parse_compile_commands(data, source_root="/p", build_dir="/p/b", max_entries=5).entries) == 5
    four = parse_compile_commands(data, source_root="/p", build_dir="/p/b", max_entries=4)
    assert len(four.entries) == 4 and four.truncated
    for bad in (b"{}", b"[" * 100 + b"]" * 100, b"\xff\xfe", b"", b"[1,2]"):
        try:
            result = parse_compile_commands(bad, source_root="/p", build_dir="/p/b")
        except BuildDomainError:
            continue
        assert result.entries == ()


# ===================================================================================
# 5. templates
# ===================================================================================

_SHELL_AND_INJECTION = list(";&|`$<>\"'%^\n\r\t \x00\x7f,=!(){}*?#~") + ["\u2028", "\x0b", "\x85"]
_NAME_ALPHABET = "abcXYZ019_.+-"


def _valid_name(rng: random.Random) -> str:
    return rng.choice("abcXYZ_") + "".join(rng.choice(_NAME_ALPHABET) for _ in range(rng.randint(0, 20)))


@pytest.mark.parametrize("system", list(BuildSystem), ids=lambda s: s.value)
def test_fuzz_validate_target_name_accepted_values_are_inert_and_injection_refused(system):
    rng = _rng(40 + list(BuildSystem).index(system))
    timings: list[float] = []
    allowed_chars = set(_NAME_ALPHABET) | set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
    msbuild = system is BuildSystem.MSBUILD
    for _ in range(3000):
        base = _valid_name(rng)
        mode = rng.randrange(4)
        if mode == 0:
            value = base
        elif mode == 1:
            at = rng.randrange(len(base) + 1)
            value = base[:at] + rng.choice(_SHELL_AND_INJECTION) + base[at:]
        elif mode == 2:
            value = rng.choice(("-", "--", "/", "/p:", "@", " -", "-t:", "\\", ":")) + base
        else:
            value = "".join(rng.choice(_NAME_ALPHABET + ";&|\\:/ \n$\u00e9") for _ in range(rng.randint(0, 30)))
        result, error = _run("validate_target_name", lambda v: validate_target_name(v, system=system), value,
                             (TemplateRejected,), timings)
        if error is None:
            body = value[: -len(":Build")] if msbuild and value.endswith(":Build") else value
            allowed = allowed_chars | ({"\\"} if msbuild else set())
            if result != value or not body or any(ch not in allowed for ch in body):
                _fail("validate_target_name", value, "accepted a value with characters outside [A-Za-z0-9_.+-]")
            if body[0] in "-/\\" or "\n" in value or " " in value:
                _fail("validate_target_name", value, "accepted option-injection or whitespace")
            if msbuild and body.casefold() in {"rebuild", "clean", "publish", "deploy", "restore", "pack", "run", "test", "vstest", "install"}:
                _fail("validate_target_name", value, "accepted a refused MSBuild target")
        elif mode == 1 and value[0] != "\\" and any(c in value for c in "\x00\n\r\t;&|`$<>\"'%^ \x7f,=!(){}*?#~\u2028\x0b\x85"):
            pass
        if mode == 1 and error is None:
            _fail("validate_target_name", value, "planted metacharacter accepted")
    _check_timings("validate_target_name", timings)


def test_validate_target_name_msbuild_case_insensitive_refusals_and_suffix():
    for name in ("Rebuild", "rebuild", "REBUILD", "ReBuild:Build", "clean", "Clean:Build", "vstest", "Install"):
        with pytest.raises(TemplateRejected):
            validate_target_name(name, system=BuildSystem.MSBUILD)
    assert validate_target_name("core:Build", system=BuildSystem.MSBUILD) == "core:Build"
    assert validate_target_name("Engine\\core", system=BuildSystem.MSBUILD) == "Engine\\core"
    for name in ("core:Rebuild", "a:b", "core:Build:Build"):
        with pytest.raises(TemplateRejected):
            validate_target_name(name, system=BuildSystem.MSBUILD)


def test_fuzz_validate_define_accepted_values_are_single_line_names():
    rng = _rng(50)
    timings: list[float] = []
    accepted = 0
    for _ in range(6000):
        name = rng.choice(("FOO", "_x9", "CMAKE_BUILD_TYPE", "A_b_C"))
        typ = rng.choice(("", ":BOOL", ":STRING", ":FILEPATH"))
        value = "".join(rng.choice("abZ09 _./:;,$&|`'\"\\=%-+") for _ in range(rng.randint(0, 30)))
        entry = "-D" + name + typ + "=" + value
        mode = rng.randrange(5)
        if mode == 1:
            at = rng.randrange(len(entry) + 1)
            entry = entry[:at] + rng.choice("\n\r\x00\x0b\x0c\x1c\x85\u2028\u2029") + entry[at:]
        elif mode == 2:
            entry = rng.choice(("", " ", "-", "--", "D", "-d", "-D1", "-D=", "-DX", "-D-X=1", "-DX Y=1")) + entry[rng.randrange(3):]
        elif mode == 3:
            entry = _mutate_text(rng, entry, [entry])
        result, error = _run("validate_define", validate_define, entry, (TemplateRejected,), timings)
        if error is not None:
            continue
        accepted += 1
        if result != entry:
            _fail("validate_define", entry, "returned a different string")
        if not re.fullmatch(r"-D[A-Za-z_][A-Za-z0-9_]*(?::[A-Z]+)?=.*", entry, re.DOTALL):
            _fail("validate_define", entry, "accepted a malformed -D define (option injection)")
        if len(entry.splitlines()) != 1 or "\x00" in entry:
            _fail("validate_define", entry, "accepted a define containing a line break or NUL")
    assert accepted > 500
    _check_timings("validate_define", timings)
    for bad in (None, 5, b"-DX=1", ["-DX=1"]):
        with pytest.raises(TemplateRejected):
            validate_define(bad)         # type: ignore[arg-type]


def test_validate_define_refuses_carriage_return_and_other_line_breaks():
    """SUSPECT regression: DEFINE_RE excludes only NUL and LF, so CR (a line break) is accepted."""
    for ch in ("\r", "\x0b", "\x0c", "\x1c", "\x85", "\u2028", "\u2029"):
        with pytest.raises(TemplateRejected):
            validate_define("-DX=a%sb" % ch)
    assert validate_define("-DCMAKE_BUILD_TYPE:STRING=Release") == "-DCMAKE_BUILD_TYPE:STRING=Release"


def _templates_by_system() -> dict[BuildSystem, object]:
    seen: dict[BuildSystem, object] = {}
    for template in TEMPLATES.values():
        seen.setdefault(template.system, template)
    return seen


def test_fuzz_validate_placeholder_accepted_values_are_inert():
    rng = _rng(51)
    templates = list(_templates_by_system().values()) + [TEMPLATES["ninja.compile_one"], TEMPLATES["msbuild.build"]]
    names = sorted(PLACEHOLDERS) + ["bogus", "net", "defines", ""]
    path_names = {"source_dir", "build_dir", "make_program", "log_file", "binlog", "solution", "project_file"}
    timings: list[float] = []
    accepted = 0
    for _ in range(9000):
        name = rng.choice(names)
        template = rng.choice(templates)
        kind = rng.randrange(5)
        if name in path_names:
            suffix = {"log_file": ".log", "binlog": ".binlog", "solution": ".sln", "project_file": ".vcxproj"}.get(name, "")
            value = rng.choice(("C:\\src\\b", "/work/x", "C:/a b/c")) + suffix
        elif name == "jobs":
            value = str(rng.randrange(0, 300))
        elif name == "file_target":
            value = rng.choice(("src/a.cpp", "src/a.cpp^", "C:/p/a.cpp"))
        else:
            value = _valid_name(rng)
        if kind == 1:
            at = rng.randrange(len(value) + 1)
            value = value[:at] + rng.choice(_SHELL_AND_INJECTION) + value[at:]
        elif kind == 2:
            value = rng.choice(("-", "--", "/", "@", "//", "\\\\", "\\\\?\\", "\\\\.\\")) + value
        elif kind == 3:
            value = _mutate_text(rng, value, [value])
        elif kind == 4:
            value = rng.choice(("٣", "１２", "1e3", "+5", " 5", "5 ", "0x10", "٠")) + rng.choice(("", "7"))
        if name == "jobs" and len(value) > 4000:
            continue                    # >4300 digits is the BUG regression below
        result, error = _run("validate_placeholder", lambda v: validate_placeholder(name, v, template=template),
                             value, (TemplateRejected,), timings)
        if error is not None:
            continue
        accepted += 1
        if name not in PLACEHOLDERS:
            _fail("validate_placeholder", (name, value), "unknown placeholder accepted")
        if "\x00" in result or "\n" in result or "\r" in result:
            _fail("validate_placeholder", (name, value), "accepted NUL/CR/LF")
        if name in path_names:
            if not is_absolute(result) or result.replace("\\", "/").startswith("//") or any(c in result for c in ';"'):
                _fail("validate_placeholder", (name, value), "path placeholder is not an absolute local path")
        elif name == "jobs":
            if not (result.isascii() and result.isdigit() and 1 <= int(result) <= 256 and result == str(int(result))):
                _fail("validate_placeholder", (name, value), "jobs is not a canonical ASCII 1..256 integer")
        elif name == "file_target":
            if result[0] in "-@" or any(c in result for c in ";,%\"'`$&|<>"):
                _fail("validate_placeholder", (name, value), "file target carries option or shell syntax")
        elif name == "generator":
            pass
        elif result[0] in "-/" or any(c in result for c in ";,%\"'`$&|<>^=\t"):
            _fail("validate_placeholder", (name, value), "model value carries option or shell syntax")
    assert accepted > 1500
    _check_timings("validate_placeholder", timings)


# ===================================================================================
# 6. lower priority
# ===================================================================================

def test_fuzz_parse_port_range_bounds():
    rng = _rng(60)
    timings: list[float] = []
    accepted = 0
    for _ in range(8000):
        lo, hi = rng.choice((0, 1, 1023, 1024, 65535, 65536, 99999, rng.randrange(100000))), rng.choice((0, 1024, 65535, 65536, rng.randrange(100000)))
        pad = lambda: rng.choice(("", " ", "\t", "\n", "  "))
        text = pad() + str(lo) + pad() + rng.choice(("-", "-", "–", ":", "--")) + pad() + str(hi) + pad()
        if rng.random() < 0.2:
            text = _mutate_text(rng, text, [text, "٣٤٥٦-٣٤٥٧", "00001024-1025", "1024-1025-1026", ""])
        result, error = _run("parse_port_range", parse_port_range, text, (InvalidInput,), timings)
        if error is not None:
            continue
        accepted += 1
        low, high = result
        if not 1024 <= low <= high <= 65535:
            _fail("parse_port_range", text, "accepted %r outside 1024 <= lo <= hi <= 65535" % (result,))
    assert accepted > 100
    _check_timings("parse_port_range", timings)
    for good, want in (("47000-47999", (47000, 47999)), (" 1024 - 1024 ", (1024, 1024)), ("65535-65535", (65535, 65535))):
        assert parse_port_range(good) == want
    for bad in ("1023-2000", "2000-1024", "1024-65536", "", None, "-", "1-", "٣٤٥-٣٤٦٧", "1024--2000", "1024-2000-3000"):
        with pytest.raises(InvalidInput):
            parse_port_range(bad)           # type: ignore[arg-type]


def test_fuzz_parse_watched_task_window_bounds():
    rng = _rng(61)
    timings: list[float] = []
    accepted = 0
    for _ in range(6000):
        name = rng.choice(("Backup", "a b", "", " ", "x=y", "\\Sonder\\Nightly"))
        hours = rng.choice(("1", "0", "-1", "24", "8784", "8784.0001", "nan", "inf", "-inf", "1e400", "1e2", "0x10", " 2 ", "", "1_0", "٣", ".5", "5."))
        text = name + rng.choice(("=", "", "==")) + hours if rng.random() < 0.8 else _mutate_text(rng, name + "=" + hours, [name])
        result, error = _run("parse_watched_task", lambda t: parse_watched_task(t, default_window_hours=26.0), text, (ValueError,), timings)
        if error is not None:
            continue
        accepted += 1
        if not (result.name and result.name == result.name.strip() and 0 < result.window_hours <= 24 * 366):
            _fail("parse_watched_task", text, "accepted %r" % (result,))
        if result.window_hours != result.window_hours:
            _fail("parse_watched_task", text, "NaN window accepted")
    assert accepted > 500
    _check_timings("parse_watched_task", timings)


_SCHTASKS_HEADER = ('"HostName","TaskName","Next Run Time","Status","Logon Mode","Last Run Time",'
                    '"Last Result","Author","Task To Run","Scheduled Task State"')


def test_fuzz_parse_schtasks_csv_error_contract():
    rng = _rng(62)
    seed = _SCHTASKS_HEADER + '\n"HOST","\\Sonder","N/A","Ready","Interactive only","11/15/2026 3:00:00 AM","0","me","cmd","Enabled"\n'
    seeds = [seed, seed.replace("Sonder", "A"), _SCHTASKS_HEADER + "\n" + _SCHTASKS_HEADER + "\n"]
    timings: list[float] = []
    ok = 0
    for _ in range(3000):
        text = _mutate_text(rng, rng.choice(seeds), seeds)
        order = rng.choice(list(DateOrder))
        state, error = _run("parse_schtasks_csv", lambda t: parse_schtasks_csv(t, rng.choice(("Sonder", "A", "\\Sonder", "")), date_order=order),
                            text, (TaskParseError,), timings)
        if error is None:
            ok += 1
            assert state.last_result is None or isinstance(state.last_result, int)
    assert ok > 300
    _check_timings("parse_schtasks_csv", timings)
    state = parse_schtasks_csv(seed, "\\Sonder", date_order=DateOrder.MDY)
    assert state.last_result == 0 and state.scheduler_state == "Enabled" and state.last_run is not None


def test_parse_schtasks_csv_malformed_csv_raises_only_task_parse_error():
    """BUG regression: csv.Error (bare CR in an unquoted field, field over 131072) escapes the typed error."""
    header = '"TaskName","Last Run Time","Last Result","Scheduled Task State"\n'
    for text in (header + '"x"\r"y"\n', header + '"x","a"b\rc","1","R"\n', header + "x" * 200_000 + "\n"):
        try:
            parse_schtasks_csv(text, "x")
        except TaskParseError:
            continue
        except Exception as exc:  # noqa: BLE001
            pytest.fail("raised %s instead of TaskParseError for %s" % (type(exc).__name__, _show(text)), pytrace=False)


def test_validate_placeholder_jobs_with_thousands_of_digits_is_a_template_rejection():
    """BUG regression: int() on a >4300-digit string raises ValueError, not TemplateRejected."""
    for digits in (4300, 4301, 5000):
        with pytest.raises(TemplateRejected):
            validate_placeholder("jobs", "9" * digits, template=TEMPLATES["ninja.compile_one"])
    assert validate_placeholder("jobs", "256", template=TEMPLATES["ninja.compile_one"]) == "256"


_UNSAFE_CODEPOINTS = set(range(0x20)) | set(range(0x7F, 0xA0)) | set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A)) | \
    set(range(0x200B, 0x2010)) | {0x2060, 0xFEFF}


def test_fuzz_sanitize_limit_and_no_control_or_invisible_characters():
    rng = _rng(63)
    timings: list[float] = []
    pool = [chr(c) for c in sorted(_UNSAFE_CODEPOINTS)] + list("abc XYZ\t\n\r\u00a0\u2028\u2029\u3000") + ["é", "😀", "\x1b[31m"]
    for _ in range(8000):
        text = "".join(rng.choice(pool) for _ in range(rng.randint(0, 80)))
        limit = rng.choice((0, 1, 5, 80, 300, 10 ** 6))
        out, _ = _run("sanitize", lambda t: sanitize(t, limit), text, (), timings)
        if len(out) > limit:
            _fail("sanitize", text, "length %d over limit %d" % (len(out), limit))
        bad = [hex(ord(ch)) for ch in out if ord(ch) in _UNSAFE_CODEPOINTS]
        if bad:
            _fail("sanitize", text, "kept unsafe code points %s" % bad[:5])
        if "\n" in out or "\r" in out or "  " in out or out != out.strip() and limit > len(text):
            _fail("sanitize", text, "whitespace not collapsed: %r" % out)
    _check_timings("sanitize", timings)
    for value in (None, 12, b"x\x00y", ["a\x00"]):
        assert not any(ord(ch) in _UNSAFE_CODEPOINTS for ch in sanitize(value))


def test_fuzz_parse_update_object_error_contract():
    rng = _rng(64)
    seeds = ['{"a":1}', '{"a":{"b":[1,2]}}', "[]", "null", '"s"', "1", "{", ""]
    timings: list[float] = []
    for _ in range(3000):
        text = _mutate_text(rng, rng.choice(seeds), seeds)
        value = rng.choice((text, None, "", {"k": text}, text.encode(), 5, [text]))
        result, error = _run("parse_update_object", lambda v: parse_update_object(v, "update"), value, (ValueError,), timings)
        if error is None and not isinstance(result, dict):
            _fail("parse_update_object", value, "returned %r, not a dict" % (result,))
        if isinstance(value, str) and error is None and value.strip() and result != json.loads(value):
            _fail("parse_update_object", value, "disagrees with json.loads")
    _check_timings("parse_update_object", timings)


def test_fuzz_clangd_uri_to_path_never_yields_nul_and_has_no_other_exception():
    """SUSPECT: ``%00`` in a file URI decodes to a NUL byte in the returned path."""
    rng = _rng(65)
    timings: list[float] = []
    pieces = ["file://", "file:///", "file:////", "host", "share", "%2e%2e", "%2E%2E", "..", "/", "%2f", "%5c", "%5C", "C:", "%3A",
              "a", "%00", "%", "%zz", "\\", "?q=1", "#f", "//", "x y", "%25", "%252e%252e"]
    unc_seen = []
    unc_inputs = []
    for _ in range(6000):
        uri = rng.choice(("file://", "file:///", "file:////", "")) + "".join(rng.choice(pieces) for _ in range(rng.randint(0, 8)))
        path, error = _run("uri_to_path", uri_to_path, uri, (), timings)
        if error is not None:
            continue
        if "\x00" in path:
            _fail("uri_to_path", uri, "decoded a NUL into the path")
        if path.replace("\\", "/").startswith("//"):
            unc_seen.append(uri)
        split = urlsplit(uri)
        decoded = unquote(split.path)
        drive_form = len(decoded) > 2 and decoded[0] == "/" and decoded[2] == ":"
        if split.scheme == "file" and ((split.netloc and split.netloc.lower() != "localhost")
                                       or (decoded.replace("\\", "/").startswith("//") and not drive_form)):
            unc_inputs.append(uri)
            if path != "":
                _fail("uri_to_path", uri, "UNC/host-shaped URI was not refused: %r" % (path,))
    _check_timings("uri_to_path", timings)
    assert uri_to_path("http://x/a") == "" and uri_to_path("") == "" and uri_to_path(None) == ""   # type: ignore[arg-type]
    assert not unc_seen, "UNC-shaped path returned: %r" % (unc_seen[:3],)
    assert unc_inputs, "the generator never produced a UNC-shaped URI"


def test_clangd_uri_to_path_host_form_does_not_become_unc():
    """file://host/share/x names a remote host; the host must not be silently dropped into a local path."""
    for uri in ("file:////host/share/x", "file:///%5C%5Chost%5Cshare%5Cx", "file://host/share/x"):
        path = uri_to_path(uri).replace("\\", "/")
        assert not path.startswith("//") and not path.startswith("/\\\\".replace("\\", "/")), (uri, path)
