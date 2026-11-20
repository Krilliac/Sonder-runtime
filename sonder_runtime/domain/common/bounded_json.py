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
from array import array
from bisect import bisect_left
from typing import Iterable, Iterator

from .errors import InvalidInput


DEFAULT_MAX_DEPTH = 64
DEFAULT_MAX_ITEM_BYTES = 65_536

# A JSON string, or an unterminated one running to the end of the text. The
# pattern succeeds at *every* opening quote (unrolled loop, no nested
# quantifier backtracking), so ``sub`` never fails after scanning ahead and
# never restarts inside an unterminated string: ``"\"\"\"...`` stays linear.
# The quantifiers are possessive (Python 3.11+): nothing they take could be
# given back to a match, and ``re`` then keeps no state per escape (a string
# with 36 000 escapes cost 5 MB and 4 ms, now nothing and 0.6 ms).
_STRING_TO_END = re.compile(r'"[^"\\]*+(?:\\.[^"\\]*+)*+(?:"|\\?\Z)', re.S)
_NOT_BRACKET = re.compile(r'[^\[\]{}]+')
_STRUCTURAL = re.compile(r'[\[\]{}",:]')
# What the container walk acts on: brackets and string openers only, so the
# ',' and ':' between them are skipped inside ``re`` rather than visited one
# at a time in Python.
_CONTAINER_TOKEN = re.compile(r'[\[\]{}"]')
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


def check_container_depth(text: str, start: int,
                          max_depth: int = DEFAULT_MAX_DEPTH) -> int:
    """Bound the first container at ``start``, without scanning later values.

    Strings, including unterminated ones, are skipped with the same linear
    matcher as ``check_depth``. Only brackets and string openers are walked in
    Python; no suffix is copied. Malformed syntax remains the decoder's
    responsibility.
    """
    return container_extent(text, start, max_depth)[0]


def container_extent(text: str, start: int,
                     max_depth: int = DEFAULT_MAX_DEPTH) -> tuple[int, int]:
    """``(deepest, end)`` for the container at ``start``; raises like ``check_container_depth``.

    ``end`` is where the walk stopped: just past the bracket that closes the
    container, or ``len(text)`` when it never closes. ``end - start`` is what
    the walk cost in characters, so a caller can charge repeated walks to a
    budget. Linear in ``end - start``.
    """
    deepest, end, _closed = container_walk(text, start, max_depth)
    return deepest, end


def container_walk(text: str, start: int,
                   max_depth: int = DEFAULT_MAX_DEPTH) -> tuple[int, int, bool]:
    """``container_extent`` plus whether the container closes (``end`` alone cannot tell a
    container that never closes from one closed by the last character).

    A container that never closes can never be decoded: a successful decode of
    it reads the same strings and brackets as this walk, up to its closing
    bracket. Raises like ``check_container_depth``; linear in ``end - start``.
    """
    value = _as_text(text)
    if start < 0 or start >= len(value) or value[start] not in "[{":
        raise JsonBoundsExceeded("JSON container does not start at the supplied offset")
    limit = max(1, int(max_depth))
    position, depth, deepest = start, 0, 0
    while (match := _CONTAINER_TOKEN.search(value, position)) is not None:
        position = match.end()
        token = match.group()
        if token == '"':
            string = _STRING_TO_END.match(value, match.start())
            if string is None:
                return deepest, len(value), False
            position = string.end()
        elif token in "[{":
            depth += 1
            if depth > limit:
                raise JsonBoundsExceeded(f"JSON nesting exceeds depth {limit}")
            deepest = max(deepest, depth)
        else:
            depth -= 1
            if depth == 0:
                return deepest, position, True
    return deepest, len(value), False


class ContainerExtents:
    """What ``container_extents`` computed: ``lookup(offset)`` for any ``[`` or ``{`` offset.

    Holds one machine integer per bracket or quote of the text in each of three
    arrays (offsets, depths, ends), so memory is 24 bytes per such character
    and no Python object is kept per opener. Immutable once built; safe to
    share between threads.
    """

    __slots__ = ("_deepest", "_ends", "_offsets")

    def __init__(self, offsets: array, deepest: array, ends: array) -> None:
        self._offsets = offsets
        self._deepest = deepest
        self._ends = ends

    def lookup(self, offset: int) -> tuple[int, int]:
        """``(deepest, end)`` for the opener at ``offset``; ``end`` is -1 when it never closes.

        ``deepest`` and a non-negative ``end`` are what ``container_extent(text, offset)``
        returns whenever it does not raise; for an opener that never closes, ``deepest`` is
        what that walk reaches before the text ends. Raises ``KeyError`` for an offset that
        holds no opener. O(log n).
        """
        slot = bisect_left(self._offsets, offset)
        if slot == len(self._offsets) or self._offsets[slot] != offset or self._ends[slot] == _NO_OPENER:
            raise KeyError(offset)
        return self._deepest[slot], self._ends[slot]

    def items(self) -> Iterator[tuple[int, tuple[int, int]]]:
        """``(offset, lookup(offset))`` for every opener, in text order."""
        for slot, offset in enumerate(self._offsets):
            if self._ends[slot] != _NO_OPENER:
                yield offset, (self._deepest[slot], self._ends[slot])


_NO_OPENER = -2


