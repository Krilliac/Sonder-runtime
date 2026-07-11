"""Deterministic, stdlib-only validation recipes for generated artifacts."""

from __future__ import annotations

import csv
import hashlib
import json
import posixpath
import re
import struct
import wave
import zipfile
import zlib
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree


MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_BUNDLE_FILES = 500
MAX_BUNDLE_BYTES = 256 * 1024 * 1024
MAX_OOXML_ENTRIES = 1000
MAX_OOXML_BYTES = 128 * 1024 * 1024

OOXML_REQUIRED_PARTS = {
    "docx": {"[Content_Types].xml", "_rels/.rels", "word/document.xml"},
    "xlsx": {
        "[Content_Types].xml",
        "_rels/.rels",
        "xl/workbook.xml",
        "xl/worksheets/sheet1.xml",
    },
    "pptx": {
        "[Content_Types].xml",
        "_rels/.rels",
        "ppt/presentation.xml",
        "ppt/slideMasters/slideMaster1.xml",
        "ppt/slideLayouts/slideLayout1.xml",
        "ppt/theme/theme1.xml",
    },
}
OOXML_ACTIVE_SUFFIXES = {
    ".bat", ".cmd", ".com", ".dll", ".exe", ".js", ".msi", ".ps1",
    ".scr", ".vbe", ".vbs",
}

EXTENSION_RECIPES = {
    ".avi": "avi",
    ".csv": "csv",
    ".docx": "docx",
    ".edl": "edl",
    ".gif": "gif",
    ".htm": "html",
    ".html": "html",
    ".json": "json",
    ".md": "markdown",
    ".markdown": "markdown",
    ".mid": "midi",
    ".midi": "midi",
    ".obj": "obj",
    ".png": "png",
    ".ppm": "ppm",
    ".pptx": "pptx",
    ".svg": "svg",
    ".srt": "srt",
    ".txt": "text",
    ".wav": "wav",
    ".vtt": "vtt",
    ".xlsx": "xlsx",
}

RECIPE_ALIASES = {
    "audio": "wav",
    "animation": "gif",
    "captions": "auto",
    "data": "auto",
    "document": "auto",
    "editable": "ooxml",
    "image": "auto",
    "model": "obj",
    "office": "ooxml",
    "presentation": "auto",
    "spreadsheet": "auto",
    "subtitle": "auto",
    "timeline": "edl",
    "ui": "ui",
    "video": "avi",
    "web": "ui",
    "writing": "auto",
}

SUPPORTED_RECIPES = {
    "auto",
    "avi",
    "binary",
    "bundle",
    "csv",
    "docx",
    "edl",
    "gif",
    "html",
    "json",
    "markdown",
    "midi",
    "obj",
    "ooxml",
    "png",
    "ppm",
    "pptx",
    "svg",
    "srt",
    "text",
    "ui",
    "wav",
    "vtt",
    "xlsx",
    *RECIPE_ALIASES,
}


def parse_requirements(value) -> dict:
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("artifact requirements must be a JSON object")
    return dict(value)


def _check(checks: list, name: str, ok: bool, detail: str) -> bool:
    checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:1000]})
    return bool(ok)


def _bounded_int(requirements: dict, key: str, default: int, minimum=0, maximum=10**9):
    try:
        value = int(requirements.get(key, default))
    except (TypeError, ValueError) as exc:
        raise ValueError("%s must be an integer" % key) from exc
    if value < minimum or value > maximum:
        raise ValueError("%s must be between %s and %s" % (key, minimum, maximum))
    return value


