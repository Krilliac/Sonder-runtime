"""Lexical symbol-path checks (pure; containment lives in adapters).

``lexical_symbol_dir`` refuses anything a debugger could turn into egress or
a path-injection: symbol-server syntax (``srv*``, ``symsrv*``, ``cache*``),
``*`` and ``;`` separators, quotes and control characters, URL schemes, UNC
and device paths (``\\\\server``, ``\\\\?\\``, ``\\\\.\\``), ``..`` segments
and relative paths. The adapter ``contain_symbol_dir`` then checks
containment, ``is_dir`` and mapped network drives.

``lexical_store`` validates an operator-configured store (from config, never
from a tool call): an ``https://`` URL on the default port, or a UNC share.
``build_cdb_symbol_path`` assembles ``cache*<dir>;<dirs>`` and, only with
network consent, ``srv*<cache>*<store>`` entries.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath

from ..common.errors import InvalidInput


MS_SYMBOL_SERVER = "https://msdl.microsoft.com/download/symbols"
DEBUGINFOD_BY_DISTRO = {
    "ubuntu": "https://debuginfod.ubuntu.com",
    "debian": "https://debuginfod.debian.net",
    "fedora": "https://debuginfod.fedoraproject.org",
    "arch": "https://debuginfod.archlinux.org",
}
MAX_SYMBOL_DIRS = 8
MAX_STORES = 4
MAX_PATH_CHARS = 1024

_FORBIDDEN_CHARS = re.compile(r"[*;\"'`|<>\x00-\x1f\x7f]")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]{1,}:(?:/|\\|$)|^[A-Za-z][A-Za-z0-9+.-]{2,}:")
_SERVER_PREFIX_RE = re.compile(r"^(?:srv|symsrv|cache)(?:\*|$)", re.IGNORECASE)
_UNC_SHARE_RE = re.compile(r"^\\\\(?P<host>[A-Za-z0-9._-]{1,253})\\(?P<share>[A-Za-z0-9 ._$-]{1,80})"
                           r"(?:\\[A-Za-z0-9 ._$-]{1,120}){0,8}\\?$")
_HTTPS_RE = re.compile(r"^https://(?P<host>[A-Za-z0-9.-]{1,253})(?::443)?(?P<path>/[A-Za-z0-9._~/-]{0,400})?$")


class SymbolPathRejected(InvalidInput):
    code = "SYMBOL_PATH_REJECTED"


class SymbolStoreRejected(InvalidInput):
    code = "SYMBOL_STORE_REJECTED"


def _is_windows(system: str) -> bool:
    return str(system or "").lower().startswith("win")


def lexical_symbol_dir(path_str: str, *, system: str) -> str:
    """Normalized absolute directory string, or ``SymbolPathRejected``."""
    text = str(path_str or "")
    if not text or len(text) > MAX_PATH_CHARS:
        raise SymbolPathRejected("symbol dir is empty or too long")
    if text != text.strip():
        raise SymbolPathRejected("symbol dir has surrounding whitespace")
    if _FORBIDDEN_CHARS.search(text):
        raise SymbolPathRejected("symbol dir contains * ; quotes or control characters")
    if _SERVER_PREFIX_RE.match(text):
        raise SymbolPathRejected("symbol-server syntax is not a directory")
    if text.startswith(("\\\\", "//")):
        raise SymbolPathRejected("UNC and device paths are refused")
    windows = _is_windows(system)
    drive_colon = bool(re.match(r"^[A-Za-z]:[\\/]", text))
    if _SCHEME_RE.match(text) and not (windows and drive_colon):
        raise SymbolPathRejected("URL schemes are refused")
    if windows:
        path = PureWindowsPath(text)
        if not (path.drive and len(path.drive) == 2 and path.drive[1] == ":" and path.root):
            raise SymbolPathRejected("symbol dir must be an absolute drive path")
        if ":" in text[2:]:
            raise SymbolPathRejected("alternate data streams and extra colons are refused")
    else:
        if "\\" in text:
            raise SymbolPathRejected("backslashes are refused on POSIX")
        path = PurePosixPath(text)
        if not path.is_absolute():
            raise SymbolPathRejected("symbol dir must be absolute")
        if ":" in text:
            raise SymbolPathRejected("colons are refused in POSIX symbol dirs")
    if any(part == ".." for part in path.parts):
        raise SymbolPathRejected("'..' segments are refused")
    return str(path)


def lexical_store(entry: str, *, system: str) -> str:
    """An operator symbol store: ``https://host[/path]`` or ``\\\\host\\share``."""
    text = str(entry or "").strip()
    if not text or len(text) > 512 or _FORBIDDEN_CHARS.search(text):
        raise SymbolStoreRejected("symbol store is empty, too long or has forbidden characters")
    if text.lower().startswith("https://"):
        match = _HTTPS_RE.match(text)
        if match is None or ".." in text:
            raise SymbolStoreRejected("https store must be https://host[/path] on port 443")
        return text.rstrip("/")
    if text.startswith("\\\\"):
        if text.startswith(("\\\\?\\", "\\\\.\\")) or ".." in text:
            raise SymbolStoreRejected("device paths are not stores")
        if _UNC_SHARE_RE.match(text) is None:
            raise SymbolStoreRejected("UNC store must be \\\\host\\share[\\path]")
        return text.rstrip("\\")
    raise SymbolStoreRejected("a store is an https URL or a UNC share")


def build_cdb_symbol_path(cache_dir: str, local_dirs, stores=(), *, network: bool) -> str:
    """``cache*<cache>;<dir>...`` plus ``srv*<cache>*<store>`` only when ``network``.

    ``cache_dir`` and ``local_dirs`` must already be lexically validated and
    contained; stores must come from ``lexical_store``. The Microsoft server
    is the first store when ``network`` is true.
    """
    dirs = [str(item) for item in local_dirs][:MAX_SYMBOL_DIRS + 1]
    if len(dirs) > MAX_SYMBOL_DIRS:
        raise SymbolPathRejected("at most %d symbol dirs" % MAX_SYMBOL_DIRS)
    for item in [cache_dir, *dirs]:
        if _FORBIDDEN_CHARS.search(item) or not item:
            raise SymbolPathRejected("symbol path component contains * ; or quotes")
    parts = ["cache*%s" % cache_dir, *dirs]
    if network:
        chosen = [MS_SYMBOL_SERVER, *list(stores)[:MAX_STORES]]
        if len(list(stores)) > MAX_STORES:
            raise SymbolStoreRejected("at most %d operator stores" % MAX_STORES)
        for store in chosen:
            if ";" in store or "*" in store:
                raise SymbolStoreRejected("store contains a separator")
            parts.append("srv*%s*%s" % (cache_dir, store))
    return ";".join(parts)


def store_display(store: str) -> str:
    """A short, stable display id for a store (used in receipts and digests)."""
    return str(store or "").strip()[:200]


__all__ = [
    "DEBUGINFOD_BY_DISTRO", "MAX_STORES", "MAX_SYMBOL_DIRS", "MS_SYMBOL_SERVER", "SymbolPathRejected",
    "SymbolStoreRejected", "build_cdb_symbol_path", "lexical_store", "lexical_symbol_dir", "store_display",
]
