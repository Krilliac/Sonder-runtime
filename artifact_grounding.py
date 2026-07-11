"""Deterministic, stdlib-only validation recipes for generated artifacts."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import struct
import wave
import zlib
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree


MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_BUNDLE_FILES = 500
MAX_BUNDLE_BYTES = 256 * 1024 * 1024

EXTENSION_RECIPES = {
    ".csv": "csv",
    ".htm": "html",
    ".html": "html",
    ".json": "json",
    ".md": "markdown",
    ".markdown": "markdown",
    ".obj": "obj",
    ".png": "png",
    ".ppm": "ppm",
    ".svg": "svg",
    ".txt": "text",
    ".wav": "wav",
}

RECIPE_ALIASES = {
    "audio": "wav",
    "data": "auto",
    "document": "auto",
    "image": "auto",
    "model": "obj",
    "ui": "ui",
    "web": "ui",
    "writing": "auto",
}

SUPPORTED_RECIPES = {
    "auto",
    "binary",
    "bundle",
    "csv",
    "html",
    "json",
    "markdown",
    "obj",
    "png",
    "ppm",
    "svg",
    "text",
    "ui",
    "wav",
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
    elif recipe == "obj":
        _validate_obj(path, requirements, checks)
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
        if child_recipe in {"html", "svg"} and "no_external_dependencies" in requirements:
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