def _string_list(requirements: dict, key: str) -> list[str]:
    value = requirements.get(key, [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("%s must be a JSON list with at most 100 items" % key)
    return [str(item) for item in value]


def _read_bytes(path: Path, maximum=MAX_FILE_BYTES) -> bytes:
    size = path.stat().st_size
    if size > maximum:
        raise ValueError("artifact exceeds %d-byte validation limit" % maximum)
    return path.read_bytes()


def _read_text(path: Path) -> str:
    data = _read_bytes(path, MAX_TEXT_BYTES)
    if b"\x00" in data:
        raise ValueError("text artifact contains NUL bytes")
    return data.decode("utf-8")


def _base_file_checks(path: Path, requirements: dict, checks: list) -> bool:
    size = path.stat().st_size
    minimum = _bounded_int(requirements, "min_bytes", 1, 0, MAX_FILE_BYTES)
    maximum = _bounded_int(
        requirements, "max_bytes", MAX_FILE_BYTES, minimum, MAX_FILE_BYTES
    )
    return _check(
        checks,
        "file-size",
        minimum <= size <= maximum,
        "%d bytes (required %d..%d)" % (size, minimum, maximum),
    )


def _validate_text(path: Path, requirements: dict, checks: list, markdown=False):
    try:
        text = _read_text(path)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        _check(checks, "utf8-text", False, str(exc))
        return
    _check(checks, "utf8-text", True, "%d characters" % len(text))
    minimum_chars = _bounded_int(requirements, "min_chars", 1, 0, MAX_TEXT_BYTES)
    _check(
        checks,
        "minimum-characters",
        len(text.strip()) >= minimum_chars,
        "%d non-edge characters (minimum %d)" % (len(text.strip()), minimum_chars),
    )
    minimum_words = _bounded_int(requirements, "min_words", 0, 0, 1_000_000)
    words = re.findall(r"\b[\w'-]+\b", text, re.UNICODE)
    _check(
        checks,
        "minimum-words",
        len(words) >= minimum_words,
        "%d words (minimum %d)" % (len(words), minimum_words),
    )
    for needle in _string_list(requirements, "required_text"):
        _check(checks, "required-text", needle in text, "contains %r" % needle)
    for needle in _string_list(requirements, "forbidden_text"):
        _check(checks, "forbidden-text", needle not in text, "excludes %r" % needle)
    if markdown:
        headings = re.findall(r"(?m)^#{1,6}\s+\S.*$", text)
        minimum_headings = _bounded_int(requirements, "min_headings", 1, 0, 10000)
        _check(
            checks,
            "markdown-headings",
            len(headings) >= minimum_headings,
            "%d headings (minimum %d)" % (len(headings), minimum_headings),
        )
        required_headings = [item.strip().lower() for item in _string_list(
            requirements, "required_headings"
        )]
        normalized = [re.sub(r"^#{1,6}\s+", "", item).strip().lower() for item in headings]
        for heading in required_headings:
            _check(
                checks,
                "required-heading",
                heading in normalized,
                "heading %r" % heading,
            )


def _json_path(value, path: str):
    current = value
    for part in [item for item in path.split(".") if item]:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise KeyError(path)
    return current


def _validate_json(path: Path, requirements: dict, checks: list):
    try:
        parsed = json.loads(_read_text(path))
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        _check(checks, "valid-json", False, str(exc))
        return
    _check(checks, "valid-json", True, "root type %s" % type(parsed).__name__)
    root_type = str(requirements.get("root_type", "")).strip().lower()
    types = {"object": dict, "array": list, "string": str, "number": (int, float)}
    if root_type:
        if root_type not in types:
            raise ValueError("root_type must be object, array, string, or number")
        _check(
            checks,
            "json-root-type",
            isinstance(parsed, types[root_type]),
            "expected %s" % root_type,
        )
    minimum_items = _bounded_int(requirements, "min_items", 0, 0, 1_000_000)
    if isinstance(parsed, (dict, list)):
        _check(
            checks,
            "json-minimum-items",
            len(parsed) >= minimum_items,
            "%d items (minimum %d)" % (len(parsed), minimum_items),
        )
    for field in _string_list(requirements, "required_fields"):
        try:
            _json_path(parsed, field)
            found = True
        except (KeyError, IndexError):
            found = False
        _check(checks, "json-required-field", found, "field %s" % field)


def _validate_csv(path: Path, requirements: dict, checks: list):
    try:
        text = _read_text(path)
        rows = list(csv.reader(text.splitlines()))
    except (OSError, UnicodeDecodeError, ValueError, csv.Error) as exc:
        _check(checks, "valid-csv", False, str(exc))
        return
    if not rows:
        _check(checks, "valid-csv", False, "CSV is empty")
        return
    header = rows[0]
    width = len(header)
    consistent = width > 0 and all(len(row) == width for row in rows)
    _check(checks, "valid-csv", consistent, "%d rows, %d columns" % (len(rows), width))
    unique_header = len(set(header)) == len(header) and all(item.strip() for item in header)
    _check(checks, "csv-header", unique_header, "non-empty unique columns")
    minimum_rows = _bounded_int(requirements, "min_rows", 1, 0, 1_000_000)
    data_rows = max(0, len(rows) - 1)
    _check(
        checks,
        "csv-minimum-rows",
        data_rows >= minimum_rows,
        "%d data rows (minimum %d)" % (data_rows, minimum_rows),
    )
    for column in _string_list(requirements, "required_columns"):
        _check(checks, "csv-required-column", column in header, "column %r" % column)


class _HTMLAudit(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.refs = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag.lower())
        attrs = dict(attrs)
        for key in ("href", "src"):
            if attrs.get(key):
                self.refs.append(str(attrs[key]))


def _is_external_ref(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered.startswith(("http://", "https://", "//"))


def _validate_html(path: Path, requirements: dict, checks: list):
    try:
        text = _read_text(path)
        parser = _HTMLAudit()
        parser.feed(text)
        parser.close()
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        _check(checks, "valid-html", False, str(exc))
        return
    _check(checks, "valid-html", bool(parser.tags), "%d start tags" % len(parser.tags))
    required = _string_list(requirements, "required_tags") or ["html", "body"]
    for tag in required:
        _check(checks, "html-required-tag", tag.lower() in parser.tags, "tag <%s>" % tag)
    if requirements.get("no_external_dependencies"):
        external = [ref for ref in parser.refs if _is_external_ref(ref)]
        _check(
            checks,
            "html-no-external-dependencies",
            not external,
            "external references: %s" % (", ".join(external[:10]) or "none"),
        )
    missing = []
    for ref in parser.refs:
        clean = ref.split("#", 1)[0].split("?", 1)[0]
        if not clean or clean.startswith(("#", "data:", "mailto:", "javascript:")):
            continue
        if _is_external_ref(clean):
            continue
        candidate = (path.parent / clean).resolve()
        if path.parent.resolve() not in (candidate, *candidate.parents) or not candidate.exists():
            missing.append(ref)
    _check(
        checks,
        "html-local-references",
        not missing,
        "missing local references: %s" % (", ".join(missing[:10]) or "none"),
    )


def _validate_svg(path: Path, requirements: dict, checks: list):
    try:
        root = ElementTree.fromstring(_read_text(path))
    except (OSError, UnicodeDecodeError, ValueError, ElementTree.ParseError) as exc:
        _check(checks, "valid-svg", False, str(exc))
        return
    root_name = root.tag.rsplit("}", 1)[-1].lower()
    _check(checks, "valid-svg", root_name == "svg", "root element <%s>" % root_name)
    graphics = {"circle", "ellipse", "image", "line", "path", "polygon", "polyline", "rect", "text"}
    count = sum(1 for element in root.iter() if element.tag.rsplit("}", 1)[-1] in graphics)
    _check(checks, "svg-graphics", count > 0, "%d graphical elements" % count)
    has_geometry = bool(root.get("viewBox") or (root.get("width") and root.get("height")))
    _check(checks, "svg-geometry", has_geometry, "viewBox or width/height present")
    if requirements.get("no_external_dependencies"):
        external = []
        for element in root.iter():
            for value in element.attrib.values():
                text = str(value).strip()
                if _is_external_ref(text) or re.search(
                    r"url\(\s*['\"]?(?:https?:)?//", text, re.IGNORECASE
                ):
                    external.append(text)
        _check(
            checks,
            "svg-no-external-dependencies",
            not external,
            "external references: %s" % (", ".join(external[:10]) or "none"),
        )


def _validate_png(path: Path, requirements: dict, checks: list):
    try:
        data = _read_bytes(path)
    except (OSError, ValueError) as exc:
        _check(checks, "valid-png", False, str(exc))
        return
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        _check(checks, "valid-png", False, "invalid PNG signature")
        return
    offset = 8
    width = height = 0
    chunk_count = 0
    crc_ok = True
    ended = False
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        if length > MAX_FILE_BYTES or offset + 12 + length > len(data):
            break
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        expected = struct.unpack(">I", data[offset + 8 + length : offset + 12 + length])[0]
        crc_ok = crc_ok and zlib.crc32(kind + payload) & 0xFFFFFFFF == expected
        chunk_count += 1
        if kind == b"IHDR" and len(payload) == 13:
            width, height = struct.unpack(">II", payload[:8])
        offset += 12 + length
        if kind == b"IEND":
            ended = True
            break
    _check(checks, "png-structure", bool(width and height and ended), "%dx%d, %d chunks" % (width, height, chunk_count))
    _check(checks, "png-crc", crc_ok, "all parsed chunk CRCs match")
    max_side = _bounded_int(requirements, "max_side", 32768, 1, 32768)
    min_side = _bounded_int(requirements, "min_side", 1, 1, max_side)
    _check(
        checks,
        "png-dimensions",
        min_side <= width <= max_side and min_side <= height <= max_side,
        "%dx%d (each side %d..%d)" % (width, height, min_side, max_side),
    )


def _ppm_tokens(data: bytes):
    tokens = []
    index = 0
    while index < len(data) and len(tokens) < 4:
        while index < len(data) and chr(data[index]).isspace():
            index += 1
        if index < len(data) and data[index : index + 1] == b"#":
            index = data.find(b"\n", index)
            if index < 0:
                break
            continue
        start = index
        while index < len(data) and not chr(data[index]).isspace():
            index += 1
        tokens.append(data[start:index])
    if data[index : index + 2] == b"\r\n":
        index += 2
    elif index < len(data) and chr(data[index]).isspace():
        index += 1
    return tokens, index


def _validate_ppm(path: Path, requirements: dict, checks: list):
    try:
        data = _read_bytes(path)
        tokens, payload_offset = _ppm_tokens(data)
        magic, width, height, maximum = tokens
        width, height, maximum = int(width), int(height), int(maximum)
    except (OSError, ValueError, TypeError) as exc:
        _check(checks, "valid-ppm", False, str(exc))
        return
    valid = magic in (b"P3", b"P6") and width > 0 and height > 0 and maximum > 0
    if magic == b"P6":
        valid = valid and len(data) - payload_offset >= width * height * 3
    elif magic == b"P3":
        try:
            samples = [int(value) for value in data[payload_offset:].split()]
            valid = (
                valid
                and len(samples) >= width * height * 3
                and all(0 <= value <= maximum for value in samples)
            )
        except ValueError:
            valid = False
    _check(checks, "valid-ppm", valid, "%s %dx%d max=%d" % (magic.decode("ascii", "replace"), width, height, maximum))


def _validate_wav(path: Path, requirements: dict, checks: list):
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            rate = handle.getframerate()
            frames = handle.getnframes()
            sample_width = handle.getsampwidth()
    except (OSError, EOFError, wave.Error) as exc:
        _check(checks, "valid-wav", False, str(exc))
        return
    duration = frames / rate if rate else 0.0
    valid = channels in (1, 2) and rate > 0 and frames > 0 and sample_width in (1, 2, 3, 4)
    _check(checks, "valid-wav", valid, "%dch %dHz %.3fs" % (channels, rate, duration))
    minimum_ms = _bounded_int(requirements, "min_duration_ms", 1, 0, 86_400_000)
    _check(
        checks,
        "wav-duration",
        duration * 1000 >= minimum_ms,
        "%.1f ms (minimum %d)" % (duration * 1000, minimum_ms),
    )


def _gif_subblocks(data: bytes, offset: int):
    payload = bytearray()
    while offset < len(data):
        size = data[offset]
        offset += 1
        if size == 0:
            return bytes(payload), offset
        if offset + size > len(data):
            raise ValueError("GIF sub-block exceeds file boundary")
        payload.extend(data[offset : offset + size])
        offset += size
    raise ValueError("GIF sub-block chain has no terminator")


def _gif_lzw_decode(payload: bytes, minimum_code_size: int, maximum_output: int):
    if not 2 <= minimum_code_size <= 8:
        raise ValueError("GIF LZW minimum code size must be 2..8")
    clear = 1 << minimum_code_size
    end = clear + 1
    code_size = minimum_code_size + 1
    dictionary = {index: bytes((index,)) for index in range(clear)}
    next_code = end + 1
    previous = None
    output = bytearray()
    bit_offset = 0
    ended = False

    def read_code(width):
        nonlocal bit_offset
        if bit_offset + width > len(payload) * 8:
            raise ValueError("GIF LZW stream ended inside a code")
        value = 0
        for bit in range(width):
            byte_index = (bit_offset + bit) // 8
            bit_index = (bit_offset + bit) % 8
            value |= ((payload[byte_index] >> bit_index) & 1) << bit
        bit_offset += width
        return value

    while bit_offset < len(payload) * 8:
        code = read_code(code_size)
        if code == clear:
            dictionary = {index: bytes((index,)) for index in range(clear)}
            next_code = end + 1
            code_size = minimum_code_size + 1
            previous = None
            continue
        if code == end:
            ended = True
            break
        if code in dictionary:
            entry = dictionary[code]
        elif code == next_code and previous is not None:
            entry = previous + previous[:1]
        else:
            raise ValueError("GIF LZW references an undefined code")
        output.extend(entry)
        if len(output) > maximum_output:
            raise ValueError("GIF LZW expands beyond the frame dimensions")
        if previous is not None and next_code < 4096:
            dictionary[next_code] = previous + entry[:1]
            next_code += 1
            if next_code == (1 << code_size) and code_size < 12:
                code_size += 1
        previous = entry
    if not ended:
        raise ValueError("GIF LZW stream has no end code")
    return bytes(output)


def _validate_gif(path: Path, requirements: dict, checks: list):
    try:
        data = _read_bytes(path)
        if len(data) < 14 or data[:6] not in {b"GIF87a", b"GIF89a"}:
            raise ValueError("invalid GIF signature or logical screen descriptor")
        width, height, packed, _background, _aspect = struct.unpack(
            "<HHBBB", data[6:13]
        )
        if not width or not height:
            raise ValueError("GIF dimensions must be positive")
        offset = 13
        global_colors = 0
        if packed & 0x80:
            global_colors = 1 << ((packed & 0x07) + 1)
            offset += global_colors * 3
        if offset > len(data):
            raise ValueError("GIF global color table is truncated")
        frames = 0
        total_delay_cs = 0
        pending_delay_cs = 0
        lzw_ok = True
        structure_ok = True
        trailer = False
        while offset < len(data):
            marker = data[offset]
            offset += 1
            if marker == 0x3B:
                trailer = True
                structure_ok = offset == len(data)
                break
            if marker == 0x21:
                if offset >= len(data):
                    raise ValueError("GIF extension label is missing")
                label = data[offset]
                offset += 1
                extension, offset = _gif_subblocks(data, offset)
                if label == 0xF9:
                    if len(extension) != 4:
                        raise ValueError("GIF graphic control extension must be 4 bytes")
                    pending_delay_cs = struct.unpack("<H", extension[1:3])[0]
                continue
            if marker != 0x2C:
                raise ValueError("unknown GIF block marker 0x%02x" % marker)
            if offset + 9 > len(data):
                raise ValueError("GIF image descriptor is truncated")
            left, top, frame_width, frame_height, image_packed = struct.unpack(
                "<HHHHB", data[offset : offset + 9]
            )
            offset += 9
            if not frame_width or not frame_height:
                raise ValueError("GIF frame dimensions must be positive")
            if left + frame_width > width or top + frame_height > height:
                raise ValueError("GIF frame exceeds the logical screen")
            color_count = global_colors
            if image_packed & 0x80:
                color_count = 1 << ((image_packed & 0x07) + 1)
                offset += color_count * 3
                if offset > len(data):
                    raise ValueError("GIF local color table is truncated")
            if color_count == 0:
                raise ValueError("GIF frame has no active color table")
            if offset >= len(data):
                raise ValueError("GIF image data is missing")
            minimum_code_size = data[offset]
            offset += 1
            compressed, offset = _gif_subblocks(data, offset)
            try:
                decoded = _gif_lzw_decode(
                    compressed, minimum_code_size, frame_width * frame_height
                )
                frame_valid = (
                    len(decoded) == frame_width * frame_height
                    and (not decoded or max(decoded) < color_count)
                )
            except ValueError:
                frame_valid = False
            lzw_ok = lzw_ok and frame_valid
            frames += 1
            total_delay_cs += pending_delay_cs
            pending_delay_cs = 0
    except (OSError, ValueError, struct.error) as exc:
        _check(checks, "valid-gif", False, str(exc))
        return
    _check(
        checks,
        "valid-gif",
        structure_ok and trailer,
        "%dx%d, %d frame(s)" % (width, height, frames),
    )
    _check(checks, "gif-lzw", lzw_ok, "all frame streams decode to their dimensions")
    minimum_frames = _bounded_int(requirements, "min_frames", 1, 1, 10_000)
    _check(
        checks,
        "gif-minimum-frames",
        frames >= minimum_frames,
        "%d frames (minimum %d)" % (frames, minimum_frames),
    )
    minimum_ms = _bounded_int(requirements, "min_duration_ms", 0, 0, 86_400_000)
    _check(
        checks,
        "gif-duration",
        total_delay_cs * 10 >= minimum_ms,
        "%d ms (minimum %d)" % (total_delay_cs * 10, minimum_ms),
    )
    max_side = _bounded_int(requirements, "max_side", 32768, 1, 32768)
    min_side = _bounded_int(requirements, "min_side", 1, 1, max_side)
    _check(
        checks,
        "gif-dimensions",
        min_side <= width <= max_side and min_side <= height <= max_side,
        "%dx%d (each side %d..%d)" % (width, height, min_side, max_side),
    )


def _riff_chunks(data: bytes, start: int, end: int):
    offset = start
    while offset < end:
        if offset + 8 > end:
            raise ValueError("RIFF chunk header is truncated")
        kind = data[offset : offset + 4]
        size = struct.unpack("<I", data[offset + 4 : offset + 8])[0]
        payload_offset = offset + 8
        payload_end = payload_offset + size
        if payload_end > end:
            raise ValueError("RIFF chunk %r exceeds its parent" % kind)
        yield kind, payload_offset, size, offset
        offset = payload_end + (size & 1)
        if offset > end:
            raise ValueError("RIFF chunk padding exceeds its parent")


def _avi_stream_header(data: bytes):
    if len(data) < 56:
        raise ValueError("AVI stream header is truncated")
    values = struct.unpack("<4s4sIHHIIIIIIIIhhhh", data[:56])
    return {
        "type": values[0],
        "handler": values[1],
        "scale": values[6],
        "rate": values[7],
        "length": values[9],
        "suggested_size": values[10],
        "sample_size": values[12],
        "rect": values[13:17],
    }


def _validate_avi(path: Path, requirements: dict, checks: list):
    try:
        data = _read_bytes(path)
        if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"AVI ":
            raise ValueError("AVI must be a RIFF AVI container")
        declared_size = struct.unpack("<I", data[4:8])[0]
        if declared_size + 8 != len(data):
            raise ValueError("AVI RIFF size does not match the file length")
        header_range = None
        movie_range = None
        movie_origin = None
        index_payload = None
        for kind, payload_offset, size, _chunk_offset in _riff_chunks(
            data, 12, len(data)
        ):
            if kind == b"LIST" and size >= 4:
                list_kind = data[payload_offset : payload_offset + 4]
                if list_kind == b"hdrl":
                    header_range = (payload_offset + 4, payload_offset + size)
                elif list_kind == b"movi":
                    movie_origin = payload_offset
                    movie_range = (payload_offset + 4, payload_offset + size)
            elif kind == b"idx1":
                index_payload = data[payload_offset : payload_offset + size]
        if header_range is None or movie_range is None:
            raise ValueError("AVI must contain hdrl and movi lists")

        main_header = None
        streams = []
        for kind, payload_offset, size, _chunk_offset in _riff_chunks(
            data, *header_range
        ):
            if kind == b"avih":
                if size < 56:
                    raise ValueError("AVI main header is truncated")
                main_header = struct.unpack(
                    "<14I", data[payload_offset : payload_offset + 56]
                )
            elif kind == b"LIST" and size >= 4:
                if data[payload_offset : payload_offset + 4] != b"strl":
                    continue
                stream_header = None
                stream_format = None
                for child_kind, child_offset, child_size, _child_chunk in _riff_chunks(
                    data, payload_offset + 4, payload_offset + size
                ):
                    if child_kind == b"strh":
                        stream_header = _avi_stream_header(
                            data[child_offset : child_offset + child_size]
                        )
                    elif child_kind == b"strf":
                        stream_format = data[child_offset : child_offset + child_size]
                if stream_header is None or stream_format is None:
                    raise ValueError("AVI stream list requires strh and strf chunks")
                streams.append((stream_header, stream_format))
        if main_header is None:
            raise ValueError("AVI main header is missing")

        video_streams = [stream for stream in streams if stream[0]["type"] == b"vids"]
        audio_streams = [stream for stream in streams if stream[0]["type"] == b"auds"]
        if len(video_streams) != 1:
            raise ValueError("AVI requires exactly one video stream")
        video_header, video_format_data = video_streams[0]
        if len(video_format_data) < 40:
            raise ValueError("AVI BITMAPINFOHEADER is truncated")
        bitmap = struct.unpack("<IiiHHIIiiII", video_format_data[:40])
        bitmap_size, width, signed_height, planes, bit_count, compression = bitmap[:6]
        image_size = bitmap[6]
        height = abs(signed_height)
        if (
            bitmap_size < 40
            or width <= 0
            or height <= 0
            or planes != 1
            or bit_count != 24
            or compression != 0
        ):
            raise ValueError("AVI video must be uncompressed 24-bit BI_RGB")
        stride = (width * 3 + 3) & ~3
        expected_frame_size = stride * height
        if image_size not in {0, expected_frame_size}:
            raise ValueError("AVI frame image size does not match its dimensions")
        if not video_header["scale"] or not video_header["rate"]:
            raise ValueError("AVI video stream rate/scale is invalid")
        fps = video_header["rate"] / float(video_header["scale"])

        audio_header = None
        audio_format = None
        if audio_streams:
            if len(audio_streams) != 1:
                raise ValueError("AVI supports at most one audio stream")
            audio_header, audio_format_data = audio_streams[0]
            if len(audio_format_data) < 16:
                raise ValueError("AVI PCM format is truncated")
            audio_format = struct.unpack("<HHIIHH", audio_format_data[:16])
            format_tag, channels, sample_rate, average_bytes, block_align, bits = (
                audio_format
            )
            if (
                format_tag != 1
                or channels not in {1, 2}
                or not sample_rate
                or not average_bytes
                or not block_align
                or bits not in {8, 16, 24, 32}
                or average_bytes != sample_rate * block_align
            ):
                raise ValueError("AVI audio stream must contain consistent PCM")

        movie_records = []
        video_chunks = []
        audio_chunks = []
        for kind, payload_offset, size, chunk_offset in _riff_chunks(
            data, *movie_range
        ):
            relative_offset = chunk_offset - movie_origin
            movie_records.append((kind, relative_offset, size))
            if re.fullmatch(rb"\d\ddb|\d\ddc", kind):
                video_chunks.append((payload_offset, size))
            elif re.fullmatch(rb"\d\dwb", kind):
                audio_chunks.append((payload_offset, size))
        if not video_chunks:
            raise ValueError("AVI movi list contains no video frames")
        if any(size != expected_frame_size for _offset, size in video_chunks):
            raise ValueError("AVI video chunk size does not match the frame format")
        total_audio_bytes = sum(size for _offset, size in audio_chunks)

        index_valid = index_payload is not None and len(index_payload) % 16 == 0
        index_records = []
        if index_valid:
            for offset in range(0, len(index_payload), 16):
                kind, _flags, chunk_offset, chunk_size = struct.unpack(
                    "<4sIII", index_payload[offset : offset + 16]
                )
                index_records.append((kind, chunk_offset, chunk_size))
            index_valid = index_records == movie_records
    except (OSError, ValueError, struct.error) as exc:
        _check(checks, "valid-avi", False, str(exc))
        return

    main_frames = main_header[4]
    main_streams = main_header[6]
    main_width = main_header[8]
    main_height = main_header[9]
    audio_consistent = True
    if audio_header is not None:
        audio_consistent = (
            bool(audio_header["scale"])
            and bool(audio_header["rate"])
            and audio_header["sample_size"] == audio_format[4]
            and audio_header["length"] * audio_header["sample_size"]
            == total_audio_bytes
        )
    stream_consistent = (
        main_streams == len(streams)
        and main_width == width
        and main_height == height
        and bool(main_header[3] & 0x10)
        and abs(main_header[0] - (1_000_000.0 / fps)) <= 1.0
        and video_header["length"] == len(video_chunks)
        and main_frames == len(video_chunks)
        and audio_consistent
    )
    _check(
        checks,
        "valid-avi",
        stream_consistent,
        "%dx%d, %.3f fps, %d frame(s), %d stream(s)"
        % (width, height, fps, len(video_chunks), len(streams)),
    )
    _check(
        checks,
        "avi-index",
        index_valid,
        "%d movi chunks, %d index entries"
        % (len(movie_records), len(index_records)),
    )
    minimum_frames = _bounded_int(requirements, "min_frames", 1, 1, 1_000_000)
    _check(
        checks,
        "avi-minimum-frames",
        len(video_chunks) >= minimum_frames,
        "%d frames (minimum %d)" % (len(video_chunks), minimum_frames),
    )
    duration_ms = len(video_chunks) * video_header["scale"] * 1000.0 / video_header["rate"]
    minimum_ms = _bounded_int(requirements, "min_duration_ms", 1, 0, 86_400_000)
    _check(
        checks,
        "avi-duration",
        duration_ms >= minimum_ms,
        "%.1f ms (minimum %d)" % (duration_ms, minimum_ms),
    )
    max_side = _bounded_int(requirements, "max_side", 32768, 1, 32768)
    min_side = _bounded_int(requirements, "min_side", 1, 1, max_side)
    _check(
        checks,
        "avi-dimensions",
        min_side <= width <= max_side and min_side <= height <= max_side,
        "%dx%d (each side %d..%d)" % (width, height, min_side, max_side),
    )
    if requirements.get("require_audio"):
        audio_duration_ms = (
            total_audio_bytes * 1000.0 / audio_format[3] if audio_format else 0.0
        )
        synchronized = (
            audio_header is not None
            and bool(audio_chunks)
            and abs(audio_duration_ms - duration_ms) <= max(2.0, 1000.0 / fps)
        )
        _check(
            checks,
            "avi-audio",
            synchronized,
            "%d PCM chunks, %.1f ms" % (len(audio_chunks), audio_duration_ms),
        )


def _midi_variable_length(data: bytes, offset: int):
    value = 0
    for _ in range(4):
        if offset >= len(data):
            raise ValueError("MIDI variable-length value is truncated")
        byte = data[offset]
        offset += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, offset
    raise ValueError("MIDI variable-length value exceeds four bytes")


def _parse_midi_track(data: bytes):
    offset = 0
    running_status = None
    ticks = 0
    note_count = 0
    tempo_count = 0
    ended = False
    while offset < len(data):
        delta, offset = _midi_variable_length(data, offset)
        ticks += delta
        if offset >= len(data):
            raise ValueError("MIDI event status is missing")
        lead = data[offset]
        if lead < 0x80:
            if running_status is None:
                raise ValueError("MIDI data byte has no running status")
            status = running_status
        else:
            status = lead
            offset += 1
            running_status = status if status < 0xF0 else None
        if status == 0xFF:
            if offset >= len(data):
                raise ValueError("MIDI meta event type is missing")
            kind = data[offset]
            offset += 1
            length, offset = _midi_variable_length(data, offset)
            if offset + length > len(data):
                raise ValueError("MIDI meta event is truncated")
            payload = data[offset : offset + length]
            offset += length
            if kind == 0x51 and len(payload) == 3:
                tempo_count += 1
            if kind == 0x2F:
                if length != 0:
                    raise ValueError("MIDI end-of-track event must be empty")
                ended = offset == len(data)
                break
            continue
        if status in {0xF0, 0xF7}:
            length, offset = _midi_variable_length(data, offset)
            if offset + length > len(data):
                raise ValueError("MIDI SysEx event is truncated")
            offset += length
            continue
        if status >= 0xF0:
            raise ValueError("unsupported MIDI system event 0x%02x" % status)
        event = status & 0xF0
        data_length = 1 if event in {0xC0, 0xD0} else 2
        if offset + data_length > len(data):
            raise ValueError("MIDI channel event is truncated")
        event_data = data[offset : offset + data_length]
        if any(value >= 0x80 for value in event_data):
            raise ValueError("MIDI channel data byte has its high bit set")
        offset += data_length
        if event == 0x90 and event_data[1] > 0:
            note_count += 1
    return {
        "ticks": ticks,
        "notes": note_count,
        "tempos": tempo_count,
        "ended": ended,
    }


def _validate_midi(path: Path, requirements: dict, checks: list):
    try:
        data = _read_bytes(path)
        if len(data) < 14 or data[:4] != b"MThd":
            raise ValueError("MIDI header chunk is missing")
        header_length = struct.unpack(">I", data[4:8])[0]
        if header_length < 6 or 8 + header_length > len(data):
            raise ValueError("MIDI header chunk has an invalid length")
        midi_format, declared_tracks, division = struct.unpack(">HHH", data[8:14])
        if midi_format not in {0, 1}:
            raise ValueError("only MIDI format 0 or 1 is supported")
        if midi_format == 0 and declared_tracks != 1:
            raise ValueError("MIDI format 0 must declare exactly one track")
        if not declared_tracks or declared_tracks > 64:
            raise ValueError("MIDI track count must be 1..64")
        if division & 0x8000 or division == 0:
            raise ValueError("MIDI must use positive ticks-per-quarter timing")
        offset = 8 + header_length
        tracks = []
        for _ in range(declared_tracks):
            if offset + 8 > len(data) or data[offset : offset + 4] != b"MTrk":
                raise ValueError("MIDI track chunk is missing")
            track_length = struct.unpack(">I", data[offset + 4 : offset + 8])[0]
            offset += 8
            if offset + track_length > len(data):
                raise ValueError("MIDI track chunk is truncated")
            tracks.append(_parse_midi_track(data[offset : offset + track_length]))
            offset += track_length
        if offset != len(data):
            raise ValueError("MIDI contains trailing bytes after declared tracks")
    except (OSError, ValueError, struct.error) as exc:
        _check(checks, "valid-midi", False, str(exc))
        return
    total_notes = sum(track["notes"] for track in tracks)
    total_tempos = sum(track["tempos"] for track in tracks)
    duration_ticks = max((track["ticks"] for track in tracks), default=0)
    _check(
        checks,
        "valid-midi",
        all(track["ended"] for track in tracks),
        "format %d, %d track(s), PPQ %d" % (midi_format, len(tracks), division),
    )
    minimum_tracks = _bounded_int(requirements, "min_tracks", 1, 1, 64)
    _check(
        checks,
        "midi-minimum-tracks",
        len(tracks) >= minimum_tracks,
        "%d tracks (minimum %d)" % (len(tracks), minimum_tracks),
    )
    minimum_notes = _bounded_int(requirements, "min_notes", 1, 0, 10_000_000)
    _check(
        checks,
        "midi-minimum-notes",
        total_notes >= minimum_notes,
        "%d note-on events (minimum %d)" % (total_notes, minimum_notes),
    )
    minimum_ticks = _bounded_int(
        requirements, "min_duration_ticks", 1, 0, 0x0FFFFFFF
    )
    _check(
        checks,
        "midi-duration",
        duration_ticks >= minimum_ticks,
        "%d ticks (minimum %d)" % (duration_ticks, minimum_ticks),
    )
    if requirements.get("require_tempo"):
        _check(checks, "midi-tempo", total_tempos > 0, "%d tempo events" % total_tempos)


def _caption_timestamp(value: str, separator: str):
    pattern = r"^(\d{2}):(\d{2}):(\d{2})%s(\d{3})$" % re.escape(separator)
    match = re.fullmatch(pattern, value.strip())
    if not match:
        raise ValueError("invalid caption timestamp %r" % value)
    hours, minutes, seconds, milliseconds = map(int, match.groups())
    if minutes >= 60 or seconds >= 60:
        raise ValueError("caption timestamp component is out of range")
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + milliseconds


def _caption_checks(cues, text, recipe: str, requirements: dict, checks: list):
    ordered = all(
        start < end and (index == 0 or start >= cues[index - 1][0])
        for index, (start, end, _caption) in enumerate(cues)
    )
    _check(checks, "%s-timing" % recipe, ordered, "%d ordered cue ranges" % len(cues))
    minimum = _bounded_int(requirements, "min_cues", 1, 0, 1_000_000)
    _check(
        checks,
        "%s-minimum-cues" % recipe,
        len(cues) >= minimum,
        "%d cues (minimum %d)" % (len(cues), minimum),
    )
    for needle in _string_list(requirements, "required_text"):
        _check(
            checks,
            "%s-required-text" % recipe,
            needle.casefold() in text.casefold(),
            "contains %r" % needle,
        )


def _validate_srt(path: Path, requirements: dict, checks: list):
    try:
        text = _read_text(path).lstrip("\ufeff")
        blocks = [block for block in re.split(r"\r?\n\s*\r?\n", text.strip()) if block]
        cues = []
        expected_index = 1
        for block in blocks:
            lines = block.splitlines()
            if len(lines) < 3 or int(lines[0].strip()) != expected_index:
                raise ValueError("SRT cue numbering must be contiguous from 1")
            timing = lines[1].split(" --> ")
            if len(timing) != 2:
                raise ValueError("SRT cue timing arrow is malformed")
            start = _caption_timestamp(timing[0], ",")
            end = _caption_timestamp(timing[1].split()[0], ",")
            caption = "\n".join(lines[2:]).strip()
            if not caption:
                raise ValueError("SRT cue text is empty")
            cues.append((start, end, caption))
            expected_index += 1
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        _check(checks, "valid-srt", False, str(exc))
        return
    _check(checks, "valid-srt", bool(cues), "%d parsed cues" % len(cues))
    _caption_checks(cues, text, "srt", requirements, checks)


def _validate_vtt(path: Path, requirements: dict, checks: list):
    try:
        text = _read_text(path).lstrip("\ufeff")
        lines = text.splitlines()
        if not lines or not lines[0].startswith("WEBVTT"):
            raise ValueError("WebVTT file must begin with WEBVTT")
        cues = []
        index = 1
        while index < len(lines):
            line = lines[index].strip()
            if not line:
                index += 1
                continue
            if line.startswith(("NOTE", "STYLE", "REGION")):
                index += 1
                while index < len(lines) and lines[index].strip():
                    index += 1
                continue
            if " --> " not in line:
                index += 1
                if index >= len(lines):
                    raise ValueError("WebVTT cue identifier has no timing line")
                line = lines[index].strip()
            timing = line.split(" --> ")
            if len(timing) != 2:
                raise ValueError("WebVTT cue timing arrow is malformed")
            start = _caption_timestamp(timing[0], ".")
            end = _caption_timestamp(timing[1].split()[0], ".")
            index += 1
            caption_lines = []
            while index < len(lines) and lines[index].strip():
                caption_lines.append(lines[index].strip())
                index += 1
            if not caption_lines:
                raise ValueError("WebVTT cue text is empty")
            cues.append((start, end, "\n".join(caption_lines)))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        _check(checks, "valid-vtt", False, str(exc))
        return
    _check(checks, "valid-vtt", bool(cues), "%d parsed cues" % len(cues))
    _caption_checks(cues, text, "vtt", requirements, checks)


def _edl_timecode(value: str, fps: int):
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2}):(\d{2})", value)
    if not match:
        raise ValueError("invalid EDL timecode %r" % value)
    hours, minutes, seconds, frames = map(int, match.groups())
    if minutes >= 60 or seconds >= 60 or frames >= fps:
        raise ValueError("EDL timecode component is out of range")
    return ((hours * 60 + minutes) * 60 + seconds) * fps + frames


def _validate_edl(path: Path, requirements: dict, checks: list):
    try:
        text = _read_text(path)
        lines = text.splitlines()
        if not any(line.startswith("TITLE:") for line in lines[:3]):
            raise ValueError("EDL title header is missing")
        if not any(line.strip() == "FCM: NON-DROP FRAME" for line in lines[:4]):
            raise ValueError("EDL must declare non-drop-frame timing")
        fps = _bounded_int(requirements, "frame_rate", 30, 1, 120)
        events = []
        for line in lines:
            parts = line.split()
            if not parts or not re.fullmatch(r"\d{3}", parts[0]):
                continue
            if len(parts) != 8:
                raise ValueError("EDL event row must contain eight fields")
            event_number = int(parts[0])
            source_in, source_out, record_in, record_out = (
                _edl_timecode(value, fps) for value in parts[4:8]
            )
            if source_out <= source_in or record_out <= record_in:
                raise ValueError("EDL event ranges must have positive duration")
            if source_out - source_in != record_out - record_in:
                raise ValueError("EDL source and record durations must match")
            events.append((event_number, record_in, record_out))
        if not events:
            raise ValueError("EDL contains no event rows")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        _check(checks, "valid-edl", False, str(exc))
        return
    numbered = [event[0] for event in events] == list(range(1, len(events) + 1))
    ordered = all(
        index == 0 or event[1] >= events[index - 1][2]
        for index, event in enumerate(events)
    )
    _check(checks, "valid-edl", numbered, "%d contiguous numbered events" % len(events))
    _check(checks, "edl-record-order", ordered, "record ranges are non-overlapping")
    minimum = _bounded_int(requirements, "min_events", 1, 1, 1_000_000)
    _check(
        checks,
        "edl-minimum-events",
        len(events) >= minimum,
        "%d events (minimum %d)" % (len(events), minimum),
    )
    for needle in _string_list(requirements, "required_text"):
        _check(
            checks,
            "edl-required-text",
            needle.casefold() in text.casefold(),
            "contains %r" % needle,
        )
    if requirements.get("no_external_dependencies"):
        clip_names = sorted(set(re.findall(
            r"(?m)^\*\s+FROM CLIP NAME:\s*(.+?)\s*$", text
        )))
        unsafe = []
        missing = []
        root = path.parent.resolve()
        for clip_name in clip_names:
            pure = PurePosixPath(clip_name.replace("\\", "/"))
            if (
                not pure.parts
                or pure.is_absolute()
                or ".." in pure.parts
                or _is_external_ref(clip_name)
                or ":" in pure.parts[0]
            ):
                unsafe.append(clip_name)
                continue
            candidate = (root / Path(*pure.parts)).resolve()
            if root not in (candidate, *candidate.parents):
                unsafe.append(clip_name)
            elif not candidate.is_file() or candidate.is_symlink():
                missing.append(clip_name)
        _check(
            checks,
            "edl-safe-media-references",
            not unsafe,
            "unsafe media: %s" % (", ".join(unsafe[:10]) or "none"),
        )
        _check(
            checks,
            "edl-local-media",
            not missing,
            "missing media: %s" % (", ".join(missing[:10]) or "none"),
        )


def _validate_obj(path: Path, requirements: dict, checks: list):
    try:
        lines = _read_text(path).splitlines()
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        _check(checks, "valid-obj", False, str(exc))
        return
    vertices = 0
    faces = 0
    bad_numbers = 0
    bad_indices = 0
    face_indices = []
    for line in lines:
        parts = line.strip().split()
        if not parts or parts[0].startswith("#"):
            continue
        if parts[0] == "v":
            try:
                if len(parts) < 4:
                    raise ValueError
                tuple(float(value) for value in parts[1:4])
                vertices += 1
            except ValueError:
                bad_numbers += 1
        elif parts[0] == "f":
            if len(parts) < 4:
                bad_indices += 1
                continue
            faces += 1
            for token in parts[1:]:
                try:
                    face_indices.append(int(token.split("/", 1)[0]))
                except ValueError:
                    bad_indices += 1
    for index in face_indices:
        resolved = index if index > 0 else vertices + index + 1
        if resolved < 1 or resolved > vertices:
            bad_indices += 1
    minimum_vertices = _bounded_int(requirements, "min_vertices", 3, 0, 10_000_000)
    minimum_faces = _bounded_int(requirements, "min_faces", 1, 0, 10_000_000)
    _check(
        checks,
        "obj-geometry",
        vertices >= minimum_vertices and faces >= minimum_faces,
        "%d vertices, %d faces" % (vertices, faces),
    )
    _check(checks, "obj-values", bad_numbers == 0, "%d malformed vertex rows" % bad_numbers)
    _check(checks, "obj-indices", bad_indices == 0, "%d malformed/out-of-range indices" % bad_indices)


def _safe_ooxml_part_name(name: str) -> str | None:
    """Return a canonical package part name, or None for an unsafe ZIP path."""
    if not name or "\\" in name or name.startswith("/") or "//" in name:
        return None
    pure = PurePosixPath(name)
    if any(part in {"", ".", ".."} for part in pure.parts):
        return None
    normalized = pure.as_posix()
    if normalized.startswith("../") or normalized == ".." or ":" in pure.parts[0]:
        return None
    return normalized


def _ooxml_relationship_source(relationship_part: str) -> str | None:
    if relationship_part == "_rels/.rels":
        return ""
    pure = PurePosixPath(relationship_part)
    if (
        len(pure.parts) < 3
        or pure.parts[-2] != "_rels"
        or not pure.name.endswith(".rels")
    ):
        return None
    source_name = pure.name[:-5]
    return PurePosixPath(*pure.parts[:-2], source_name).as_posix()


def _ooxml_relationship_target(relationship_part: str, target: str) -> str | None:
    """Resolve an internal OPC relationship without allowing package-root escape."""
    target = str(target or "").replace("\\", "/")
    target = target.split("#", 1)[0].split("?", 1)[0]
    if not target:
        return None
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith("//"):
        return None
    source = _ooxml_relationship_source(relationship_part)
    if source is None:
        return None
    if target.startswith("/"):
        joined = target.lstrip("/")
    else:
        joined = posixpath.join(posixpath.dirname(source), target)
    normalized = posixpath.normpath(joined)
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        return None
    return _safe_ooxml_part_name(normalized)


def _ooxml_kind(path: Path, recipe: str, names: set[str]) -> str:
    if recipe in OOXML_REQUIRED_PARTS:
        return recipe
    suffix = path.suffix.lower().lstrip(".")
    if suffix in OOXML_REQUIRED_PARTS:
        return suffix
    for kind, marker in (
        ("docx", "word/document.xml"),
        ("xlsx", "xl/workbook.xml"),
        ("pptx", "ppt/presentation.xml"),
    ):
        if marker in names:
            return kind
    return "unknown"


def _validate_ooxml(path: Path, recipe: str, requirements: dict, checks: list):
    """Validate an editable Office Open XML ZIP package without third parties."""
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        _check(checks, "valid-ooxml-zip", False, str(exc))
        return

    with archive:
        infos = archive.infolist()
        _check(checks, "valid-ooxml-zip", True, "%d package entries" % len(infos))
        within_entry_limit = _check(
            checks,
            "ooxml-entry-limit",
            len(infos) <= MAX_OOXML_ENTRIES,
            "%d entries (maximum %d)" % (len(infos), MAX_OOXML_ENTRIES),
        )
        total_bytes = sum(max(0, item.file_size) for item in infos)
        within_size_limit = _check(
            checks,
            "ooxml-uncompressed-limit",
            total_bytes <= MAX_OOXML_BYTES,
            "%d uncompressed bytes (maximum %d)" % (total_bytes, MAX_OOXML_BYTES),
        )
        canonical = [_safe_ooxml_part_name(item.filename.rstrip("/")) for item in infos]
        unsafe = [item.filename for item, safe in zip(infos, canonical) if safe is None]
        _check(
            checks,
            "ooxml-safe-paths",
            not unsafe,
            "unsafe entries: %s" % (", ".join(unsafe[:10]) or "none"),
        )
        safe_names = [name for name in canonical if name is not None]
        folded = [name.casefold() for name in safe_names]
        _check(
            checks,
            "ooxml-unique-paths",
            len(folded) == len(set(folded)),
            "%d unique entries" % len(set(folded)),
        )
        encrypted = [item.filename for item in infos if item.flag_bits & 0x1]
        _check(
            checks,
            "ooxml-not-encrypted",
            not encrypted,
            "encrypted entries: %s" % (", ".join(encrypted[:10]) or "none"),
        )
        linked = [
            item.filename
            for item in infos
            if ((item.external_attr >> 16) & 0o170000) == 0o120000
        ]
        _check(
            checks,
            "ooxml-no-symlinks",
            not linked,
            "symlink entries: %s" % (", ".join(linked[:10]) or "none"),
        )
        active = [
            name
            for name in safe_names
            if PurePosixPath(name).suffix.lower() in OOXML_ACTIVE_SUFFIXES
            or "vbaproject" in name.casefold()
            or "/activex/" in ("/" + name.casefold())
            or "/embeddings/" in ("/" + name.casefold())
        ]
        _check(
            checks,
            "ooxml-no-active-content",
            not active,
            "active/embedded entries: %s" % (", ".join(active[:10]) or "none"),
        )
        if (
            not within_entry_limit
            or not within_size_limit
            or unsafe
            or encrypted
            or linked
        ):
            return
        try:
            corrupt = archive.testzip()
        except (OSError, RuntimeError, zipfile.BadZipFile, zlib.error) as exc:
            _check(checks, "ooxml-zip-integrity", False, str(exc))
            return
        _check(
            checks,
            "ooxml-zip-integrity",
            corrupt is None,
            "first corrupt entry: %s" % (corrupt or "none"),
        )
        if corrupt is not None:
            return

        names = set(safe_names)
        kind = _ooxml_kind(path, recipe, names)
        _check(
            checks,
            "ooxml-package-kind",
            kind in OOXML_REQUIRED_PARTS,
            "detected %s" % kind,
        )
        if kind not in OOXML_REQUIRED_PARTS:
            return
        if recipe in OOXML_REQUIRED_PARTS:
            _check(
                checks,
                "ooxml-matching-extension",
                path.suffix.lower() == "." + recipe,
                "expected .%s" % recipe,
            )
        for required in sorted(OOXML_REQUIRED_PARTS[kind]):
            _check(
                checks,
                "ooxml-required-part",
                required in names,
                required,
            )

        roots = {}
        malformed = []
        unsafe_markup = []
        xml_names = [
            item.filename
            for item in infos
            if item.filename.lower().endswith((".xml", ".rels"))
        ]
        for name in xml_names:
            info = archive.getinfo(name)
            if info.file_size > MAX_TEXT_BYTES:
                malformed.append("%s exceeds XML size limit" % name)
                continue
            try:
                data = archive.read(name)
                lowered = data.lower()
                if b"<!doctype" in lowered or b"<!entity" in lowered:
                    unsafe_markup.append(name)
                    continue
                roots[name] = ElementTree.fromstring(data)
            except (ElementTree.ParseError, OSError, RuntimeError, ValueError) as exc:
                malformed.append("%s: %s" % (name, exc))
        _check(
            checks,
            "ooxml-valid-xml",
            not malformed and len(roots) == len(xml_names),
            "%d XML parts; malformed: %s"
            % (len(roots), "; ".join(malformed[:5]) or "none"),
        )
        _check(
            checks,
            "ooxml-safe-xml",
            not unsafe_markup,
            "DTD/entity parts: %s" % (", ".join(unsafe_markup[:10]) or "none"),
        )

        relationship_ns = "{http://schemas.openxmlformats.org/package/2006/relationships}"
        missing_targets = []
        invalid_targets = []
        external_targets = []
        for relationship_name, root in roots.items():
            if not relationship_name.endswith(".rels"):
                continue
            for relationship in root.findall("%sRelationship" % relationship_ns):
                target = relationship.attrib.get("Target", "")
                mode = relationship.attrib.get("TargetMode", "").casefold()
                external = mode == "external" or bool(
                    re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target)
                    or target.startswith("//")
                )
                if external:
                    external_targets.append(target)
                    continue
                resolved = _ooxml_relationship_target(relationship_name, target)
                if resolved is None:
                    invalid_targets.append("%s -> %s" % (relationship_name, target))
                elif resolved not in names:
                    missing_targets.append("%s -> %s" % (relationship_name, resolved))
        _check(
            checks,
            "ooxml-valid-relationship-targets",
            not invalid_targets,
            "invalid targets: %s" % ("; ".join(invalid_targets[:10]) or "none"),
        )
        _check(
            checks,
            "ooxml-complete-relationships",
            not missing_targets,
            "missing targets: %s" % ("; ".join(missing_targets[:10]) or "none"),
        )
        if requirements.get("no_external_dependencies"):
            _check(
                checks,
                "ooxml-no-external-dependencies",
                not external_targets,
                "external targets: %s"
                % (", ".join(external_targets[:10]) or "none"),
            )

        text_fragments = []
        if kind == "docx" and "word/document.xml" in roots:
            root = roots["word/document.xml"]
            namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            paragraphs = root.findall(".//%sp" % namespace)
            text_fragments = [node.text or "" for node in root.findall(".//%st" % namespace)]
            minimum = _bounded_int(requirements, "min_paragraphs", 1, 0, 1_000_000)
            _check(
                checks,
                "docx-minimum-paragraphs",
                len(paragraphs) >= minimum,
                "%d paragraphs (minimum %d)" % (len(paragraphs), minimum),
            )
        elif kind == "xlsx":
            sheet_names = sorted(
                name
                for name in roots
                if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)
            )
            sheet_ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
            rows = []
            for name in sheet_names:
                rows.extend(roots[name].findall(".//%srow" % sheet_ns))
                text_fragments.extend(
                    node.text or "" for node in roots[name].findall(".//%st" % sheet_ns)
                )
                text_fragments.extend(
                    node.text or "" for node in roots[name].findall(".//%sv" % sheet_ns)
                )
            minimum = _bounded_int(requirements, "min_rows", 1, 0, 1_000_000)
            _check(
                checks,
                "xlsx-minimum-rows",
                len(rows) >= minimum,
                "%d rows (minimum %d)" % (len(rows), minimum),
            )
            workbook = roots.get("xl/workbook.xml")
            actual_sheets = []
            if workbook is not None:
                actual_sheets = [
                    node.attrib.get("name", "")
                    for node in workbook.findall(".//%ssheet" % sheet_ns)
                ]
            for sheet in _string_list(requirements, "required_sheet_names"):
                _check(
                    checks,
                    "xlsx-required-sheet",
                    sheet in actual_sheets,
                    "sheet %r" % sheet,
                )
        elif kind == "pptx":
            slide_names = sorted(
                name for name in roots if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
            )
            drawing_ns = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
            for name in slide_names:
                text_fragments.extend(
                    node.text or "" for node in roots[name].findall(".//%st" % drawing_ns)
                )
            minimum = _bounded_int(requirements, "min_slides", 1, 0, 10_000)
            _check(
                checks,
                "pptx-minimum-slides",
                len(slide_names) >= minimum,
                "%d slides (minimum %d)" % (len(slide_names), minimum),
            )
        rendered_text = " ".join(text_fragments)
        for needle in _string_list(requirements, "required_text"):
            _check(
                checks,
                "ooxml-required-text",
                needle.casefold() in rendered_text.casefold(),
                "contains %r" % needle,
            )


