"""Pure helper for detecting bidirectional/invisible control characters in text."""
from __future__ import annotations

_BIDI_CODEPOINTS: dict[int, str] = {
    0x061C: "U+061C",
    0x200B: "U+200B",
    0x200C: "U+200C",
    0x200D: "U+200D",
    0x200E: "U+200E",
    0x200F: "U+200F",
    0x202A: "U+202A",
    0x202B: "U+202B",
    0x202C: "U+202C",
    0x202D: "U+202D",
    0x202E: "U+202E",
    0x2066: "U+2066",
    0x2067: "U+2067",
    0x2068: "U+2068",
    0x2069: "U+2069",
    0xFEFF: "U+FEFF",
}


def detect_bidi_chars(text: object) -> tuple[bool, list[str]]:
    """Return ``(has_bidi_or_hidden, sorted_codepoint_labels)`` for ``text``."""
    raw = str(text or "")
    if not raw:
        return False, []
    found: set[str] = set()
    for ch in raw:
        code = ord(ch)
        if code in _BIDI_CODEPOINTS:
            found.add(_BIDI_CODEPOINTS[code])
        elif code < 0x20 and ch not in ("\n", "\r", "\t"):
            found.add(f"U+{code:04X}")
    labels = sorted(found)
    return bool(labels), labels


__all__ = ["detect_bidi_chars"]
