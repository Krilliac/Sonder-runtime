"""Reading the library metadata an index carries (``schema_version`` and ``sources``).

I/O-free.  The values come from a file the runtime did not write, so they are
untrusted: text is cleaned with ``sanitize`` (control, bidi and zero-width
characters and ANSI sequences removed, whitespace collapsed) and capped, the
number of entries is capped, and a malformed entry is dropped with a warning
instead of failing the lookup.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .models import LibrarySource, SchemaCompat
from .sanitize import ansi_free, sanitize

MAX_SCHEMA_VERSION = 2
MAX_SOURCES = 64
MAX_SUBJECT_CHARS = 120
MAX_SOURCE_CHARS = 60
MAX_LICENCE_CHARS = 60
MAX_RAW_CHARS = 64 * 1024   # a larger ``sources`` value is not parsed at all


@dataclass(frozen=True)
class LibraryMeta:
    schema_version: int
    compat: SchemaCompat
    library: tuple[LibrarySource, ...]
    warnings: tuple[str, ...]


def _clean(value: object, limit: int) -> str:
    return sanitize(ansi_free(value), limit)


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _schema(raw: object) -> tuple[int, SchemaCompat, tuple[str, ...]]:
    if raw is None:
        return 1, SchemaCompat.LEGACY, ()
    text = _clean(raw, 20)
    if not text.isascii() or not text.isdigit() or int(text) < 1:
        return 1, SchemaCompat.INVALID, (f"meta schema_version is not a version number: {text!r}",)
    version = int(text)
    if version > MAX_SCHEMA_VERSION:
        return version, SchemaCompat.NEWER, (
            f"index built by a newer library tool (schema {version}, this runtime reads up to "
            f"{MAX_SCHEMA_VERSION}); its sources metadata is not used",)
    return version, SchemaCompat.LEGACY if version == 1 else SchemaCompat.CURRENT, ()


def _entries(raw: object) -> tuple[list[object], tuple[str, ...]]:
    if raw is None:
        return [], ("meta has no sources row",)
    text = raw if isinstance(raw, str) else str(raw)
    if len(text) > MAX_RAW_CHARS:
        return [], ("meta sources row is too large and was ignored",)
    try:
        loaded = json.loads(text)
    except ValueError:
        return [], ("meta sources row is not valid JSON and was ignored",)
    if not isinstance(loaded, list):
        return [], ("meta sources row is not a list and was ignored",)
    if len(loaded) > MAX_SOURCES:
        return loaded[:MAX_SOURCES], (
            f"meta lists {len(loaded)} sources; only the first {MAX_SOURCES} are used",)
    return loaded, ()


def _entry(item: object) -> LibrarySource | None:
    if not isinstance(item, dict):
        return None
    source = _clean(item.get("source"), MAX_SOURCE_CHARS) if isinstance(item.get("source"), str) else ""
    subject = _clean(item.get("subject"), MAX_SUBJECT_CHARS) if isinstance(item.get("subject"), str) else ""
    passages = _count(item.get("passages"))
    if not source or not subject or passages is None:
        return None
    licence = item.get("licence")
    return LibrarySource(source, subject, passages,
                         _clean(licence, MAX_LICENCE_CHARS) if isinstance(licence, str) else "")


def parse_library_meta(schema_raw: object, sources_raw: object) -> LibraryMeta:
    """The library metadata from the raw ``meta`` values (``None`` for a missing key).

    A version 1 index (no ``schema_version``) has no library entries and no
    warnings: its ``sources`` row, if any, is not read.  A newer schema is
    reported in ``warnings`` and its ``sources`` row is not read either, as
    its shape is not known.  Never raises.
    """
    version, compat, warnings = _schema(schema_raw)
    if compat is not SchemaCompat.CURRENT:
        return LibraryMeta(version, compat, (), warnings)
    items, problems = _entries(sources_raw)
    library = []
    for number, item in enumerate(items):
        entry = _entry(item)
        if entry is None:
            problems += (f"meta sources entry {number} is malformed and was ignored",)
        else:
            library.append(entry)
    return LibraryMeta(version, compat, tuple(library), warnings + problems)
