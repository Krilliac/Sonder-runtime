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

1. Empty entries and entries holding a NUL byte are refused.
2. An extended-length local path (``\\\\?\\C:\\x`` or ``//?/C:/x``) has its
   prefix stripped, so containment agrees with ``resolve_path`` whatever a given
   Python version's ``resolve()`` does with the prefix. Every other prefixed or
   network form (``\\\\?\\UNC\\...``, ``\\\\.\\...``, ``\\??\\...``,
   ``GLOBALROOT``, ``Volume{...}``, plain UNC ``\\\\host\\share``, in either
   slash flavour) is refused.
3. ``~`` is expanded and rule 2 runs again on the result, so a UNC or prefixed
   home directory cannot turn ``~`` into a network or device root. The
   configuration loader turns this rule off (``expand_user=False``), so a
   ``~`` workspace root there stays relative and is refused by rule 4.
4. The result must be absolute. ``C:``, ``C:x``, ``\\x``, a bare ``/`` on
   Windows, and an unexpanded ``%VAR%`` / ``$VAR`` are all relative and refused.
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


class PolicyRootError(ValueError):
    """A policy-root entry was refused; the message says why, without its text."""


def _strip_extended_or_refuse_prefixed(text: str) -> str:
    extended = EXTENDED_LOCAL_RE.match(text)
    if extended:
        return text[extended.start(1):]
    flat = text.replace("/", "\\")
    if flat.startswith(("\\\\", "\\??\\")) or flat == "\\??":
        raise PolicyRootError("a UNC, device or NT-namespace path is not allowed")
    return text


def parse_policy_root(raw: str, *, expand_user: bool = True) -> Path:
    """Return the absolute, local, normalised root *raw* names, or raise ``PolicyRootError``.

    ``expand_user=False`` skips rule 3, so a ``~`` entry stays relative and is
    refused; the configuration loader uses it because a configured
    ``[state].workspace_roots`` entry must be written as a literal absolute path.

    Pure and thread-safe: it reads only the home-directory environment variables
    ``Path.expanduser`` consults, and makes no filesystem or network call.
    """
    text = (raw or "").strip()
    if not text:
        raise PolicyRootError("the entry is empty")
    if "\0" in text:
        raise PolicyRootError("the entry contains a NUL byte")
    expanded = _strip_extended_or_refuse_prefixed(text)
    if expand_user:
        try:
            expanded = str(Path(expanded).expanduser())
        except RuntimeError:
            raise PolicyRootError("'~' cannot be expanded: no home directory is known") from None
        if "\0" in expanded:
            raise PolicyRootError("the expanded entry contains a NUL byte")
        expanded = _strip_extended_or_refuse_prefixed(expanded)
    if not Path(expanded).is_absolute():
        raise PolicyRootError("the entry is not absolute")
    normalized = Path(os.path.normpath(expanded))
    if not normalized.is_absolute():
        raise PolicyRootError("the entry is not absolute")
    if normalized.parent == normalized or not normalized.name:
        raise PolicyRootError("a drive root or the filesystem root is not allowed")
    return normalized


def try_parse_policy_root(raw: str, *, expand_user: bool = True) -> Path | None:
    """:func:`parse_policy_root`, returning ``None`` for a refused entry."""
    try:
        return parse_policy_root(raw, expand_user=expand_user)
    except PolicyRootError:
        return None