def container_extents(text: object) -> ContainerExtents:
    """``container_walk`` for every container opener in ``text`` at once, without a depth limit.

    For every ``[`` and ``{``, inside strings or not (a caller may try any
    offset), the returned table gives the ``(deepest, end)`` of
    ``container_walk(text, offset)`` whenever that does not raise, with ``end``
    -1 for an opener that never closes; the caller compares ``deepest`` with
    its own limit. Linear in ``len(text)`` in time and memory (about 24 bytes
    per bracket plus 32 per quote), meant for texts the caller has already
    bounded (``verdict`` cuts replies to 200 000 characters).

    A walk from an opener reads brackets up to the next quote, skips the string
    that quote opens, and goes on right after that string closes. So every walk
    that reaches a given quote continues identically from there, and the
    quote-free stretches between quotes form a forest: each stretch continues
    into the one after its next quote's string, or ends the text. One
    right-to-left bracket match over each stretch, run on top of the state of
    the stretch it continues into and undone before that stretch's other
    continuations are visited, answers every opener in it. Each stretch is
    matched and undone once.

    Where a walk's string ends, for every quote: a quote strictly inside a
    string matched from an earlier quote was escaped there, so a match started
    at it rejoins that match and ends at the same place; every other quote is
    matched with ``_STRING_TO_END``, and those matches overlap by at most one
    character.
    """
    value = _as_text(text)
    size = len(value)
    tokens = array("q", (match.start() for match in _CONTAINER_TOKEN.finditer(value)))
    count = len(tokens)
    quote_slots = array("q", (slot for slot in range(count) if value[tokens[slot]] == '"'))
    quotes = len(quote_slots)
    # Stretch k holds the tokens before quote k (the last stretch: after the last quote) and
    # continues into the stretch after the quote that closes quote k's string. ``final`` stands
    # for the end of the text: a string that runs to it leaves nothing to walk. The forest is
    # kept as first-child and next-sibling links.
    final = quotes + 1
    first_child = array("q", [-1]) * (final + 1)
    next_sibling = array("q", [-1]) * (final + 1)
    matched_to = -1
    closing = 0  # the quote number at ``matched_to - 1``; string ends never decrease
    for number in range(quotes):
        offset = tokens[quote_slots[number]]
        if offset >= matched_to - 1:
            matched_to = _STRING_TO_END.match(value, offset).end()
        if matched_to >= size:
            parent = final
        else:
            while tokens[quote_slots[closing]] < matched_to - 1:
                closing += 1
            parent = closing + 1
        next_sibling[number] = first_child[parent]
        first_child[parent] = number
    next_sibling[quotes] = first_child[final]
    first_child[final] = quotes
    deepest = array("q", [0]) * count
    ends = array("q", [_NO_OPENER]) * count
    # Right-to-left match state: closers not yet matched (nearest last), and for each the
    # deepest container closed before it while it was the nearest. Slot 0 is the end of the
    # text; its depth is the deepest the unclosed rest reaches. ``undo`` holds what each step
    # changed, newest last, each entry ending with its kind.
    closers = array("q", [size])
    inner = array("q", [0])
    undo = array("q")
    # Stretches to visit (>= 0) and undo marks (-1 - mark), newest last.
    pending = array("q")
    # Bound methods and counters instead of attribute and ``len`` lookups in the loops below.
    push_closer, pop_closer = closers.append, closers.pop
    push_inner, pop_inner = inner.append, inner.pop
    log, log_many, unlog = undo.append, undo.extend, undo.pop
    visit, next_visit = pending.append, pending.pop
    waiting = 0  # closers not yet matched (``len(closers) - 1``)
    logged = 0  # ``len(undo)``
    child = first_child[final]
    while child >= 0:
        visit(child)
        child = next_sibling[child]
    while pending:
        stretch = next_visit()
        if stretch < 0:
            mark = -1 - stretch
            while logged > mark:
                kind = unlog()
                if kind == _PUSHED:
                    pop_closer()
                    pop_inner()
                    waiting -= 1
                    logged -= 1
                elif kind == _MATCHED:
                    outer = unlog()
                    nested = unlog()
                    inner[-1] = outer
                    push_closer(unlog())
                    push_inner(nested)
                    waiting += 1
                    logged -= 4
                else:
                    inner[0] = unlog()
                    logged -= 2
            continue
        visit(-1 - logged)
        low = quote_slots[stretch - 1] + 1 if stretch else 0
        high = quote_slots[stretch] if stretch < quotes else count
        for slot in range(high - 1, low - 1, -1):
            offset = tokens[slot]
            if value[offset] in "]}":
                push_closer(offset)
                push_inner(0)
                log(_PUSHED)
                waiting += 1
                logged += 1
            elif waiting:
                close = pop_closer()
                nested = pop_inner() + 1
                outer = inner[-1]
                if nested > outer:
                    inner[-1] = nested
                log_many((close, nested - 1, outer, _MATCHED))
                waiting -= 1
                logged += 4
                deepest[slot] = nested
                ends[slot] = close + 1
            else:
                outer = inner[0]
                inner[0] = outer + 1
                log_many((outer, _UNCLOSED))
                logged += 2
                deepest[slot] = outer + 1
                ends[slot] = -1
        child = first_child[stretch]
        while child >= 0:
            visit(child)
            child = next_sibling[child]
    return ContainerExtents(tokens, deepest, ends)


# ``container_extents`` undo kinds: a closer pushed, an opener matched, an unclosed opener.
_PUSHED, _MATCHED, _UNCLOSED = -1, -2, -3


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
    "ArrayObjectScan", "ContainerExtents", "DEFAULT_MAX_DEPTH", "DEFAULT_MAX_ITEM_BYTES",
    "JsonBoundsExceeded", "check_container_depth", "check_depth", "container_extent", "container_extents",
    "container_walk", "iter_array_objects",
    "loads_bounded",
]
