"""Command names are ASCII: the one place that decides how typed text folds.

``str.lower()`` and ``re.IGNORECASE`` are Unicode operations, and four code
points outside ASCII come out of them as, or match as, an ASCII letter:

* U+212A KELVIN SIGN: ``lower()`` gives ``k``; IGNORECASE matches ``k``.
* U+017F LATIN SMALL LETTER LONG S: IGNORECASE matches ``s``.
* U+0130 LATIN CAPITAL LETTER I WITH DOT ABOVE: ``lower()`` gives ``i`` plus
  U+0307; IGNORECASE matches ``i``.
* U+0131 LATIN SMALL LETTER DOTLESS I: IGNORECASE matches ``i``.

That list is complete for ``lower()`` and IGNORECASE (a test enumerates every
code point). ``casefold()`` and ``upper()`` reach more (sharp s, the ff/fi/fl/st
ligatures and others), so command text is never passed through either.

Two rules follow, and every command surface uses these helpers for them:

* Command words are folded with ``ascii_lower`` (``slash_head`` for the first
  word of a slash line): the letters A-Z are lowercased and every other
  character stays as typed, so a word holding anything outside ASCII can
  equal no command name.
* A natural-language turn holding a fold character is read twice by the router,
  once with each fold character as its ASCII look-alike (``ascii_reading``) and
  once as a letter with no ASCII meaning (``opaque_reading``). The router
  resolves the turn only when both readings give the same command line.

Stdlib only; no state.
"""
from __future__ import annotations

import string

FOLD_CHARACTERS = "\u212a\u017f\u0130\u0131"

# What each fold character is taken for when it folds, in FOLD_CHARACTERS order.
_ASCII_LOOKALIKES = "KsIi"

# Phoenician letters: category Lo, matched by ``\w``, no case mapping and no
# compatibility decomposition, so no Python text operation gives them an ASCII
# meaning. One per fold character, so a reading can be mapped back.
_OPAQUE_LETTERS = "\U00010900\U00010901\U00010902\U00010903"

_TO_ASCII = str.maketrans(FOLD_CHARACTERS, _ASCII_LOOKALIKES)
_TO_OPAQUE = str.maketrans(FOLD_CHARACTERS, _OPAQUE_LETTERS)
_FROM_OPAQUE = str.maketrans(_OPAQUE_LETTERS, FOLD_CHARACTERS)
_LOWER_ASCII = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def ascii_lower(text: object) -> str:
    """Lowercase the letters A-Z of ``text``; every other character stays as typed."""
    return str(text or "").translate(_LOWER_ASCII)


def slash_head(line: object) -> str:
    """The first word of ``line``, folded the way command names are compared.

    ``"/Work fix it"`` gives ``"/work"``. A first word holding a character
    outside ASCII keeps that character, so it is never a command name. An
    empty line gives ``""``.
    """
    words = str(line or "").split(None, 1)
    return ascii_lower(words[0]) if words else ""


def has_fold_character(text: str) -> bool:
    """Whether ``text`` holds a character that folds into an ASCII letter."""
    return not text.isascii() and any(ch in text for ch in FOLD_CHARACTERS)


def ascii_reading(text: str) -> str:
    """``text`` with each fold character replaced by its ASCII look-alike."""
    return text.translate(_TO_ASCII)


def opaque_reading(text: str) -> str | None:
    """``text`` with each fold character replaced by a letter that cannot fold.

    None when ``text`` already holds one of the stand-in letters, because the
    result could then not be mapped back.
    """
    if any(ch in text for ch in _OPAQUE_LETTERS):
        return None
    return text.translate(_TO_OPAQUE)


def restore_fold_characters(text: str) -> str:
    """Undo ``opaque_reading``: put back the characters that were typed."""
    return text.translate(_FROM_OPAQUE)
