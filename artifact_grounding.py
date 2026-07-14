"""Deterministic, stdlib-only validation recipes for generated artifacts."""

from __future__ import annotations

import csv
import hashlib
import json
import math
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
MAX_GLB_ACCESSOR_ITEMS = 2_000_000

GLB_COMPONENTS = {
    5120: ("b", 1),
    5121: ("B", 1),
    5122: ("h", 2),
    5123: ("H", 2),
    5125: ("I", 4),
    5126: ("f", 4),
}
GLB_TYPES = {
    "SCALAR": 1,
    "VEC2": 2,
    "VEC3": 3,
    "VEC4": 4,
    "MAT2": 4,
    "MAT3": 9,
    "MAT4": 16,
}
HUMANOID_JOINT_PARENTS = {
    "Hips": None,
    "Spine": "Hips",
    "Chest": "Spine",
    "Neck": "Chest",
    "Head": "Neck",
    "LeftShoulder": "Chest",
    "LeftElbow": "LeftShoulder",
    "LeftWrist": "LeftElbow",
    "RightShoulder": "Chest",
    "RightElbow": "RightShoulder",
    "RightWrist": "RightElbow",
    "LeftHip": "Hips",
    "LeftKnee": "LeftHip",
    "LeftAnkle": "LeftKnee",
    "RightHip": "Hips",
    "RightKnee": "RightHip",
    "RightAnkle": "RightKnee",
}

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
    ".glb": "glb",
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
    "rigged_model": "glb",
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
    "glb",
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


def _inspect_png_bytes(data: bytes) -> dict:
    result = {
        "chunk_count": 0,
        "crc_ok": True,
        "data_ok": False,
        "errors": [],
        "height": 0,
        "structure_ok": False,
        "width": 0,
    }
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        result["errors"].append("invalid PNG signature")
        return result
    offset = 8
    bit_depth = color_type = compression = filtering = interlace = None
    ended = False
    idat = []
    palette = False
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        if length > MAX_FILE_BYTES or offset + 12 + length > len(data):
            result["errors"].append("truncated or oversized PNG chunk")
            break
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        expected = struct.unpack(">I", data[offset + 8 + length : offset + 12 + length])[0]
        result["crc_ok"] = (
            result["crc_ok"]
            and zlib.crc32(kind + payload) & 0xFFFFFFFF == expected
        )
        result["chunk_count"] += 1
        if kind == b"IHDR" and len(payload) == 13:
            if result["chunk_count"] != 1 or result["width"]:
                result["errors"].append("IHDR must be the first and only header")
            (
                result["width"],
                result["height"],
                bit_depth,
                color_type,
                compression,
                filtering,
                interlace,
            ) = struct.unpack(">IIBBBBB", payload)
        elif kind == b"IHDR":
            result["errors"].append("IHDR has an invalid length")
        elif kind == b"PLTE":
            palette = True
        elif kind == b"IDAT":
            idat.append(payload)
        offset += 12 + length
        if kind == b"IEND":
            if length:
                result["errors"].append("IEND must be empty")
            ended = True
            break
    if not result["width"] or not result["height"]:
        result["errors"].append("missing or invalid IHDR")
    if not idat:
        result["errors"].append("PNG has no IDAT data")
    if not ended:
        result["errors"].append("PNG has no IEND chunk")
    elif offset != len(data):
        result["errors"].append("PNG has trailing data after IEND")
    allowed_depths = {
        0: {1, 2, 4, 8, 16},
        2: {8, 16},
        3: {1, 2, 4, 8},
        4: {8, 16},
        6: {8, 16},
    }
    if color_type not in allowed_depths or bit_depth not in allowed_depths.get(color_type, set()):
        result["errors"].append("PNG has an invalid color type/bit depth")
    if compression != 0 or filtering != 0 or interlace not in {0, 1}:
        result["errors"].append("PNG uses unsupported header methods")
    if color_type == 3 and not palette:
        result["errors"].append("indexed PNG is missing PLTE")
    if color_type in {0, 4} and palette:
        result["errors"].append("grayscale PNG must not contain PLTE")
    result["structure_ok"] = not result["errors"]

    data_errors = []
    if result["structure_ok"] and interlace == 0:
        channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
        row_bytes = (result["width"] * channels * bit_depth + 7) // 8
        expected_bytes = result["height"] * (row_bytes + 1)
        try:
            inflater = zlib.decompressobj()
            decoded = inflater.decompress(b"".join(idat), expected_bytes + 1)
            if len(decoded) <= expected_bytes:
                decoded += inflater.flush(expected_bytes + 1 - len(decoded))
            if (
                len(decoded) != expected_bytes
                or not inflater.eof
                or inflater.unused_data
                or inflater.unconsumed_tail
            ):
                data_errors.append("PNG pixel stream has the wrong decoded size")
            elif any(decoded[row * (row_bytes + 1)] > 4 for row in range(result["height"])):
                data_errors.append("PNG pixel stream has an invalid row filter")
        except zlib.error as exc:
            data_errors.append("invalid PNG pixel stream: %s" % exc)
        result["data_ok"] = not data_errors
    elif result["structure_ok"]:
        result["data_ok"] = True
    result["errors"].extend(data_errors)
    return result


