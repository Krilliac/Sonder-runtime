"""Display-safe text: control, bidi, zero-width and ANSI sequences removed.

Ported unchanged from the lookup client's text-cleaning functions (the
token-redaction helpers of the client are not part of this port).
"""
from __future__ import annotations

import re

MAX_SERVER_TEXT = 300

# Bidi controls (U+202A-202E, U+2066-2069) and zero-width characters (U+200B-200F, U+2060, U+FEFF).
_INVISIBLE = ''.join(chr(c) for c in (
    *range(0x200B, 0x2010), *range(0x202A, 0x202F), 0x2060, *range(0x2066, 0x206A), 0xFEFF))
_CONTROL_RE = re.compile('[\x00-\x1f\x7f-\x9f' + _INVISIBLE + ']')
_DISPLAY_UNSAFE_RE = re.compile('[\x00-\x08\x0b-\x1f\x7f-\x9f' + _INVISIBLE + ']')
_ANSI_RE = re.compile(
    '\x1b\\[[0-?]*[ -/]*[@-~]'                      # CSI ... final byte
    '|\x1b\\][^\x07\x1b]*(?:\x07|\x1b\\\\)'         # OSC ... BEL or ST
    '|\x1b[PX^_][^\x1b]*\x1b\\\\'                   # DCS, SOS, PM, APC strings ... ST
    # Other two-character escapes.  An unterminated OSC/DCS lands here too: only its two-byte
    # introducer is removed, so ordinary text after it is never swallowed.
    '|\x1b[ -/]*[0-~]')


def ansi_free(text: object) -> str:
    return _ANSI_RE.sub('', str(text))


def sanitize(text: object, limit: int = MAX_SERVER_TEXT) -> str:
    """Text made safe to show: control, bidi and zero-width characters become
    spaces, whitespace is collapsed and the result is cut to limit characters."""
    return ' '.join(_CONTROL_RE.sub(' ', str(text)).split())[:limit]


def terminal_text(text: object) -> str:
    """Model- or document-derived text made safe to print: ANSI escape sequences, C0/C1 control
    characters (except newline and tab), bidi controls and zero-width characters are removed.
    Newlines are kept and nothing is truncated.  Safe to apply to a streamed fragment: the ESC
    byte that starts a sequence is always removed, so a split sequence cannot act."""
    return _DISPLAY_UNSAFE_RE.sub('', ansi_free(text))


def one_line(text: object) -> str:
    """terminal_text collapsed to a single line (not truncated)."""
    return ' '.join(terminal_text(text).split())