def _resolve_recipe(path: Path, recipe: str) -> str:
    recipe = str(recipe or "auto").strip().lower().replace("-", "_")
    if recipe not in SUPPORTED_RECIPES:
        raise ValueError(
            "unknown artifact recipe %r; choose: %s"
            % (recipe, ", ".join(sorted(SUPPORTED_RECIPES)))
        )
    alias = RECIPE_ALIASES.get(recipe)
    if alias and alias != "auto":
        recipe = alias
    if recipe == "auto" or alias == "auto":
        if path.is_dir():
            return "bundle"
        return EXTENSION_RECIPES.get(path.suffix.lower(), "text")
    return recipe


def _child_requirements(requirements: dict, recipe: str) -> dict:
    common = requirements.get("file_requirements", {})
    if not isinstance(common, dict):
        raise ValueError("file_requirements must be an object")
    recipes = requirements.get("recipes", {})
    if not isinstance(recipes, dict):
        raise ValueError("recipes must be an object")
    specific = recipes.get(recipe, {})
    if not isinstance(specific, dict):
        raise ValueError("recipes.%s must be an object" % recipe)
    return {**common, **specific}


def _validate_file(path: Path, recipe: str, requirements: dict) -> dict:
    checks = []
    _base_file_checks(path, requirements, checks)
    if recipe == "binary":
        pass
    elif recipe == "text":
        _validate_text(path, requirements, checks)
    elif recipe == "markdown":
        _validate_text(path, requirements, checks, markdown=True)
    elif recipe == "json":
        _validate_json(path, requirements, checks)
    elif recipe == "csv":
        _validate_csv(path, requirements, checks)
    elif recipe == "html":
        _validate_html(path, requirements, checks)
    elif recipe == "svg":
        _validate_svg(path, requirements, checks)
    elif recipe == "png":
        _validate_png(path, requirements, checks)
    elif recipe == "ppm":
        _validate_ppm(path, requirements, checks)
    elif recipe == "wav":
        _validate_wav(path, requirements, checks)
    elif recipe == "avi":
        _validate_avi(path, requirements, checks)
    elif recipe == "gif":
        _validate_gif(path, requirements, checks)
    elif recipe == "midi":
        _validate_midi(path, requirements, checks)
    elif recipe == "srt":
        _validate_srt(path, requirements, checks)
    elif recipe == "vtt":
        _validate_vtt(path, requirements, checks)
    elif recipe == "edl":
        _validate_edl(path, requirements, checks)
    elif recipe == "obj":
        _validate_obj(path, requirements, checks)
    elif recipe in {"docx", "xlsx", "pptx", "ooxml"}:
        _validate_ooxml(path, recipe, requirements, checks)
    else:
        raise ValueError("recipe %s requires a directory" % recipe)
    return {
        "ok": all(item["ok"] for item in checks),
        "path": str(path),
        "recipe": recipe,
        "checks": checks,
        "children": [],
        "checked_files": 1,
    }


