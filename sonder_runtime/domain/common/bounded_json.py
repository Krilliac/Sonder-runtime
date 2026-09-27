"""Bounded JSON decoding for hostile documents.

Python's ``json`` module recurses once per nesting level and converts
arbitrarily long digit strings, so a small hostile document can raise
``RecursionError`` deep inside the decoder or burn CPU on a 10 MB integer.
Every reader of untrusted JSON (``.ips`` crash reports, Chrome traces,
debugger ``--json`` output) goes through this module instead:

- ``check_depth`` is a linear bracket scan that ignores string contents and
  refuses documents nested deeper than ``max_depth`` before ``json`` sees them;
- ``loads_bounded`` adds a byte cap and maps ``RecursionError`` and the
  int-string-limit ``ValueError`` to ``JsonBoundsExceeded``;
- ``iter_array_objects`` streams the objects of one top-level array (a bare
  array, or the array under ``key`` in a top-level object) from text chunks,
  decoding each element on its own with ``raw_decode``. Elements larger than
  ``max_item_bytes`` are skipped and counted, never buffered in full.

Pure domain module: no I/O. All failures raise ``JsonBoundsExceeded`` (an
``InvalidInput``).
"""
from __future__ import annotations

import codecs
import json
import re
from typing import Iterable, Iterator

from .errors import InvalidInput


DEFAULT_MAX_DEPTH = 64
DEFAULT_MAX_ITEM_BYTES = 65_536

