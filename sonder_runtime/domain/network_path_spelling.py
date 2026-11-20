"""Lexical detection of Windows UNC, device and NT-namespace path spellings.

On Windows a ``stat``, ``lstat``, ``realpath`` or ``Path.resolve`` on
``\\\\host\\share\\x`` (or ``//host/share/x``, ``\\\\?\\UNC\\...``,
``\\\\.\\pipe\\...``, ``\\\\?\\GLOBALROOT\\...``, ``\\\\.\\NUL``,
``\\??\\C:\\...``) opens an SMB or device access before any containment check
can refuse it, and an SMB connection can leak the user's NTLM credentials.
A policy check must therefore decide on the spelling alone, before any
filesystem call.

These rules mirror ``file_ops._reject_network_path`` for callers that may not
import the filesystem adapter (the restricted tool lane, the speculation
prefetcher, the argv guard): an extended-length local drive path
(``\\\\?\\D:\\x`` or ``//?/D:/x``) is not a network spelling; every other
``\\\\`` / ``//`` / ``\\??\\`` prefix is.

Pure functions over ``str``: no I/O, no state, safe from any thread and on
every OS (the rules are the same on POSIX, where such a value is still refused).
"""
from __future__ import annotations

import re

REFUSAL = "UNC, device or network spelling"

_EXTENDED_LOCAL = re.compile(r"(?:\\\\\?\\|//\?/)[A-Za-z]:[\\/]")
# A backslash UNC, device or NT-namespace prefix anywhere inside a token
# (``-I\\host\share``, ``@\\.\pipe\x``); an embedded extended-length local
# drive (``\\?\C:\``) is not one.
_EMBEDDED = re.compile(r"\\\\(?!\?\\[A-Za-z]:\\)[^\\]+\\[^\\]+|\\\?\?\\")


def is_network_spelling(text: str) -> bool:
    """Whether ``text`` as a whole path is a UNC, device or NT-namespace spelling."""
    value = str(text).strip()
    if _EXTENDED_LOCAL.match(value):
        return False
    return value.replace("/", "\\").startswith(("\\\\", "\\??\\"))


def contains_network_spelling(token: str) -> bool:
    """Whether a command-line token is, or embeds, a UNC, device or NT-namespace spelling.

    Forward-slash forms count only at the start of the token: ``a//b/c`` is
    an ordinary relative path.
    """
    text = str(token)
    return is_network_spelling(text) or bool(_EMBEDDED.search(text))