def _safe_manifest_path(root: Path, value: str) -> Path | None:
    pure = PurePosixPath(str(value or "").replace("\\", "/"))
    if not pure.parts or pure.is_absolute() or ".." in pure.parts:
        return None
    lexical = root / Path(*pure.parts)
    candidate = lexical.resolve()
    if root not in candidate.parents:
        return None
    current = root
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            return None
    return candidate


def _validate_directory(path: Path, recipe: str, requirements: dict) -> dict:
    checks = []
    children = []
    if recipe not in {"bundle", "ui"}:
        raise ValueError("recipe %s requires a file" % recipe)
    manifest_path = path / "manifest.json"
    require_manifest = bool(requirements.get("require_manifest", False))
    manifest = None
    if manifest_path.is_file():
        try:
            manifest = json.loads(_read_text(manifest_path))
            _check(checks, "bundle-manifest-json", isinstance(manifest, dict), "manifest.json is an object")
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            _check(checks, "bundle-manifest-json", False, str(exc))
    else:
        _check(checks, "bundle-manifest", not require_manifest, "manifest.json %s" % ("required" if require_manifest else "optional"))

    declared = []
    if isinstance(manifest, dict):
        files = manifest.get("files", [])
        valid_list = isinstance(files, list) and bool(files)
        _check(checks, "bundle-manifest-files", valid_list, "%d declared files" % (len(files) if isinstance(files, list) else 0))
        seen = set()
        if isinstance(files, list):
            for row in files[: MAX_BUNDLE_FILES + 1]:
                if not isinstance(row, dict):
                    _check(checks, "bundle-manifest-row", False, "file row must be an object")
                    continue
                relative = str(row.get("path", ""))
                candidate = _safe_manifest_path(path, relative)
                unique = relative not in seen
                _check(checks, "bundle-unique-path", unique, relative or "(empty)")
                seen.add(relative)
                safe = candidate is not None
                _check(checks, "bundle-safe-path", safe, relative or "(empty)")
                if not safe:
                    continue
                exists = candidate.is_file() and not candidate.is_symlink()
                _check(checks, "bundle-file-exists", exists, relative)
                if not exists:
                    continue
                declared.append((relative, candidate))
                size = candidate.stat().st_size
                _check(checks, "bundle-size", size == row.get("bytes"), "%s: %d bytes" % (relative, size))
                digest = hashlib.sha256(_read_bytes(candidate)).hexdigest()
                _check(checks, "bundle-sha256", digest == row.get("sha256"), relative)
        _check(checks, "bundle-file-limit", len(files) <= MAX_BUNDLE_FILES, "at most %d files" % MAX_BUNDLE_FILES)
        kinds = set(str(item) for item in manifest.get("kinds", []) if item)
        for kind in _string_list(requirements, "required_kinds"):
            _check(checks, "bundle-required-kind", kind in kinds, "kind %r" % kind)
    else:
        generic = []
        for item in path.rglob("*"):
            if item.is_file() and not item.is_symlink():
                generic.append(item)
                if len(generic) > MAX_BUNDLE_FILES:
                    break
        generic.sort()
        _check(checks, "bundle-file-limit", len(generic) <= MAX_BUNDLE_FILES, "%d files" % len(generic))
        declared = [(item.relative_to(path).as_posix(), item) for item in generic[:MAX_BUNDLE_FILES]]

    required_files = _string_list(requirements, "required_files")
    declared_names = {name for name, _candidate in declared}
    for filename in required_files:
        _check(checks, "bundle-required-file", filename in declared_names, "file %r" % filename)
    minimum_files = _bounded_int(requirements, "min_files", 1, 0, MAX_BUNDLE_FILES)
    _check(checks, "bundle-minimum-files", len(declared) >= minimum_files, "%d files (minimum %d)" % (len(declared), minimum_files))
    total_bytes = sum(candidate.stat().st_size for _name, candidate in declared)
    _check(checks, "bundle-total-size", total_bytes <= MAX_BUNDLE_BYTES, "%d bytes" % total_bytes)

    if recipe == "ui":
        entry_names = ["index.html", "preview.html"]
        entry = next((candidate for name, candidate in declared if name in entry_names), None)
        _check(checks, "ui-entrypoint", entry is not None, "index.html or preview.html")
        if entry is not None:
            child_requirements = _child_requirements(requirements, "html")
            child_requirements.setdefault("no_external_dependencies", bool(requirements.get("no_external_dependencies", False)))
            children.append(_validate_file(entry, "html", child_requirements))

    for relative, candidate in declared:
        if recipe == "ui" and entry is not None and candidate == entry:
            continue
        if recipe == "ui" and candidate.suffix.lower() not in {".html", ".htm", ".svg", ".json"}:
            continue
        child_recipe = _resolve_recipe(candidate, "auto")
        child_requirements = _child_requirements(requirements, child_recipe)
        if child_recipe in {
            "html", "svg", "docx", "xlsx", "pptx", "ooxml", "edl",
        } and "no_external_dependencies" in requirements:
            child_requirements.setdefault(
                "no_external_dependencies",
                bool(requirements.get("no_external_dependencies")),
            )
        children.append(_validate_file(candidate, child_recipe, child_requirements))
    return {
        "ok": all(item["ok"] for item in checks) and all(child["ok"] for child in children),
        "path": str(path),
        "recipe": recipe,
        "checks": checks,
        "children": children,
        "checked_files": len(declared),
    }