def _validate_png(path: Path, requirements: dict, checks: list):
    try:
        data = _read_bytes(path)
    except (OSError, ValueError) as exc:
        _check(checks, "valid-png", False, str(exc))
        return
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        _check(checks, "valid-png", False, "invalid PNG signature")
        return
    info = _inspect_png_bytes(data)
    _check(
        checks,
        "png-structure",
        info["structure_ok"],
        "%dx%d, %d chunks%s"
        % (
            info["width"],
            info["height"],
            info["chunk_count"],
            ("; " + "; ".join(info["errors"][:4])) if info["errors"] else "",
        ),
    )
    _check(checks, "png-crc", info["crc_ok"], "all parsed chunk CRCs match")
    _check(checks, "png-pixels", info["data_ok"], "zlib stream and row filters are valid")
    max_side = _bounded_int(requirements, "max_side", 32768, 1, 32768)
    min_side = _bounded_int(requirements, "min_side", 1, 1, max_side)
    _check(
        checks,
        "png-dimensions",
        min_side <= info["width"] <= max_side and min_side <= info["height"] <= max_side,
        "%dx%d (each side %d..%d)"
        % (info["width"], info["height"], min_side, max_side),
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


def _parse_glb(data: bytes):
    if len(data) < 20:
        raise ValueError("GLB is shorter than its header and JSON chunk")
    magic, version, declared_length = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF":
        raise ValueError("missing glTF magic")
    if version != 2:
        raise ValueError("GLB version must be 2")
    if declared_length != len(data):
        raise ValueError(
            "declared GLB length %d does not match %d bytes"
            % (declared_length, len(data))
        )
    if declared_length % 4:
        raise ValueError("GLB length must be 4-byte aligned")
    chunks = []
    offset = 12
    while offset < len(data):
        if offset + 8 > len(data):
            raise ValueError("truncated GLB chunk header")
        length, kind = struct.unpack_from("<II", data, offset)
        offset += 8
        if length % 4:
            raise ValueError("GLB chunk length must be 4-byte aligned")
        end = offset + length
        if end > len(data):
            raise ValueError("GLB chunk exceeds the declared container length")
        chunks.append((kind, data[offset:end]))
        offset = end
    if not chunks or chunks[0][0] != 0x4E4F534A:
        raise ValueError("the first GLB chunk must be JSON")
    if sum(kind == 0x4E4F534A for kind, _payload in chunks) != 1:
        raise ValueError("GLB must contain exactly one JSON chunk")
    if sum(kind == 0x004E4942 for kind, _payload in chunks) > 1:
        raise ValueError("GLB may contain at most one BIN chunk")
    if any(kind == 0x004E4942 for kind, _payload in chunks) and chunks[1][0] != 0x004E4942:
        raise ValueError("the BIN chunk must immediately follow the JSON chunk")
    try:
        json_text = chunks[0][1].decode("utf-8")
        document, json_end = json.JSONDecoder().raw_decode(json_text)
        if any(character != " " for character in json_text[json_end:]):
            raise ValueError("GLB JSON padding must contain only spaces")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid GLB JSON chunk: %s" % exc) from exc
    if not isinstance(document, dict):
        raise ValueError("GLB JSON root must be an object")
    binary = next(
        (payload for kind, payload in chunks[1:] if kind == 0x004E4942), b""
    )
    return document, binary, chunks


def _glb_integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _glb_accessor_values(document, binary, index):
    accessors = document.get("accessors", [])
    views = document.get("bufferViews", [])
    if not _glb_integer(index) or index < 0 or index >= len(accessors):
        raise ValueError("invalid accessor index %r" % index)
    accessor = accessors[index]
    if not isinstance(accessor, dict):
        raise ValueError("accessor %d must be an object" % index)
    view_index = accessor.get("bufferView")
    if not _glb_integer(view_index) or view_index < 0 or view_index >= len(views):
        raise ValueError("accessor %d has an invalid bufferView" % index)
    view = views[view_index]
    if (
        not isinstance(view, dict)
        or not _glb_integer(view.get("buffer"))
        or view.get("buffer") != 0
    ):
        raise ValueError("accessor %d must use embedded buffer 0" % index)
    component_type = accessor.get("componentType")
    value_type = accessor.get("type")
    count = accessor.get("count")
    if component_type not in GLB_COMPONENTS or value_type not in GLB_TYPES:
        raise ValueError("accessor %d has an unsupported component/type" % index)
    if not _glb_integer(count) or count < 1 or count > MAX_GLB_ACCESSOR_ITEMS:
        raise ValueError("accessor %d has an invalid count" % index)
    code, component_size = GLB_COMPONENTS[component_type]
    components = GLB_TYPES[value_type]
    element_size = component_size * components
    stride = view.get("byteStride", element_size)
    if (
        not _glb_integer(stride)
        or stride < element_size
        or stride > 252
        or ("byteStride" in view and stride % 4)
    ):
        raise ValueError("accessor %d has an invalid byteStride" % index)
    view_offset = view.get("byteOffset", 0)
    view_length = view.get("byteLength")
    accessor_offset = accessor.get("byteOffset", 0)
    if (
        not _glb_integer(view_offset)
        or view_offset < 0
        or not _glb_integer(view_length)
        or view_length < 1
        or not _glb_integer(accessor_offset)
        or accessor_offset < 0
    ):
        raise ValueError("accessor %d has a non-integer byte offset" % index)
    if (view_offset + accessor_offset) % component_size:
        raise ValueError("accessor %d has a misaligned component offset" % index)
    start = view_offset + accessor_offset
    end = start + (count - 1) * stride + element_size
    view_end = view_offset + view_length
    if start < view_offset or end > view_end or end > len(binary):
        raise ValueError("accessor %d exceeds its bufferView" % index)
    unpacker = struct.Struct("<" + code * components)
    return [unpacker.unpack_from(binary, start + row * stride) for row in range(count)]


def _validate_glb(path: Path, requirements: dict, checks: list):
    try:
        data = _read_bytes(path)
        document, binary, chunks = _parse_glb(data)
    except (OSError, ValueError, struct.error) as exc:
        _check(checks, "valid-glb", False, str(exc))
        return
    _check(
        checks,
        "valid-glb",
        True,
        "%d bytes, %d chunk(s)" % (len(data), len(chunks)),
    )

    asset = document.get("asset")
    asset_ok = isinstance(asset, dict) and asset.get("version") == "2.0"
    _check(checks, "glb-asset-version", asset_ok, "glTF asset version 2.0")

    buffer_errors = []
    buffers = document.get("buffers", [])
    if not isinstance(buffers, list) or len(buffers) != 1 or not isinstance(buffers[0], dict):
        buffer_errors.append("expected one embedded buffer")
        declared_buffer = -1
    else:
        declared_buffer = buffers[0].get("byteLength")
        if not _glb_integer(declared_buffer) or declared_buffer < 1:
            buffer_errors.append("buffer byteLength must be a positive integer")
            declared_buffer = -1
        elif declared_buffer > len(binary) or len(binary) - declared_buffer > 3:
            buffer_errors.append("BIN chunk length does not match buffer byteLength")
        elif any(binary[declared_buffer:]):
            buffer_errors.append("BIN chunk padding must contain only zero bytes")
    views = document.get("bufferViews", [])
    if not isinstance(views, list) or not views:
        buffer_errors.append("bufferViews must be a non-empty array")
        views = []
    for index, view in enumerate(views):
        if not isinstance(view, dict):
            buffer_errors.append("bufferView %d is not an object" % index)
            continue
        offset = view.get("byteOffset", 0)
        length = view.get("byteLength")
        if not _glb_integer(view.get("buffer")) or view.get("buffer") != 0:
            buffer_errors.append("bufferView %d does not use buffer 0" % index)
        if not _glb_integer(offset) or offset < 0 or offset % 4:
            buffer_errors.append("bufferView %d has an unaligned offset" % index)
        if not _glb_integer(length) or length < 1:
            buffer_errors.append("bufferView %d has an invalid length" % index)
        elif _glb_integer(offset) and offset + length > max(0, declared_buffer):
            buffer_errors.append("bufferView %d exceeds the buffer" % index)
        stride = view.get("byteStride")
        if stride is not None and (
            not _glb_integer(stride) or stride < 4 or stride > 252 or stride % 4
        ):
            buffer_errors.append("bufferView %d has an invalid byteStride" % index)
        if "target" in view and view["target"] not in {34962, 34963}:
            buffer_errors.append("bufferView %d has an invalid target" % index)
    _check(
        checks,
        "glb-buffers",
        not buffer_errors,
        "; ".join(buffer_errors[:8]) or "%d bufferView(s)" % len(views),
    )

    accessor_errors = []
    accessors = document.get("accessors", [])
    if not isinstance(accessors, list) or not accessors:
        accessor_errors.append("accessors must be a non-empty array")
        accessors = []
    accessor_cache = {}
    for index, accessor in enumerate(accessors):
        if not isinstance(accessor, dict):
            accessor_errors.append("accessor %d is not an object" % index)
            continue
        if "sparse" in accessor:
            accessor_errors.append("accessor %d uses unsupported sparse storage" % index)
        component_type = accessor.get("componentType")
        value_type = accessor.get("type")
        if component_type not in GLB_COMPONENTS or value_type not in GLB_TYPES:
            accessor_errors.append("accessor %d has an invalid component/type" % index)
            continue
        if accessor.get("normalized") and component_type == 5126:
            accessor_errors.append("accessor %d normalizes floating-point data" % index)
        try:
            values = _glb_accessor_values(document, binary, index)
            accessor_cache[index] = values
        except (ValueError, struct.error) as exc:
            accessor_errors.append(str(exc))
            continue
        if component_type == 5126 and any(
            not math.isfinite(float(value)) for row in values for value in row
        ):
            accessor_errors.append("accessor %d contains non-finite values" % index)
        for key, chooser in (("min", min), ("max", max)):
            declared = accessor.get(key)
            if declared is None:
                continue
            components = GLB_TYPES[value_type]
            if not isinstance(declared, list) or len(declared) != components:
                accessor_errors.append("accessor %d has an invalid %s" % (index, key))
                continue
            actual = [chooser(row[column] for row in values) for column in range(components)]
            if any(
                not isinstance(declared[column], (int, float))
                or not math.isclose(float(declared[column]), float(actual[column]), rel_tol=1e-5, abs_tol=1e-5)
                for column in range(components)
            ):
                accessor_errors.append("accessor %d %s does not match its data" % (index, key))
    _check(
        checks,
        "glb-accessors",
        not accessor_errors,
        "; ".join(accessor_errors[:8]) or "%d accessor(s)" % len(accessors),
    )

    nodes = document.get("nodes", [])
    meshes = document.get("meshes", [])
    skins = document.get("skins", [])
    materials = document.get("materials", [])
    node_errors = []
    if not isinstance(nodes, list) or not nodes:
        node_errors.append("nodes must be a non-empty array")
        nodes = []
    if not isinstance(meshes, list) or not meshes:
        node_errors.append("meshes must be a non-empty array")
        meshes = []
    if not isinstance(skins, list):
        node_errors.append("skins must be an array")
        skins = []
    if not isinstance(materials, list):
        node_errors.append("materials must be an array")
        materials = []
    mesh_skins = {}
    graph = {}
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            node_errors.append("node %d is not an object" % index)
            continue
        children = node.get("children", [])
        if not isinstance(children, list) or any(
            not _glb_integer(child) or child < 0 or child >= len(nodes) or child == index
            for child in children
        ):
            node_errors.append("node %d has invalid children" % index)
            children = []
        graph[index] = children
        mesh_index = node.get("mesh")
        skin_index = node.get("skin")
        if mesh_index is not None and (
            not _glb_integer(mesh_index) or mesh_index < 0 or mesh_index >= len(meshes)
        ):
            node_errors.append("node %d has an invalid mesh" % index)
        if skin_index is not None and (
            not _glb_integer(skin_index) or skin_index < 0 or skin_index >= len(skins)
        ):
            node_errors.append("node %d has an invalid skin" % index)
        if _glb_integer(mesh_index) and _glb_integer(skin_index):
            mesh_skins.setdefault(mesh_index, set()).add(skin_index)
    indegree = {index: 0 for index in graph}
    parent_of = {}
    for parent_index, children in graph.items():
        for child in children:
            if child in indegree:
                indegree[child] += 1
                parent_of[child] = parent_index
    if any(value > 1 for value in indegree.values()):
        node_errors.append("a node has multiple parents")
    pending = [index for index, value in indegree.items() if value == 0]
    processed = 0
    while pending:
        parent = pending.pop()
        processed += 1
        for child in graph.get(parent, []):
            if child not in indegree:
                continue
            indegree[child] -= 1
            if indegree[child] == 0:
                pending.append(child)
    if processed != len(graph):
        node_errors.append("node hierarchy contains a cycle")
    scenes = document.get("scenes", [])
    default_scene = document.get("scene")
    if not isinstance(scenes, list) or not scenes:
        node_errors.append("scenes must be a non-empty array")
    else:
        if not _glb_integer(default_scene) or default_scene < 0 or default_scene >= len(scenes):
            node_errors.append("default scene is invalid")
        for scene_index, scene in enumerate(scenes):
            roots = scene.get("nodes", []) if isinstance(scene, dict) else None
            if not isinstance(roots, list) or any(
                not _glb_integer(root) or root < 0 or root >= len(nodes) for root in roots
            ):
                node_errors.append("scene %d has invalid root nodes" % scene_index)
    _check(
        checks,
        "glb-scene",
        not node_errors,
        "; ".join(node_errors[:8]) or "%d node(s), %d scene(s)" % (len(nodes), len(scenes)),
    )

    image_errors = []
    images = document.get("images", [])
    textures = document.get("textures", [])
    texture_samplers = document.get("samplers", [])
    if not isinstance(images, list):
        image_errors.append("images must be an array")
        images = []
    if not isinstance(textures, list):
        image_errors.append("textures must be an array")
        textures = []
    if not isinstance(texture_samplers, list):
        image_errors.append("samplers must be an array")
        texture_samplers = []
    embedded_images = 0
    power_of_two_images = 0
    for image_index, image in enumerate(images):
        if not isinstance(image, dict):
            image_errors.append("image %d is not an object" % image_index)
            continue
        has_uri = "uri" in image
        has_view = "bufferView" in image
        if has_uri == has_view:
            image_errors.append("image %d must use exactly one URI or bufferView" % image_index)
            continue
        if has_uri:
            if not isinstance(image.get("uri"), str) or not image["uri"].strip():
                image_errors.append("image %d has an invalid URI" % image_index)
            continue
        view_index = image.get("bufferView")
        mime_type = image.get("mimeType")
        if not _glb_integer(view_index) or view_index < 0 or view_index >= len(views):
            image_errors.append("image %d has an invalid bufferView" % image_index)
            continue
        if mime_type not in {"image/png", "image/jpeg"}:
            image_errors.append("image %d has an invalid embedded MIME type" % image_index)
            continue
        view = views[view_index]
        try:
            if not isinstance(view, dict) or "target" in view:
                raise ValueError("image bufferView must not declare a target")
            start = view.get("byteOffset", 0)
            length = view.get("byteLength")
            if (
                not _glb_integer(start)
                or start < 0
                or not _glb_integer(length)
                or length < 1
                or start + length > len(binary)
            ):
                raise ValueError("image bufferView is out of bounds")
            payload = binary[start:start + length]
            if mime_type == "image/png":
                info = _inspect_png_bytes(payload)
                if not info["structure_ok"] or not info["crc_ok"] or not info["data_ok"]:
                    raise ValueError(
                        "invalid embedded PNG: %s"
                        % ("; ".join(info["errors"][:4]) or "CRC mismatch")
                    )
                if (
                    info["width"] > 0
                    and info["height"] > 0
                    and info["width"] & (info["width"] - 1) == 0
                    and info["height"] & (info["height"] - 1) == 0
                ):
                    power_of_two_images += 1
            elif not (
                len(payload) >= 4
                and payload.startswith(b"\xff\xd8")
                and payload.endswith(b"\xff\xd9")
            ):
                raise ValueError("invalid embedded JPEG markers")
            embedded_images += 1
        except (TypeError, ValueError) as exc:
            image_errors.append("image %d: %s" % (image_index, exc))
    minimum_images = _bounded_int(requirements, "min_images", 0, 0, 100_000)
    require_embedded_images = bool(requirements.get("require_embedded_images"))
    require_power_of_two_images = bool(
        requirements.get("require_power_of_two_images")
    )
    images_ok = (
        not image_errors
        and len(images) >= minimum_images
        and (
            not require_embedded_images
            or (embedded_images == len(images) and embedded_images > 0)
        )
        and (
            not require_power_of_two_images
            or (power_of_two_images == len(images) and power_of_two_images > 0)
        )
    )
    _check(
        checks,
        "glb-images",
        images_ok,
        (
            "; ".join(image_errors[:8])
            or "%d image(s), %d embedded, %d power-of-two"
            % (len(images), embedded_images, power_of_two_images)
        ),
    )

    material_errors = []
    for sampler_index, sampler in enumerate(texture_samplers):
        if not isinstance(sampler, dict):
            material_errors.append("sampler %d is not an object" % sampler_index)
            continue
        if "magFilter" in sampler and sampler["magFilter"] not in {9728, 9729}:
            material_errors.append("sampler %d has an invalid magFilter" % sampler_index)
        if "minFilter" in sampler and sampler["minFilter"] not in {
            9728, 9729, 9984, 9985, 9986, 9987,
        }:
            material_errors.append("sampler %d has an invalid minFilter" % sampler_index)
        for field in ("wrapS", "wrapT"):
            if field in sampler and sampler[field] not in {33071, 33648, 10497}:
                material_errors.append(
                    "sampler %d has an invalid %s" % (sampler_index, field)
                )
    for texture_index, texture in enumerate(textures):
        if not isinstance(texture, dict):
            material_errors.append("texture %d is not an object" % texture_index)
            continue
        source = texture.get("source")
        sampler = texture.get("sampler")
        if not _glb_integer(source) or source < 0 or source >= len(images):
            material_errors.append("texture %d has an invalid source" % texture_index)
        if sampler is not None and (
            not _glb_integer(sampler)
            or sampler < 0
            or sampler >= len(texture_samplers)
        ):
            material_errors.append("texture %d has an invalid sampler" % texture_index)

    material_texcoords = {}
    normal_mapped_materials = set()
    used_texture_slots = 0

    def texture_slot(material_index, owner, field):
        nonlocal used_texture_slots
        if field not in owner:
            return
        info = owner[field]
        if not isinstance(info, dict):
            material_errors.append("material %d %s is not an object" % (material_index, field))
            return
        texture_index = info.get("index")
        texcoord = info.get("texCoord", 0)
        if (
            not _glb_integer(texture_index)
            or texture_index < 0
            or texture_index >= len(textures)
        ):
            material_errors.append("material %d %s has an invalid texture" % (material_index, field))
        if not _glb_integer(texcoord) or texcoord < 0:
            material_errors.append("material %d %s has an invalid texCoord" % (material_index, field))
            return
        material_texcoords.setdefault(material_index, set()).add(texcoord)
        used_texture_slots += 1

    for material_index, material in enumerate(materials):
        if not isinstance(material, dict):
            material_errors.append("material %d is not an object" % material_index)
            continue
        pbr = material.get("pbrMetallicRoughness", {})
        if not isinstance(pbr, dict):
            material_errors.append("material %d PBR data is not an object" % material_index)
            pbr = {}
        factor = pbr.get("baseColorFactor", [1.0, 1.0, 1.0, 1.0])
        if (
            not isinstance(factor, list)
            or len(factor) != 4
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0.0 <= float(value) <= 1.0
                for value in factor
            )
        ):
            material_errors.append("material %d has an invalid baseColorFactor" % material_index)
        for field in ("metallicFactor", "roughnessFactor"):
            value = pbr.get(field, 1.0)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0.0 <= float(value) <= 1.0
            ):
                material_errors.append("material %d has an invalid %s" % (material_index, field))
        emissive = material.get("emissiveFactor", [0.0, 0.0, 0.0])
        if (
            not isinstance(emissive, list)
            or len(emissive) != 3
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0.0 <= float(value) <= 1.0
                for value in emissive
            )
        ):
            material_errors.append("material %d has an invalid emissiveFactor" % material_index)
        texture_slot(material_index, pbr, "baseColorTexture")
        texture_slot(material_index, pbr, "metallicRoughnessTexture")
        texture_slot(material_index, material, "normalTexture")
        texture_slot(material_index, material, "occlusionTexture")
        texture_slot(material_index, material, "emissiveTexture")
        if "normalTexture" in material:
            normal_mapped_materials.add(material_index)
            normal_info = material.get("normalTexture")
            scale = normal_info.get("scale", 1.0) if isinstance(normal_info, dict) else None
            if (
                isinstance(scale, bool)
                or not isinstance(scale, (int, float))
                or not math.isfinite(float(scale))
            ):
                material_errors.append(
                    "material %d has an invalid normal scale" % material_index
                )
        if "occlusionTexture" in material:
            occlusion_info = material.get("occlusionTexture")
            strength = (
                occlusion_info.get("strength", 1.0)
                if isinstance(occlusion_info, dict)
                else None
            )
            if (
                isinstance(strength, bool)
                or not isinstance(strength, (int, float))
                or not math.isfinite(float(strength))
                or not 0.0 <= float(strength) <= 1.0
            ):
                material_errors.append(
                    "material %d has an invalid occlusion strength" % material_index
                )
    minimum_materials = _bounded_int(requirements, "min_materials", 0, 0, 100_000)
    minimum_textures = _bounded_int(requirements, "min_textures", 0, 0, 100_000)
    require_material_textures = bool(requirements.get("require_material_textures"))
    materials_ok = (
        not material_errors
        and len(materials) >= minimum_materials
        and len(textures) >= minimum_textures
        and (not require_material_textures or used_texture_slots > 0)
    )
    _check(
        checks,
        "glb-materials",
        materials_ok,
        (
            "; ".join(material_errors[:8])
            or "%d material(s), %d texture(s), %d used slot(s)"
            % (len(materials), len(textures), used_texture_slots)
        ),
    )

    skin_errors = []
    joint_sets = []
    maximum_joints = 0
    for skin_index, skin in enumerate(skins):
        if not isinstance(skin, dict):
            skin_errors.append("skin %d is not an object" % skin_index)
            joint_sets.append(set())
            continue
        joint_list = skin.get("joints")
        if not isinstance(joint_list, list) or not joint_list:
            skin_errors.append("skin %d has no joints" % skin_index)
            joint_list = []
        elif any(
            not _glb_integer(joint) or joint < 0 or joint >= len(nodes)
            for joint in joint_list
        ) or len(set(joint_list)) != len(joint_list):
            skin_errors.append("skin %d has invalid or duplicate joints" % skin_index)
        joint_set = {joint for joint in joint_list if _glb_integer(joint)}
        joint_sets.append(joint_set)
        maximum_joints = max(maximum_joints, len(joint_list))
        bind_index = skin.get("inverseBindMatrices")
        if bind_index is not None:
            if not _glb_integer(bind_index) or bind_index < 0 or bind_index >= len(accessors):
                skin_errors.append("skin %d has invalid inverse bind matrices" % skin_index)
            else:
                bind = accessors[bind_index]
                if (
                    not isinstance(bind, dict)
                    or bind.get("componentType") != 5126
                    or bind.get("type") != "MAT4"
                    or bind.get("count") != len(joint_list)
                ):
                    skin_errors.append("skin %d inverse bind matrices do not match joints" % skin_index)
                else:
                    try:
                        bind_rows = accessor_cache.get(bind_index) or _glb_accessor_values(
                            document, binary, bind_index
                        )
                        if len(bind_rows) != len(joint_list) or any(
                            len(row) != 16
                            or any(not math.isfinite(float(value)) for value in row)
                            or not math.isclose(float(row[3]), 0.0, abs_tol=1e-5)
                            or not math.isclose(float(row[7]), 0.0, abs_tol=1e-5)
                            or not math.isclose(float(row[11]), 0.0, abs_tol=1e-5)
                            or not math.isclose(float(row[15]), 1.0, abs_tol=1e-5)
                            for row in bind_rows
                        ):
                            raise ValueError("must contain finite affine MAT4 values")
                    except (IndexError, KeyError, TypeError, ValueError, struct.error) as exc:
                        skin_errors.append(
                            "skin %d inverse bind matrices %s" % (skin_index, exc)
                        )
        skeleton = skin.get("skeleton")
        if skeleton is not None and (
            not _glb_integer(skeleton) or skeleton < 0 or skeleton >= len(nodes)
        ):
            skin_errors.append("skin %d has an invalid skeleton" % skin_index)

    require_humanoid_rig = bool(requirements.get("require_humanoid_rig"))
    required_joint_names = set(_string_list(requirements, "required_joint_names"))
    if require_humanoid_rig:
        required_joint_names.update(HUMANOID_JOINT_PARENTS)
    humanoid_errors = []
    matched_humanoid_names = set()
    matched_humanoid_skin = None
    for skin_index, joint_set in enumerate(joint_sets):
        names = {}
        duplicates = set()
        for joint in joint_set:
            node = nodes[joint] if 0 <= joint < len(nodes) else None
            name = node.get("name") if isinstance(node, dict) else None
            if isinstance(name, str) and name.strip():
                if name in names:
                    duplicates.add(name)
                names[name] = joint
        if duplicates:
            humanoid_errors.append(
                "skin %d duplicates joint names: %s"
                % (skin_index, ", ".join(sorted(duplicates)))
            )
        if len(required_joint_names & set(names)) > len(matched_humanoid_names):
            matched_humanoid_names = required_joint_names & set(names)
            matched_humanoid_skin = (skin_index, names)
    missing_joint_names = sorted(required_joint_names - matched_humanoid_names)
    if missing_joint_names:
        humanoid_errors.append(
            "missing required joint names: %s" % ", ".join(missing_joint_names)
        )
    if require_humanoid_rig and matched_humanoid_skin is not None and not missing_joint_names:
        skin_index, names = matched_humanoid_skin
        for child_name, parent_name in HUMANOID_JOINT_PARENTS.items():
            child = names[child_name]
            if parent_name is None:
                skeleton = skins[skin_index].get("skeleton")
                if skeleton != child:
                    humanoid_errors.append("skin skeleton must be the Hips joint")
            elif parent_of.get(child) != names[parent_name]:
                humanoid_errors.append(
                    "%s must be a direct child of %s" % (child_name, parent_name)
                )
    humanoid_ok = not humanoid_errors and (
        not required_joint_names or matched_humanoid_skin is not None
    )
    _check(
        checks,
        "glb-humanoid-rig",
        humanoid_ok,
        (
            "; ".join(humanoid_errors[:8])
            or "%d required humanoid joint name(s) matched"
            % len(matched_humanoid_names)
        ),
    )

    geometry_errors = []
    tangent_errors = []
    texcoord_errors = []
    total_vertices = 0
    total_triangles = 0
    maximum_texcoord_sets = 0
    rigged_primitives = 0
    normal_mapped_primitives = 0
    textured_primitives = 0
    for mesh_index, mesh in enumerate(meshes):
        primitives = mesh.get("primitives", []) if isinstance(mesh, dict) else None
        if not isinstance(primitives, list) or not primitives:
            geometry_errors.append("mesh %d has no primitives" % mesh_index)
            continue
        for primitive_index, primitive in enumerate(primitives):
            if not isinstance(primitive, dict):
                geometry_errors.append("mesh %d primitive %d is invalid" % (mesh_index, primitive_index))
                continue
            attributes = primitive.get("attributes")
            position_index = attributes.get("POSITION") if isinstance(attributes, dict) else None
            try:
                position_accessor = accessors[position_index]
                position_rows = accessor_cache.get(position_index) or _glb_accessor_values(
                    document, binary, position_index
                )
                if position_accessor.get("type") != "VEC3" or position_accessor.get("componentType") != 5126:
                    raise ValueError("POSITION must be floating-point VEC3")
                if any(not math.isfinite(float(value)) for row in position_rows for value in row):
                    raise ValueError("POSITION contains non-finite values")
            except (IndexError, KeyError, TypeError, ValueError, struct.error) as exc:
                geometry_errors.append(
                    "mesh %d primitive %d: %s" % (mesh_index, primitive_index, exc)
                )
                continue
            vertex_count = len(position_rows)
            total_vertices += vertex_count
            normal_index = attributes.get("NORMAL")
            normal_rows = None
            if normal_index is not None:
                try:
                    normal_accessor = accessors[normal_index]
                    normal_rows = accessor_cache.get(normal_index) or _glb_accessor_values(
                        document, binary, normal_index
                    )
                    if normal_accessor.get("type") != "VEC3" or len(normal_rows) != vertex_count:
                        raise ValueError("NORMAL must be VEC3 and match POSITION count")
                    if any(
                        not math.isclose(
                            math.sqrt(sum(float(value) ** 2 for value in row)),
                            1.0,
                            rel_tol=1e-4,
                            abs_tol=1e-4,
                        )
                        for row in normal_rows
                    ):
                        raise ValueError("NORMAL vectors must be unit length")
                except (IndexError, TypeError, ValueError, struct.error) as exc:
                    geometry_errors.append(
                        "mesh %d primitive %d: %s" % (mesh_index, primitive_index, exc)
                    )
            index_index = primitive.get("indices")
            if index_index is None:
                index_values = list(range(vertex_count))
            else:
                try:
                    index_accessor = accessors[index_index]
                    index_rows = accessor_cache.get(index_index) or _glb_accessor_values(
                        document, binary, index_index
                    )
                    if index_accessor.get("type") != "SCALAR" or index_accessor.get("componentType") not in {5121, 5123, 5125}:
                        raise ValueError("indices must be an unsigned SCALAR accessor")
                    index_values = [int(row[0]) for row in index_rows]
                except (IndexError, TypeError, ValueError, struct.error) as exc:
                    geometry_errors.append(
                        "mesh %d primitive %d: %s" % (mesh_index, primitive_index, exc)
                    )
                    continue
            if any(index < 0 or index >= vertex_count for index in index_values):
                geometry_errors.append("mesh %d primitive %d has out-of-range indices" % (mesh_index, primitive_index))
            mode = primitive.get("mode", 4)
            if mode == 4:
                if len(index_values) % 3:
                    geometry_errors.append("mesh %d primitive %d has an incomplete triangle" % (mesh_index, primitive_index))
                total_triangles += len(index_values) // 3
            material = primitive.get("material")
            if material is not None and (
                not _glb_integer(material) or material < 0 or material >= len(materials)
            ):
                geometry_errors.append("mesh %d primitive %d has an invalid material" % (mesh_index, primitive_index))
            required_sets = material_texcoords.get(material, set())
            if required_sets:
                textured_primitives += 1
            for texcoord_set in required_sets:
                attribute_name = "TEXCOORD_%d" % texcoord_set
                texcoord_index = attributes.get(attribute_name)
                try:
                    if texcoord_index is None:
                        raise ValueError("%s is missing" % attribute_name)
                    texcoord_accessor = accessors[texcoord_index]
                    texcoord_rows = accessor_cache.get(texcoord_index) or _glb_accessor_values(
                        document, binary, texcoord_index
                    )
                    valid_component = (
                        texcoord_accessor.get("componentType") == 5126
                        or (
                            texcoord_accessor.get("componentType") in {5121, 5123}
                            and texcoord_accessor.get("normalized") is True
                        )
                    )
                    if texcoord_accessor.get("type") != "VEC2" or not valid_component:
                        raise ValueError("%s must be float or normalized unsigned VEC2" % attribute_name)
                    if len(texcoord_rows) != vertex_count:
                        raise ValueError("%s count must match POSITION" % attribute_name)
                    if any(
                        not math.isfinite(float(value))
                        for row in texcoord_rows
                        for value in row
                    ):
                        raise ValueError("%s contains non-finite values" % attribute_name)
                    maximum_texcoord_sets = max(maximum_texcoord_sets, texcoord_set + 1)
                except (IndexError, KeyError, TypeError, ValueError, struct.error) as exc:
                    texcoord_errors.append(
                        "mesh %d primitive %d: %s"
                        % (mesh_index, primitive_index, exc)
                    )
            if material in normal_mapped_materials:
                normal_mapped_primitives += 1
                tangent_index = attributes.get("TANGENT")
                try:
                    if normal_rows is None:
                        raise ValueError("NORMAL is required for a normal map")
                    if tangent_index is None:
                        raise ValueError("TANGENT is missing")
                    tangent_accessor = accessors[tangent_index]
                    tangent_rows = accessor_cache.get(tangent_index) or _glb_accessor_values(
                        document, binary, tangent_index
                    )
                    if (
                        tangent_accessor.get("type") != "VEC4"
                        or tangent_accessor.get("componentType") != 5126
                    ):
                        raise ValueError("TANGENT must be floating-point VEC4")
                    if len(tangent_rows) != vertex_count:
                        raise ValueError("TANGENT count must match POSITION")
                    if any(
                        not math.isclose(
                            math.sqrt(sum(float(value) ** 2 for value in row[:3])),
                            1.0,
                            rel_tol=1e-4,
                            abs_tol=1e-4,
                        )
                        or not math.isclose(abs(float(row[3])), 1.0, abs_tol=1e-4)
                        for row in tangent_rows
                    ):
                        raise ValueError("TANGENT vectors and handedness must be normalized")
                    if normal_rows is not None and any(
                        not math.isclose(
                            sum(
                                float(normal_value) * float(tangent_value)
                                for normal_value, tangent_value in zip(normal, tangent[:3])
                            ),
                            0.0,
                            abs_tol=1e-4,
                        )
                        for normal, tangent in zip(normal_rows, tangent_rows)
                    ):
                        raise ValueError("TANGENT vectors must be orthogonal to NORMAL")
                except (IndexError, KeyError, TypeError, ValueError, struct.error) as exc:
                    tangent_errors.append(
                        "mesh %d primitive %d: %s"
                        % (mesh_index, primitive_index, exc)
                    )
            joint_index = attributes.get("JOINTS_0")
            weight_index = attributes.get("WEIGHTS_0")
            if (joint_index is None) != (weight_index is None):
                skin_errors.append("mesh %d primitive %d has incomplete skin attributes" % (mesh_index, primitive_index))
            elif joint_index is not None:
                rigged_primitives += 1
                try:
                    joint_accessor = accessors[joint_index]
                    weight_accessor = accessors[weight_index]
                    joint_rows = accessor_cache.get(joint_index) or _glb_accessor_values(
                        document, binary, joint_index
                    )
                    weight_rows = accessor_cache.get(weight_index) or _glb_accessor_values(
                        document, binary, weight_index
                    )
                    if joint_accessor.get("type") != "VEC4" or joint_accessor.get("componentType") not in {5121, 5123}:
                        raise ValueError("JOINTS_0 must be unsigned VEC4")
                    if weight_accessor.get("type") != "VEC4" or weight_accessor.get("componentType") != 5126:
                        raise ValueError("WEIGHTS_0 must be floating-point VEC4")
                    if len(joint_rows) != vertex_count or len(weight_rows) != vertex_count:
                        raise ValueError("skin attribute counts must match POSITION")
                    skin_indices = mesh_skins.get(mesh_index, set())
                    if not skin_indices:
                        raise ValueError("skinned mesh is not attached to a skin")
                    joint_limit = min(len(skins[skin]["joints"]) for skin in skin_indices)
                    if any(int(value) < 0 or int(value) >= joint_limit for row in joint_rows for value in row):
                        raise ValueError("JOINTS_0 contains an out-of-range joint")
                    if any(
                        any(not math.isfinite(float(value)) or float(value) < 0.0 for value in row)
                        or not math.isclose(sum(float(value) for value in row), 1.0, rel_tol=1e-4, abs_tol=1e-4)
                        for row in weight_rows
                    ):
                        raise ValueError("WEIGHTS_0 rows must be finite, nonnegative, and sum to one")
                except (IndexError, KeyError, TypeError, ValueError, struct.error) as exc:
                    skin_errors.append(
                        "mesh %d primitive %d: %s" % (mesh_index, primitive_index, exc)
                    )

    minimum_vertices = _bounded_int(requirements, "min_vertices", 3, 0, 10_000_000)
    minimum_triangles = _bounded_int(requirements, "min_triangles", 1, 0, 10_000_000)
    geometry_ok = (
        not geometry_errors
        and total_vertices >= minimum_vertices
        and total_triangles >= minimum_triangles
    )
    _check(
        checks,
        "glb-geometry",
        geometry_ok,
        (
            "; ".join(geometry_errors[:8])
            or "%d vertices, %d triangles" % (total_vertices, total_triangles)
        ),
    )

    minimum_texcoord_sets = _bounded_int(
        requirements, "min_texcoord_sets", 0, 0, 100_000
    )
    texcoords_ok = (
        not texcoord_errors
        and maximum_texcoord_sets >= minimum_texcoord_sets
        and (not require_material_textures or textured_primitives > 0)
    )
    _check(
        checks,
        "glb-texture-coordinates",
        texcoords_ok,
        (
            "; ".join(texcoord_errors[:8])
            or "%d coordinate set(s), %d textured primitive(s)"
            % (maximum_texcoord_sets, textured_primitives)
        ),
    )

    require_tangents = bool(requirements.get("require_tangents"))
    tangents_ok = (
        not tangent_errors
        and (
            not require_tangents
            or (
                normal_mapped_primitives > 0
                and normal_mapped_primitives == textured_primitives
            )
        )
    )
    _check(
        checks,
        "glb-tangents",
        tangents_ok,
        (
            "; ".join(tangent_errors[:8])
            or "%d normal-mapped primitive(s)" % normal_mapped_primitives
        ),
    )

    morph_errors = []
    mesh_target_counts = {}
    maximum_morph_targets = 0
    named_morph_targets = 0
    all_morph_targets_named = True
    total_morph_targets = 0
    morph_normal_targets = 0
    morph_tangent_targets = 0
    for mesh_index, mesh in enumerate(meshes):
        if not isinstance(mesh, dict):
            continue
        primitives = mesh.get("primitives", [])
        if not isinstance(primitives, list) or not primitives:
            continue
        primitive_target_counts = []
        expected_target_semantics = None
        for primitive_index, primitive in enumerate(primitives):
            if not isinstance(primitive, dict):
                continue
            targets = primitive.get("targets", [])
            if not isinstance(targets, list):
                morph_errors.append(
                    "mesh %d primitive %d targets must be an array"
                    % (mesh_index, primitive_index)
                )
                continue
            primitive_target_counts.append(len(targets))
            semantics = []
            attributes = primitive.get("attributes", {})
            position_index = attributes.get("POSITION") if isinstance(attributes, dict) else None
            vertex_count = (
                accessors[position_index].get("count", 0)
                if _glb_integer(position_index)
                and 0 <= position_index < len(accessors)
                and isinstance(accessors[position_index], dict)
                else 0
            )
            for target_index, target in enumerate(targets):
                if not isinstance(target, dict) or not target:
                    morph_errors.append(
                        "mesh %d primitive %d target %d is empty or invalid"
                        % (mesh_index, primitive_index, target_index)
                    )
                    semantics.append(frozenset())
                    continue
                keys = frozenset(target)
                semantics.append(keys)
                total_morph_targets += 1
                morph_normal_targets += int("NORMAL" in keys)
                morph_tangent_targets += int("TANGENT" in keys)
                if not keys <= {"POSITION", "NORMAL", "TANGENT"}:
                    morph_errors.append(
                        "mesh %d primitive %d target %d has invalid semantics"
                        % (mesh_index, primitive_index, target_index)
                    )
                target_delta_rows = {}
                for semantic, accessor_index in target.items():
                    try:
                        if (
                            not _glb_integer(accessor_index)
                            or accessor_index < 0
                            or accessor_index >= len(accessors)
                        ):
                            raise ValueError("%s delta has an invalid accessor" % semantic)
                        target_accessor = accessors[accessor_index]
                        target_rows = accessor_cache.get(accessor_index) or _glb_accessor_values(
                            document, binary, accessor_index
                        )
                        if (
                            not isinstance(target_accessor, dict)
                            or target_accessor.get("componentType") != 5126
                            or target_accessor.get("type") != "VEC3"
                        ):
                            raise ValueError("%s delta must be floating-point VEC3" % semantic)
                        if len(target_rows) != vertex_count:
                            raise ValueError("%s delta count must match POSITION" % semantic)
                        if any(
                            not math.isfinite(float(value))
                            for row in target_rows
                            for value in row
                        ):
                            raise ValueError("%s deltas contain non-finite values" % semantic)
                        if semantic == "POSITION":
                            if "min" not in target_accessor or "max" not in target_accessor:
                                raise ValueError("POSITION deltas require min/max bounds")
                            if not any(
                                not math.isclose(float(value), 0.0, abs_tol=1e-8)
                                for row in target_rows
                                for value in row
                            ):
                                raise ValueError("POSITION morph target is entirely zero")
                        elif not any(
                            not math.isclose(float(value), 0.0, abs_tol=1e-8)
                            for row in target_rows
                            for value in row
                        ):
                            raise ValueError("%s morph target is entirely zero" % semantic)
                        target_delta_rows[semantic] = target_rows
                    except (IndexError, KeyError, TypeError, ValueError, struct.error) as exc:
                        morph_errors.append(
                            "mesh %d primitive %d target %d: %s"
                            % (mesh_index, primitive_index, target_index, exc)
                        )
                if {"NORMAL", "TANGENT"} <= set(target_delta_rows):
                    try:
                        normal_index = attributes.get("NORMAL")
                        tangent_index = attributes.get("TANGENT")
                        base_normals = accessor_cache.get(normal_index) or _glb_accessor_values(
                            document, binary, normal_index
                        )
                        base_tangents = accessor_cache.get(tangent_index) or _glb_accessor_values(
                            document, binary, tangent_index
                        )
                        normal_deltas = target_delta_rows["NORMAL"]
                        tangent_deltas = target_delta_rows["TANGENT"]
                        if not (
                            len(base_normals)
                            == len(base_tangents)
                            == len(normal_deltas)
                            == len(tangent_deltas)
                            == vertex_count
                        ):
                            raise ValueError("morph frame counts do not match base attributes")
                        for base_normal, base_tangent, normal_delta, tangent_delta in zip(
                            base_normals, base_tangents, normal_deltas, tangent_deltas
                        ):
                            changed_normal = [
                                float(value) + float(delta)
                                for value, delta in zip(base_normal, normal_delta)
                            ]
                            changed_tangent = [
                                float(value) + float(delta)
                                for value, delta in zip(base_tangent[:3], tangent_delta)
                            ]
                            normal_length = math.sqrt(sum(value * value for value in changed_normal))
                            tangent_length = math.sqrt(sum(value * value for value in changed_tangent))
                            if normal_length < 1e-6 or tangent_length < 1e-6:
                                raise ValueError("morph frame produces a degenerate normal or tangent")
                            dot = sum(
                                normal_value * tangent_value
                                for normal_value, tangent_value in zip(
                                    changed_normal, changed_tangent
                                )
                            ) / (normal_length * tangent_length)
                            if not math.isclose(dot, 0.0, abs_tol=1e-3):
                                raise ValueError("morphed tangents must remain orthogonal to normals")
                    except (IndexError, KeyError, TypeError, ValueError, struct.error) as exc:
                        morph_errors.append(
                            "mesh %d primitive %d target %d: %s"
                            % (mesh_index, primitive_index, target_index, exc)
                        )
            if expected_target_semantics is None:
                expected_target_semantics = semantics
            elif semantics != expected_target_semantics:
                morph_errors.append(
                    "mesh %d primitives have inconsistent morph semantics" % mesh_index
                )
        if primitive_target_counts:
            if len(set(primitive_target_counts)) != 1:
                morph_errors.append(
                    "mesh %d primitives have inconsistent morph target counts" % mesh_index
                )
            target_count = min(primitive_target_counts)
            mesh_target_counts[mesh_index] = target_count
            maximum_morph_targets = max(maximum_morph_targets, target_count)
            weights = mesh.get("weights")
            if weights is not None and (
                target_count < 1
                or not isinstance(weights, list)
                or len(weights) != target_count
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    for value in weights
                )
            ):
                morph_errors.append("mesh %d has invalid default morph weights" % mesh_index)
            extras = mesh.get("extras", {})
            target_names = extras.get("targetNames") if isinstance(extras, dict) else None
            if target_names is not None:
                if (
                    not isinstance(target_names, list)
                    or len(target_names) != target_count
                    or any(not isinstance(name, str) or not name.strip() for name in target_names)
                    or len(set(target_names)) != len(target_names)
                ):
                    morph_errors.append("mesh %d has invalid morph target names" % mesh_index)
                else:
                    named_morph_targets = max(named_morph_targets, len(target_names))
            elif target_count:
                all_morph_targets_named = False
    for node_index, node in enumerate(nodes):
        if not isinstance(node, dict) or "weights" not in node:
            continue
        mesh_index = node.get("mesh")
        target_count = mesh_target_counts.get(mesh_index, 0)
        weights = node.get("weights")
        if (
            target_count < 1
            or not isinstance(weights, list)
            or len(weights) != target_count
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                for value in weights
            )
        ):
            morph_errors.append("node %d has invalid morph weights" % node_index)
    minimum_morph_targets = _bounded_int(
        requirements, "min_morph_targets", 0, 0, 100_000
    )
    require_named_morph_targets = bool(
        requirements.get("require_named_morph_targets")
    )
    require_morph_normals = bool(requirements.get("require_morph_normals"))
    require_morph_tangents = bool(requirements.get("require_morph_tangents"))
    morphs_ok = (
        not morph_errors
        and maximum_morph_targets >= minimum_morph_targets
        and (
            not require_named_morph_targets
            or (
                named_morph_targets >= maximum_morph_targets
                and maximum_morph_targets > 0
                and all_morph_targets_named
            )
        )
        and (
            not require_morph_normals
            or (total_morph_targets > 0 and morph_normal_targets == total_morph_targets)
        )
        and (
            not require_morph_tangents
            or (total_morph_targets > 0 and morph_tangent_targets == total_morph_targets)
        )
    )
    _check(
        checks,
        "glb-morph-targets",
        morphs_ok,
        (
            "; ".join(morph_errors[:8])
            or "%d maximum target(s), %d named, %d normal, %d tangent"
            % (
                maximum_morph_targets,
                named_morph_targets,
                morph_normal_targets,
                morph_tangent_targets,
            )
        ),
    )

    minimum_joints = _bounded_int(requirements, "min_joints", 0, 0, 100_000)
    skin_ok = (
        not skin_errors
        and maximum_joints >= minimum_joints
        and (minimum_joints == 0 or rigged_primitives > 0)
    )
    _check(
        checks,
        "glb-skinning",
        skin_ok,
        (
            "; ".join(skin_errors[:8])
            or "%d skin(s), %d maximum joints, %d rigged primitive(s)"
            % (len(skins), maximum_joints, rigged_primitives)
        ),
    )

    animation_errors = []
    animations = document.get("animations", [])
    if not isinstance(animations, list):
        animation_errors.append("animations must be an array")
        animations = []
    joint_targets = set().union(*joint_sets) if joint_sets else set()
    animation_names = set()
    named_animations = 0
    skeletal_animations = set()
    morph_animations = set()
    animation_durations = {}
    for animation_index, animation in enumerate(animations):
        if not isinstance(animation, dict):
            animation_errors.append("animation %d is not an object" % animation_index)
            continue
        name = animation.get("name")
        if name is not None:
            if not isinstance(name, str) or not name.strip():
                animation_errors.append("animation %d has an invalid name" % animation_index)
            elif name in animation_names:
                animation_errors.append("animation name %r is duplicated" % name)
            else:
                animation_names.add(name)
                named_animations += 1
        samplers = animation.get("samplers", [])
        channels = animation.get("channels", [])
        if not isinstance(samplers, list) or not samplers or not isinstance(channels, list) or not channels:
            animation_errors.append("animation %d has no samplers/channels" % animation_index)
            continue
        channel_targets = set()
        animation_duration = 0.0
        for channel_index, channel in enumerate(channels):
            try:
                sampler_index = channel["sampler"]
                target = channel["target"]
                if not _glb_integer(sampler_index) or sampler_index < 0 or sampler_index >= len(samplers):
                    raise ValueError("invalid sampler")
                if not isinstance(target, dict):
                    raise ValueError("invalid target")
                target_node = target.get("node")
                target_path = target.get("path")
                if not _glb_integer(target_node) or target_node < 0 or target_node >= len(nodes):
                    raise ValueError("invalid target node")
                if target_path not in {"translation", "rotation", "scale", "weights"}:
                    raise ValueError("invalid target path")
                target_key = (target_node, target_path)
                if target_key in channel_targets:
                    raise ValueError("duplicate node/path target")
                channel_targets.add(target_key)
                sampler = samplers[sampler_index]
                if not isinstance(sampler, dict):
                    raise ValueError("invalid sampler")
                input_index = sampler.get("input")
                output_index = sampler.get("output")
                if (
                    not _glb_integer(input_index)
                    or input_index < 0
                    or input_index >= len(accessors)
                    or not _glb_integer(output_index)
                    or output_index < 0
                    or output_index >= len(accessors)
                ):
                    raise ValueError("animation sampler has invalid accessors")
                interpolation = sampler.get("interpolation", "LINEAR")
                if interpolation not in {"LINEAR", "STEP", "CUBICSPLINE"}:
                    raise ValueError("invalid interpolation")
                input_accessor = accessors[input_index]
                output_accessor = accessors[output_index]
                input_rows = accessor_cache.get(input_index) or _glb_accessor_values(
                    document, binary, input_index
                )
                output_rows = accessor_cache.get(output_index) or _glb_accessor_values(
                    document, binary, output_index
                )
                if input_accessor.get("type") != "SCALAR" or input_accessor.get("componentType") != 5126:
                    raise ValueError("animation input must be floating-point SCALAR")
                times = [float(row[0]) for row in input_rows]
                if not times or times[0] < 0.0 or any(
                    not math.isfinite(value) for value in times
                ) or any(right <= left for left, right in zip(times, times[1:])):
                    raise ValueError("animation input times must be finite and increasing")
                animation_duration = max(animation_duration, times[-1])
                multiplier = 3 if interpolation == "CUBICSPLINE" else 1
                expected_type = {
                    "translation": "VEC3",
                    "rotation": "VEC4",
                    "scale": "VEC3",
                    "weights": "SCALAR",
                }[target_path]
                output_multiplier = multiplier
                if target_path == "weights":
                    target_node_value = nodes[target_node]
                    if not isinstance(target_node_value, dict):
                        raise ValueError("weight animation target node is invalid")
                    target_mesh = target_node_value.get("mesh")
                    target_count = mesh_target_counts.get(target_mesh, 0)
                    if target_count < 1:
                        raise ValueError("weight animation target has no morph targets")
                    output_multiplier *= target_count
                if len(output_rows) != len(input_rows) * output_multiplier:
                    raise ValueError("animation input/output counts do not match")
                if output_accessor.get("type") != expected_type:
                    raise ValueError("animation output has the wrong type")
                if output_accessor.get("componentType") != 5126:
                    raise ValueError("animation output must use floating-point values")
                if any(
                    not math.isfinite(float(value))
                    for row in output_rows
                    for value in row
                ):
                    raise ValueError("animation output contains non-finite values")
                if target_path == "rotation":
                    value_rows = output_rows[1::3] if interpolation == "CUBICSPLINE" else output_rows
                    if any(
                        not math.isclose(
                            math.sqrt(sum(float(value) ** 2 for value in row)),
                            1.0,
                            rel_tol=1e-4,
                            abs_tol=1e-4,
                        )
                        for row in value_rows
                    ):
                        raise ValueError("animation rotations must be unit quaternions")
                if target_path == "weights":
                    morph_animations.add(animation_index)
                elif target_node in joint_targets:
                    skeletal_animations.add(animation_index)
            except (IndexError, KeyError, TypeError, ValueError, struct.error) as exc:
                animation_errors.append(
                    "animation %d channel %d: %s" % (animation_index, channel_index, exc)
                )
        if isinstance(name, str) and name.strip() and name in animation_names:
            animation_durations[name] = animation_duration
    minimum_animations = _bounded_int(requirements, "min_animations", 0, 0, 100_000)
    minimum_skeletal_animations = _bounded_int(
        requirements, "min_skeletal_animations", 0, 0, 100_000
    )
    minimum_morph_animations = _bounded_int(
        requirements, "min_morph_animations", 0, 0, 100_000
    )
    require_named_animations = bool(requirements.get("require_named_animations"))
    animation_ok = (
        not animation_errors
        and len(animations) >= minimum_animations
        and len(skeletal_animations) >= minimum_skeletal_animations
        and len(morph_animations) >= minimum_morph_animations
        and (
            not require_named_animations
            or (named_animations == len(animations) and named_animations > 0)
        )
    )
    _check(
        checks,
        "glb-animations",
        animation_ok,
        (
            "; ".join(animation_errors[:8])
            or "%d animation(s), %d skeletal, %d morph, %d named"
            % (
                len(animations),
                len(skeletal_animations),
                len(morph_animations),
                named_animations,
            )
        ),
    )

    sequence_errors = []
    extras = document.get("extras", {})
    if extras is None:
        extras = {}
    if not isinstance(extras, dict):
        sequence_errors.append("document extras must be an object for animation metadata")
        extras = {}
    clip_metadata = extras.get("animationClips", [])
    if clip_metadata is None:
        clip_metadata = []
    if not isinstance(clip_metadata, list):
        sequence_errors.append("animationClips must be an array")
        clip_metadata = []
    clip_indices = set()
    clip_names = set()
    for clip_index, clip in enumerate(clip_metadata):
        if not isinstance(clip, dict):
            sequence_errors.append("animation clip %d is not an object" % clip_index)
            continue
        index = clip.get("index")
        clip_name = clip.get("name")
        duration = clip.get("duration")
        if (
            not _glb_integer(index)
            or index < 0
            or index >= len(animations)
            or index in clip_indices
        ):
            sequence_errors.append("animation clip %d has an invalid index" % clip_index)
            continue
        expected_name = animations[index].get("name") if isinstance(animations[index], dict) else None
        valid_clip_name = not (
            not isinstance(clip_name, str)
            or not clip_name.strip()
            or clip_name != expected_name
            or clip_name in clip_names
        )
        if not valid_clip_name:
            sequence_errors.append("animation clip %d has an invalid name" % clip_index)
        expected_duration = (
            animation_durations.get(clip_name, -1.0) if valid_clip_name else -1.0
        )
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(float(duration))
            or float(duration) <= 0.0
            or not math.isclose(
                float(duration), expected_duration, abs_tol=1e-4
            )
        ):
            sequence_errors.append("animation clip %d has an invalid duration" % clip_index)
        clip_indices.add(index)
        if valid_clip_name:
            clip_names.add(clip_name)
    sequences = extras.get("animationSequences", [])
    if sequences is None:
        sequences = []
    if not isinstance(sequences, list):
        sequence_errors.append("animationSequences must be an array")
        sequences = []
    sequence_names = set()
    sequenced_clips = set()
    for sequence_index, sequence in enumerate(sequences):
        if not isinstance(sequence, dict):
            sequence_errors.append("animation sequence %d is not an object" % sequence_index)
            continue
        sequence_name = sequence.get("name")
        clips = sequence.get("clips")
        transitions = sequence.get("transitions")
        loop = sequence.get("loop")
        if (
            not isinstance(sequence_name, str)
            or not sequence_name.strip()
            or sequence_name in sequence_names
        ):
            sequence_errors.append("animation sequence %d has an invalid name" % sequence_index)
        else:
            sequence_names.add(sequence_name)
        if (
            not isinstance(clips, list)
            or len(clips) < 2
            or any(not isinstance(clip, str) or clip not in animation_names for clip in clips)
        ):
            sequence_errors.append("animation sequence %d references invalid clips" % sequence_index)
            clips = []
        else:
            sequenced_clips.update(clips)
        if (
            not isinstance(transitions, list)
            or len(transitions) != max(0, len(clips) - 1)
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0.0 <= float(value) <= 10.0
                for value in transitions
            )
        ):
            sequence_errors.append("animation sequence %d has invalid transitions" % sequence_index)
        if type(loop) is not bool:
            sequence_errors.append("animation sequence %d has an invalid loop flag" % sequence_index)
    minimum_sequences = _bounded_int(
        requirements, "min_animation_sequences", 0, 0, 100_000
    )
    require_clip_metadata = bool(requirements.get("require_animation_clip_metadata"))
    required_animation_clips = set(
        _string_list(requirements, "required_animation_clips")
    )
    missing_sequence_clips = sorted(required_animation_clips - sequenced_clips)
    if missing_sequence_clips:
        sequence_errors.append(
            "required clips are not sequenced: %s" % ", ".join(missing_sequence_clips)
        )
    sequences_ok = (
        not sequence_errors
        and len(sequences) >= minimum_sequences
        and (
            not require_clip_metadata
            or (
                len(clip_metadata) == len(animations)
                and clip_indices == set(range(len(animations)))
            )
        )
    )
    _check(
        checks,
        "glb-animation-sequences",
        sequences_ok,
        (
            "; ".join(sequence_errors[:8])
            or "%d sequence(s), %d clip metadata record(s), %d sequenced clip(s)"
            % (len(sequences), len(clip_metadata), len(sequenced_clips))
        ),
    )

    if requirements.get("no_external_dependencies"):
        external = []
        for index, buffer in enumerate(buffers if isinstance(buffers, list) else []):
            if isinstance(buffer, dict) and "uri" in buffer:
                external.append("buffer %d" % index)
        images = document.get("images", [])
        if not isinstance(images, list):
            images = [{"uri": "invalid images collection"}]
        for index, image in enumerate(images):
            if isinstance(image, dict) and "uri" in image:
                external.append("image %d" % index)
        _check(
            checks,
            "glb-no-external-dependencies",
            not external,
            "external references: %s" % (", ".join(external) or "none"),
        )
    searchable = json.dumps(document, ensure_ascii=True, sort_keys=True)
    for needle in _string_list(requirements, "required_text"):
        _check(
            checks,
            "glb-required-text",
            needle.casefold() in searchable.casefold(),
            "contains %r" % needle,
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
    elif recipe == "glb":
        _validate_glb(path, requirements, checks)
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

    # required_text at the bundle level: each needle must appear in at least one
    # of the bundle's files. Previously the bundle recipe never evaluated
    # required_text (only the single-file recipes did, and children do not
    # inherit it), so an absent required string produced a false PASS.
    needles = _string_list(requirements, "required_text")
    if needles:
        corpus_parts = []
        for _name, candidate in declared:
            try:
                corpus_parts.append(_read_text(candidate))
            except (OSError, UnicodeDecodeError, ValueError):
                continue
        corpus = "\n".join(corpus_parts)
        for needle in needles:
            _check(checks, "bundle-required-text", needle in corpus,
                   "text %r present in bundle" % needle)

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