# A JSON string, or an unterminated one running to the end of the text. The
# pattern succeeds at *every* opening quote (unrolled loop, no nested
# quantifier backtracking), so ``sub`` never fails after scanning ahead and
# never restarts inside an unterminated string: ``"\"\"\"...`` stays linear.
_STRING_TO_END = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*(?:"|\\?\Z)', re.S)
_NOT_BRACKET = re.compile(r'[^\[\]{}]+')
_STRUCTURAL = re.compile(r'[\[\]{}",:]')
_IN_STRING = re.compile(r'["\\]')


class JsonBoundsExceeded(InvalidInput):
    """The JSON document is too large, too deep, or not decodable."""


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            return bytes(value).decode("utf-8-sig")
        except UnicodeDecodeError:
            raise JsonBoundsExceeded("JSON document is not valid UTF-8") from None
    raise JsonBoundsExceeded("JSON document must be text or bytes")


def check_depth(text: str, max_depth: int = DEFAULT_MAX_DEPTH) -> int:
    """Return the maximum nesting depth of ``text``; refuse deeper documents.

    String contents are skipped, so brackets inside strings never count.
    Linear in ``len(text)``.
    """
    limit = max(1, int(max_depth))
    # Both passes run inside ``re``: strings are blanked, then everything but
    # brackets is dropped; only the bracket characters are walked in Python.
    brackets = _NOT_BRACKET.sub("", _STRING_TO_END.sub("", _as_text(text)))
    depth = 0
    deepest = 0
    for token in brackets:
        if token in "[{":
            depth += 1
            if depth > limit:
                raise JsonBoundsExceeded("JSON nesting exceeds depth %d" % limit)
            if depth > deepest:
                deepest = depth
        elif depth:
            depth -= 1
    return deepest


def loads_bounded(text: object, *, max_bytes: int, max_depth: int = DEFAULT_MAX_DEPTH):
    """``json.loads`` with a byte cap, a depth pre-scan and bounded failures."""
    if isinstance(text, (bytes, bytearray, memoryview)) and len(text) > int(max_bytes):
        raise JsonBoundsExceeded("JSON document exceeds %d bytes" % int(max_bytes))
    value = _as_text(text)
    if len(value) > int(max_bytes) or len(value.encode("utf-8", "surrogatepass")) > int(max_bytes):
        raise JsonBoundsExceeded("JSON document exceeds %d bytes" % int(max_bytes))
    check_depth(value, max_depth)
    try:
        return json.loads(value)
    except RecursionError:
        raise JsonBoundsExceeded("JSON nesting is too deep to decode") from None
    except ValueError as exc:
        # JSONDecodeError and the int-string conversion limit both land here.
        raise JsonBoundsExceeded("JSON is not decodable: %s" % str(exc)[:120]) from None


class ArrayObjectScan:
    """Iterator over the objects of one JSON array, streamed from chunks.

    Counters (valid after iteration): ``items`` yielded, ``skipped_oversize``
    elements over ``max_item_bytes``, ``skipped_invalid`` elements that were
    not decodable objects, ``truncated`` when ``max_items`` stopped the scan,
    ``found`` when the target array was located, and ``complete`` when its
    closing bracket was reached.
    """

    def __init__(self, chunks: Iterable[object], *, key: str | None = "traceEvents",
                 max_items: int, max_item_bytes: int = DEFAULT_MAX_ITEM_BYTES,
                 max_depth: int = DEFAULT_MAX_DEPTH) -> None:
        self._chunks = chunks
        self._key = key
        self._max_items = max(0, int(max_items))
        self._max_item_bytes = max(2, int(max_item_bytes))
        self._max_depth = max(2, int(max_depth))
        self.items = 0
        self.skipped_oversize = 0
        self.skipped_invalid = 0
        self.truncated = False
        self.found = False
        self.complete = False
        self._iterator: Iterator[dict] | None = None

    def __iter__(self) -> "ArrayObjectScan":
        return self

    def __next__(self) -> dict:
        if self._iterator is None:
            self._iterator = self._scan()
        return next(self._iterator)

    # The scanner is a small state machine over structural characters only;
    # string contents are skipped with one regex search per string.
    def _scan(self) -> Iterator[dict]:
        depth = 0
        in_string = False
        escape = False
        started = False          # first structural character seen
        array_depth = 0          # depth *inside* the target array, 0 = not yet
        expect_key = False       # at depth 1 of the top object, before a key
        key_parts: list[str] | None = None
        last_key = None
        after_colon = False
        element: list[str] | None = None
        element_len = 0
        element_oversize = False
        element_start = 0
        decoder = json.JSONDecoder()
        utf8 = codecs.getincrementaldecoder("utf-8")(errors="replace")
        for raw in self._chunks:
            if isinstance(raw, (bytes, bytearray, memoryview)):
                chunk = utf8.decode(bytes(raw))
            else:
                chunk = _as_text(raw)
            n = len(chunk)
            i = 0
            if element is not None and not element_oversize:
                element_start = 0
            while i < n:
                if escape:
                    escape = False
                    i += 1
                    continue
                if in_string:
                    match = _IN_STRING.search(chunk, i)
                    if match is None:
                        if key_parts is not None:
                            self._add_key(key_parts, chunk[i:])
                        i = n
                        break
                    if key_parts is not None:
                        self._add_key(key_parts, chunk[i:match.start()])
                    if match.group() == "\\":
                        if key_parts is not None:
                            key_parts.append("\\")
                        if match.end() >= n:
                            escape = True
                            i = n
                        else:
                            if key_parts is not None:
                                key_parts.append(chunk[match.end()])
                            i = match.end() + 1
                        continue
                    in_string = False
                    i = match.end()
                    if key_parts is not None:
                        last_key = "".join(key_parts)[:512]
                        key_parts = None
                        expect_key = False
                    continue
                match = _STRUCTURAL.search(chunk, i)
                if match is None:
                    if not started and chunk[i:].strip():
                        raise JsonBoundsExceeded("JSON document does not start with an array or object")
                    i = n
                    break
                ch = match.group()
                pos = match.start()
                if not started:
                    if chunk[i:pos].strip():
                        raise JsonBoundsExceeded("JSON document does not start with an array or object")
                    if ch not in "[{":
                        raise JsonBoundsExceeded("JSON document does not start with an array or object")
                    started = True
                    if ch == "[":
                        # A bare array is accepted for keyed scans too.
                        array_depth = 1
                        self.found = True
                    else:
                        expect_key = True
                    depth = 1
                    i = pos + 1
                    continue
                i = pos + 1
                if ch == '"':
                    in_string = True
                    if depth == 1 and not array_depth and expect_key:
                        key_parts = []
                    continue
                if ch in "[{":
                    depth += 1
                    if not array_depth and depth > self._max_depth:
                        # Inside the target array, deep elements are decoded
                        # (and refused) one at a time by ``_decode``.
                        raise JsonBoundsExceeded("JSON nesting exceeds depth %d" % self._max_depth)
                    if not array_depth and depth == 2 and ch == "[" and after_colon \
                            and self._key is not None and last_key == self._key:
                        array_depth = 2
                        self.found = True
                        after_colon = False
                        continue
                    if array_depth and depth == array_depth + 1:
                        if ch == "{":
                            element = []
                            element_len = 0
                            element_oversize = False
                            element_start = pos
                        else:
                            self.skipped_invalid += 1
                    after_colon = False
                    continue
                if ch in "]}":
                    if array_depth and depth == array_depth + 1 and element is not None and ch == "}":
                        if not element_oversize:
                            piece = chunk[element_start:pos + 1]
                            element_len += len(piece)
                            if element_len > self._max_item_bytes:
                                element_oversize = True
                            else:
                                element.append(piece)
                        if element_oversize:
                            self.skipped_oversize += 1
                        else:
                            value = self._decode("".join(element), decoder)
                            if value is None:
                                self.skipped_invalid += 1
                            else:
                                if self.items >= self._max_items:
                                    self.truncated = True
                                    return
                                self.items += 1
                                yield value
                        element = None
                    depth -= 1
                    if array_depth and depth < array_depth:
                        self.complete = True
                        return
                    if depth <= 0:
                        return
                    continue
                if ch == ",":
                    if depth == 1 and not array_depth:
                        expect_key = True
                        after_colon = False
                    continue
                if ch == ":":
                    if depth == 1 and not array_depth:
                        after_colon = True
                    continue
            if element is not None and not element_oversize:
                piece = chunk[element_start:]
                element_len += len(piece)
                if element_len > self._max_item_bytes:
                    element_oversize = True
                    element = []
                else:
                    element.append(piece)

    @staticmethod
    def _add_key(parts: list[str], text: str) -> None:
        if sum(len(part) for part in parts) < 512:
            parts.append(text[:512])

    def _decode(self, text: str, decoder: json.JSONDecoder):
        try:
            check_depth(text, self._max_depth)
            value, end = decoder.raw_decode(text)
        except (JsonBoundsExceeded, RecursionError, ValueError):
            return None
        if text[end:].strip() or not isinstance(value, dict):
            return None
        return value


def iter_array_objects(chunks: Iterable[object], *, key: str | None = "traceEvents",
                       max_items: int, max_item_bytes: int = DEFAULT_MAX_ITEM_BYTES,
                       max_depth: int = DEFAULT_MAX_DEPTH) -> ArrayObjectScan:
    """Stream the objects of a bare top-level array or of ``{key: [...]}``.

    Returns an ``ArrayObjectScan`` iterator whose counters report skipped and
    truncated elements. Array elements are skipped and counted as invalid;
    scalar elements are ignored. Raises ``JsonBoundsExceeded`` when the document does not start
    with an array or object or nests beyond ``max_depth`` outside elements.
    """
    return ArrayObjectScan(chunks, key=key, max_items=max_items,
                           max_item_bytes=max_item_bytes, max_depth=max_depth)


__all__ = [
    "ArrayObjectScan", "DEFAULT_MAX_DEPTH", "DEFAULT_MAX_ITEM_BYTES",
    "JsonBoundsExceeded", "check_depth", "iter_array_objects", "loads_bounded",
]