def validate(path, recipe="auto", requirements=None) -> dict:
    """Validate one artifact path with an inferred or explicit recipe."""
    requirements = parse_requirements(requirements)
    requested = Path(path).expanduser()
    if not requested.exists():
        return {
            "ok": False,
            "path": str(requested.absolute()),
            "recipe": str(recipe or "auto"),
            "checks": [{"name": "exists", "ok": False, "detail": "artifact path does not exist"}],
            "children": [],
            "checked_files": 0,
            "passed_checks": 0,
            "failed_checks": 1,
        }
    if requested.is_symlink():
        return {
            "ok": False,
            "path": str(requested.absolute()),
            "recipe": str(recipe or "auto"),
            "checks": [{"name": "symlink", "ok": False, "detail": "artifact root may not be a symlink"}],
            "children": [],
            "checked_files": 0,
            "passed_checks": 0,
            "failed_checks": 1,
        }
    source = requested.resolve()
    resolved_recipe = _resolve_recipe(source, recipe)
    if source.is_dir():
        result = _validate_directory(source, resolved_recipe, requirements)
    elif source.is_file():
        result = _validate_file(source, resolved_recipe, requirements)
    else:
        raise ValueError("artifact path must be a regular file or directory")
    flat_checks = list(result["checks"])
    for child in result.get("children", []):
        flat_checks.extend(child.get("checks", []))
    result["passed_checks"] = sum(1 for item in flat_checks if item["ok"])
    result["failed_checks"] = sum(1 for item in flat_checks if not item["ok"])
    return result


def format_result(result: dict) -> str:
    lines = [
        "artifact grounding: %s" % ("PASS" if result.get("ok") else "FAIL"),
        "  recipe: %s | files: %s | checks: %s passed, %s failed"
        % (
            result.get("recipe", "unknown"),
            result.get("checked_files", 0),
            result.get("passed_checks", 0),
            result.get("failed_checks", 0),
        ),
        "  path: %s" % result.get("path", ""),
    ]
    failures = []
    for item in result.get("checks", []):
        if not item.get("ok"):
            failures.append(item)
    for child in result.get("children", []):
        for item in child.get("checks", []):
            if not item.get("ok"):
                failures.append({**item, "detail": "%s: %s" % (Path(child.get("path", "")).name, item.get("detail", ""))})
    for item in failures[:30]:
        lines.append("  [FAIL] %s: %s" % (item.get("name"), item.get("detail")))
    if len(failures) > 30:
        lines.append("  ... %d more failures" % (len(failures) - 30))
    return "\n".join(lines)
