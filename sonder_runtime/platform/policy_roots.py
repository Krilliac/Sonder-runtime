"""The one parser for filesystem policy roots.

A policy root widens what the file tools may reach, so every source of one --
``SONDER_FILE_ROOTS``, ``file_roots.local``, a lane's configured roots and
``[state].workspace_roots`` -- goes through :func:`parse_policy_root` before a
``Path`` built from it reaches ``resolve()``, ``stat()`` or a containment check.

The parser is purely lexical: it never touches the filesystem or the network,
so a refused UNC, device or NT-namespace spelling cannot open an SMB connection
(and leak NTLM credentials) on the way to being refused. It lives in the
platform layer so the configuration loader and the filesystem adapter share it
instead of each keeping a copy.

Rules, applied in order:

1. Non-string entries, empty entries, and entries holding a NUL byte or ASCII
   control character are refused.
2. An extended-length local path (``\\\\?\\C:\\x`` or ``//?/C:/x``) has its
   prefix stripped, so containment agrees with ``resolve_path`` whatever a given
   Python version's ``resolve()`` does with the prefix. Every other prefixed or
   network form (``\\\\?\\UNC\\...``, ``\\\\.\\...``, ``\\??\\...``,
   ``GLOBALROOT``, ``Volume{...}``, plain UNC ``\\\\host\\share``, in either
   slash flavour) is refused.
3. ``~`` is expanded and rules 1 (control characters) and 2 run again on the
   result, so a UNC or prefixed home directory cannot turn ``~`` into a network
   or device root. The configuration loader turns this rule off
   (``expand_user=False``), so a ``~`` workspace root there stays relative and
   is refused by rule 4.
4. The result must be absolute. ``C:``, ``C:x``, ``\\x``, a bare ``/`` on
   Windows, and an unexpanded ``%VAR%`` / ``$VAR`` are all relative and refused.
   On Windows, the drive must be an ASCII letter (``[A-Za-z]:``) and no path
   component (outside ``.`` and ``..``) may end in ``.`` or space or hold ``:``
   (trailing dot/space aliases like ``C:\\...`` or ``C:\\ \\`` collapse to
   ``C:\\`` under Win32 normalization, and ADS spellings like ``C:\\a:b`` break
   ``Path.resolve``).
5. ``.`` and ``..`` are collapsed lexically, and a drive root or the filesystem
   root is refused, so ``\\\\?\\C:\\x\\..\\..`` cannot become ``C:\\``.

The returned path is the normalised one: it holds no ``..`` component, so a
later ``resolve()`` can only follow links in components that really exist.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# ``\\?\C:\`` or ``//?/C:/`` (either separator after the drive): the only
# prefixed spelling that names an ordinary local path.
EXTENDED_LOCAL_RE = re.compile(r"(?:\\\\\?\\|//\?/)([A-Za-z]:[\\/])")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:$")


class PolicyRootError(ValueError):
    """A policy-root entry was refused; the message says why, without its text."""


def _check_no_control_chars(text: str, *, expanded: bool = False) -> None:
    prefix = "the expanded entry" if expanded else "the entry"
    if "\0" in text:
        raise PolicyRootError(f"{prefix} contains a NUL byte")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise PolicyRootError(f"{prefix} contains a control character")


def _strip_extended_or_refuse_prefixed(text: str) -> str:
    extended = EXTENDED_LOCAL_RE.match(text)
    if extended:
        return text[extended.start(1):]
    flat = text.replace("/", "\\")
    if flat.startswith(("\\\\", "\\??\\")) or flat == "\\??":
        raise PolicyRootError("a UNC, device or NT-namespace path is not allowed")
    return text


def _check_windows_components(path: Path) -> None:
    if os.name != "nt":
        return
    if not _WINDOWS_DRIVE_RE.fullmatch(path.drive):
        raise PolicyRootError("the entry does not name a local drive letter")
    parts = path.parts[1:] if path.anchor else path.parts
    for part in parts:
        if part in (".", ".."):
            continue
        if part.endswith((".", " ")) or ":" in part:
            raise PolicyRootError(
                "a Windows alias or alternate-data-stream component is not allowed"
            )


def parse_policy_root(raw: object, *, expand_user: bool = True) -> Path:
    """Return the absolute, local, normalised root *raw* names, or raise ``PolicyRootError``.

    ``expand_user=False`` skips rule 3, so a ``~`` entry stays relative and is
    refused; the configuration loader uses it because a configured
    ``[state].workspace_roots`` entry must be written as a literal absolute path.

    Pure and thread-safe: it reads only the home-directory environment variables
    ``Path.expanduser`` consults, and makes no filesystem or network call.
    """
    if not isinstance(raw, str):
        raise PolicyRootError("the entry must be a string")
    text = raw.strip()
    if not text:
        raise PolicyRootError("the entry is empty")
    _check_no_control_chars(text)
    expanded = _strip_extended_or_refuse_prefixed(text)
    if expand_user:
        try:
            expanded = str(Path(expanded).expanduser())
        except RuntimeError:
            raise PolicyRootError("'~' cannot be expanded: no home directory is known") from None
        _check_no_control_chars(expanded, expanded=True)
        expanded = _strip_extended_or_refuse_prefixed(expanded)
    raw_path = Path(expanded)
    if not raw_path.is_absolute():
        raise PolicyRootError("the entry is not absolute")
    _check_windows_components(raw_path)
    normalized = Path(os.path.normpath(expanded))
    if not normalized.is_absolute():
        raise PolicyRootError("the entry is not absolute")
    _check_windows_components(normalized)
    if normalized.parent == normalized or not normalized.name:
        raise PolicyRootError("a drive root or the filesystem root is not allowed")
    return normalized


def try_parse_policy_root(raw: object, *, expand_user: bool = True) -> Path | None:
    """:func:`parse_policy_root`, returning ``None`` for a refused entry."""
    try:
        return parse_policy_root(raw, expand_user=expand_user)
    except PolicyRootError:
        return None
