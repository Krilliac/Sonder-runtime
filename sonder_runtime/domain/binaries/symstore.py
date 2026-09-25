"""Symbol-store keys for verified binary identities.

- ``pe_symbol_key``: the symstore/symsrv two-tier layout
  ``name.pdb/<GUID><AGE>/name.pdb`` (GUID as 32 upper-case hex digits without
  dashes, age in upper-case hex without padding).
- ``breakpad_debug_id``: Breakpad's module debug id (GUID hex followed by the
  age in hex), as used by ``dump_syms`` output directories.
- ``elf_debug_path``: the ``.build-id/xx/rest.debug`` path used by
  debug-file directories and debuginfod caches.

Names are validated lexically; nothing here touches the filesystem.
"""
from __future__ import annotations

import re
import struct

from ..common.errors import InvalidInput


_GUID_RE = re.compile(r"^[0-9A-Fa-f]{8}-?[0-9A-Fa-f]{4}-?[0-9A-Fa-f]{4}-?[0-9A-Fa-f]{4}-?[0-9A-Fa-f]{12}$")
_BUILD_ID_RE = re.compile(r"^[0-9a-fA-F]{4,128}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.+\- ]{1,128}$")


def format_guid(raw: bytes) -> str:
    """Canonical upper-case ``XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX`` from 16 GUID bytes."""
    data = bytes(raw)
    if len(data) != 16:
        raise InvalidInput("a GUID is 16 bytes")
    d1, d2, d3 = struct.unpack_from("<IHH", data, 0)
    d4 = data[8:16].hex().upper()
    return "%08X-%04X-%04X-%s-%s" % (d1, d2, d3, d4[:4], d4[4:])


def guid_hex(guid: str) -> str:
    """32 upper-case hex digits, dashes and braces removed; validated."""
    text = str(guid or "").strip().strip("{}")
    if not _GUID_RE.match(text):
        raise InvalidInput("not a GUID")
    return text.replace("-", "").upper()


def _basename(name: str) -> str:
    text = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    if not _NAME_RE.match(text) or text in (".", "..") or text.startswith("."):
        raise InvalidInput("symbol file name is not a plain file name")
    return text


def pe_symbol_key(pdb_name: str, guid: str, age: int) -> str:
    """``name.pdb/GUIDAGE/name.pdb`` for a symstore or symbol server."""
    name = _basename(pdb_name)
    age_value = int(age)
    if age_value < 0 or age_value > 0xFFFFFFFF:
        raise InvalidInput("PDB age out of range")
    return "%s/%s%X/%s" % (name, guid_hex(guid), age_value, name)


def breakpad_debug_id(guid: str, age: int) -> str:
    """Breakpad debug id: GUID hex plus the age in upper-case hex."""
    age_value = int(age)
    if age_value < 0 or age_value > 0xFFFFFFFF:
        raise InvalidInput("PDB age out of range")
    return "%s%X" % (guid_hex(guid), age_value)


def elf_debug_path(build_id: str) -> str:
    """``.build-id/ab/cdef....debug`` for a hex GNU build-id."""
    text = str(build_id or "").strip()
    if not _BUILD_ID_RE.match(text) or len(text) % 2:
        raise InvalidInput("build-id must be an even number of hex digits")
    text = text.lower()
    return ".build-id/%s/%s.debug" % (text[:2], text[2:])


__all__ = ["breakpad_debug_id", "elf_debug_path", "format_guid", "guid_hex", "pe_symbol_key"]
