"""Parse a model reply into a verdict (item mode) or a list of hypotheses (chunk mode).

The reply is untrusted text. ``<think>`` blocks are discarded first. A JSON object that carries a
valid ``verdict`` always wins; a bare ``VERDICT: X`` line is accepted only when the reply holds no
JSON object at all, and is flagged ``label_line`` so the report can count it.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from ..common.bounded_json import (
    DEFAULT_MAX_DEPTH,
    ContainerExtents,
    JsonBoundsExceeded,
    container_extents,
    container_walk,
)
from .errors import DatasetRecordError
from .records import LineRange, parse_line_range

VERDICTS = ("CONFIRMED", "REFUTED", "UNCERTAIN")
MAX_REASON_CHARS = 500
MAX_TEXT_CHARS = 200_000
_FENCE_RE = re.compile(r"```[^\n`]*\n(.*?)```", re.DOTALL)
_LABEL_RE = re.compile(r"^[ \t>*_#-]*VERDICT[ \t*_]*[:=][ \t*_]*(CONFIRMED|REFUTED|UNCERTAIN)\b",
                       re.IGNORECASE | re.MULTILINE)
_THINK_OPEN = re.compile(r"<think>", re.IGNORECASE)
_THINK_CLOSE = re.compile(r"</think>", re.IGNORECASE)
_DECODER = json.JSONDecoder()


class VerdictParseError(ValueError):
    """The reply holds no usable verdict; the runner records it as UNPARSED."""


class _JsonScanRefused(VerdictParseError):
    """A bounded scan could not establish the reply's complete JSON contents."""


@dataclass(frozen=True)
class ParsedVerdict:
    verdict: str
    confidence: float | None
    reason: str
    parse_mode: str


@dataclass(frozen=True)
class ParsedHypothesis:
    file: str
    lines: LineRange | None
    category: str
    hypothesis: str
    severity: str


def _strip_think(text: str) -> str:
    """Drop each ``<think>...</think>`` block (any case); an unterminated ``<think>`` drops the rest.

    One left-to-right pass over the original text (case-insensitive searches, so indices stay
    aligned even for characters whose lowercase form has a different length, such as U+0130):
    every position is examined a bounded number of times, so a run of openers or closers cannot
    go quadratic.
    """
    parts: list[str] = []
    position = 0
    while True:
        opener = _THINK_OPEN.search(text, position)
        if opener is None:
            parts.append(text[position:])
            break
        parts.append(text[position:opener.start()])
        closer = _THINK_CLOSE.search(text, opener.end())
        if closer is None:
            break
        position = closer.end()
    return "".join(parts)


def _clean(text: str) -> str:
    if not isinstance(text, str):
        raise VerdictParseError("the reply is not text")
    text = text[:MAX_TEXT_CHARS]
    text = _strip_think(text)
    if not text.strip():
        raise VerdictParseError("the reply is empty")
    return text


def _fence_spans(text: str) -> list[tuple[int, int]]:
    return [match.span(1) for match in _FENCE_RE.finditer(text)]


# What one scan guarantees. Until the first opener that does not decode, each plausible opener
# is walked on its own (``container_walk``, bounded by the nesting limit) and decoded; those walks
# and decodes cover disjoint values, so a valid reply is walked and decoded once. From the first
# failure on (or the first opener that never closes or is too deep to walk), openers are looked
# up in one linear ``container_extents`` table, built once per reply and shared by its scans, so
# a lookup is O(log n) whatever the nesting. An opener that never closes can
# never decode, so it is skipped for a charge of 1 and never refused for depth. Every other decode
# reads a copy of its opener's extent, which the decoder never reads past; that keeps a failure's
# error position, and the line count ``JSONDecodeError`` makes up to it, inside the extent instead
# of the whole reply. Each failed decode is charged what it read plus a ``_COPY_DIVISOR``-th of
# its copy (``_failed_decode_cost``); once a scan's charges pass ``_FAILED_SCAN_FACTOR`` times the
# reply's length plus the floor, the scan is refused. No single charge exceeds 1 + 1/64 times the
# length, so the failed decodes of a scan read at most about (factor + 1) times the length plus
# the floor and copy at most ``_COPY_DIVISOR`` times that. A failed opener in prose (``[0, len)``,
# ``[10-13]``, ``{"k": os.environ}``) is charged a few characters plus its copy, so realistic
# replies stay far below the budget, while nested closed containers whose decodes each read to
# the end of the reply exhaust it.
_FAILED_SCAN_FACTOR = 4
_FAILED_SCAN_FLOOR = 65_536
_COPY_DIVISOR = 64
_OBJECT_START = frozenset('"}')
_ARRAY_START = frozenset('"-0123456789[{tfn]')
_WHITESPACE = " \t\n\r"


def _plausible(text: str, index: int, opener: str) -> bool:
    """Whether the character after the opener (past JSON whitespace) can begin that value's content.

    Non-standard NaN/Infinity array starts are skipped, which fails closed.
    """
    following = index + 1
    while following < len(text) and text[following] in _WHITESPACE:
        following += 1
    if following >= len(text):
        return False
    return text[following] in (_OBJECT_START if opener == "{" else _ARRAY_START)


def _failed_decode_cost(piece: str, error: ValueError) -> int:
    """The charge for a ``raw_decode`` of ``piece`` (one opener's extent) that raised ``error``.

    The decoder reads up to the error position, except for an unterminated string: that is
    reported where the string starts, after a read to the end of the piece, and is charged the
    whole piece, as is a ``ValueError`` with no position (the int-string conversion limit). Every
    other error, at a quote or not (a missing comma or colon between strings), is charged its
    position. The copy adds a ``_COPY_DIVISOR``-th of its length. (``_objects`` decodes only
    extents that close, which hold no unterminated string; that case is charged in full anyway.)
    """
    read = len(piece)
    if isinstance(error, json.JSONDecodeError) and not error.msg.startswith("Unterminated string"):
        read = error.pos
    return max(1, read) + len(piece) // _COPY_DIVISOR


class _Extents:
    """The ``container_extents`` table of one cleaned reply, built on first use.

    Owned by one parse call and shared by its scans (``parse_hypotheses`` runs two), so the
    table is built at most once per reply. Not thread-safe; it never leaves the call.
    """

    __slots__ = ("_table", "_text")

    def __init__(self, text: str) -> None:
        self._text = text
        self._table: ContainerExtents | None = None

    def lookup(self, index: int) -> tuple[int, int]:
        if self._table is None:
            self._table = container_extents(self._text)
        return self._table.lookup(index)


def _objects(text: str, opener: str, extents: _Extents | None = None) -> Iterator[tuple[int, Any]]:
    """Yield ``(start, value)`` for each decodable JSON value starting at ``opener``, left to right.

    A closed container the nesting bound refuses raises ``_JsonScanRefused`` where it stands:
    values yielded before it keep their precedence, and nothing after it or nested inside it is
    ever yielded, because an unbounded decoder might have read the refused container instead. An
    opener that never closes cannot decode, so it is skipped whatever the nesting it opens.

    Until the first plausible opener that does not decode, each one is walked on its own; the
    walks of decoded values are disjoint. From then on the extents come from ``extents``, one
    linear table for the reply. Each decode reads a copy of the opener's extent, which yields
    what decoding the reply in place would. Failed decodes are charged ``_failed_decode_cost``
    and skipped openers that never close 1; once the charges pass ``_FAILED_SCAN_FACTOR`` times
    the text length plus ``_FAILED_SCAN_FLOOR`` the scan is refused, so total work stays linear
    in the text.
    """
    index = text.find(opener)
    budget = _FAILED_SCAN_FACTOR * len(text) + _FAILED_SCAN_FLOOR
    if extents is None:
        extents = _Extents(text)
    walking = True
    while index >= 0:
        if not _plausible(text, index, opener):
            index = text.find(opener, index + 1)
            continue
        looked_up = not walking
        if walking:
            try:
                deepest, extent_end, closed = container_walk(text, index)
            except JsonBoundsExceeded:
                looked_up = True
            # Past an opener that never closes (its walk read the rest of the reply) or one too
            # deep to walk, the next openers may sit inside it, so they use the table.
            walking = not looked_up and closed
        if looked_up:
            deepest, extent_end = extents.lookup(index)
            closed = extent_end >= 0
        if not closed:
            budget -= 1
            if budget < 0:
                raise _JsonScanRefused("the reply exceeds the JSON scan work limit")
            index = text.find(opener, index + 1)
            continue
        if deepest > DEFAULT_MAX_DEPTH:
            raise _JsonScanRefused("the reply holds JSON refused by the nesting bound")
        piece = text[index:extent_end]
        try:
            value, used = _DECODER.raw_decode(piece, 0)
        except RecursionError:
            raise _JsonScanRefused("the reply holds JSON refused by the nesting bound") from None
        except ValueError as error:
            walking = False
            budget -= _failed_decode_cost(piece, error)
            if budget < 0:
                raise _JsonScanRefused("the reply exceeds the JSON scan work limit") from None
            index = text.find(opener, index + 1)
            continue
        yield index, value
        index = text.find(opener, index + used)


def _reason(value: object) -> str:
    return value.strip()[:MAX_REASON_CHARS] if isinstance(value, str) else ""


def _confidence(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0.0 <= float(value) <= 1.0 else None


def parse_verdict(text: str) -> ParsedVerdict:
    """Return the verdict in ``text`` or raise ``VerdictParseError``."""
    cleaned = _clean(text)
    fences = _fence_spans(cleaned)
    saw_json = False
    for start, value in _objects(cleaned, "{"):
        if not isinstance(value, dict):
            continue
        saw_json = True
        raw = value.get("verdict")
        if isinstance(raw, str) and raw.strip().upper() in VERDICTS:
            fenced = any(low <= start < high for low, high in fences)
            return ParsedVerdict(raw.strip().upper(), _confidence(value.get("confidence")),
                                 _reason(value.get("reason")), "fenced_json" if fenced else "json")
    if saw_json:
        raise VerdictParseError("the reply holds JSON but no valid verdict")
    labels = {match.group(1).upper() for match in _LABEL_RE.finditer(cleaned)}
    if len(labels) == 1:
        return ParsedVerdict(labels.pop(), None, "", "label_line")
    if labels:
        raise VerdictParseError("the reply names conflicting verdict lines")
    raise VerdictParseError("the reply holds no verdict")


def _hypothesis(item: object) -> ParsedHypothesis | None:
    if not isinstance(item, dict):
        return None
    hypothesis = item.get("hypothesis")
    if not isinstance(hypothesis, str) or not hypothesis.strip():
        return None
    file = item.get("file")
    lines = None
    if "lines" in item:
        try:
            lines = parse_line_range(item["lines"])
        except DatasetRecordError:
            lines = None

    def text(name: str, limit: int) -> str:
        value = item.get(name)
        return value.strip()[:limit] if isinstance(value, str) else ""

    return ParsedHypothesis(file=file.strip().replace("\\", "/")[:1024] if isinstance(file, str) else "",
                            lines=lines, category=text("category", 64),
                            hypothesis=hypothesis.strip()[:4000], severity=text("severity", 32))


def _usable(value: object, max_items: int) -> tuple[ParsedHypothesis, ...] | None:
    """The hypotheses of a candidate list: None when it is not a list or holds only unusable items."""
    if not isinstance(value, list):
        return None
    found = [parsed for parsed in (_hypothesis(item) for item in value) if parsed is not None]
    return tuple(found[:max_items]) if found or not value else None


def parse_hypotheses(text: str, max_items: int = 50) -> tuple[ParsedHypothesis, ...]:
    """Return the hypotheses a model listed for a source chunk (a JSON array, or ``{"hypotheses": [...]}``).

    Candidates rank as every array in reply order, then every ``{"hypotheses": [...]}`` object in
    reply order; the first usable one wins. A scan refusal (``_JsonScanRefused``) propagates at the
    point it occurs, so a candidate ranked after a refused container is never returned.
    """
    cleaned = _clean(text)
    extents = _Extents(cleaned)
    for _start, value in _objects(cleaned, "[", extents):
        found = _usable(value, max_items)
        if found is not None:
            return found
    for _start, value in _objects(cleaned, "{", extents):
        if isinstance(value, dict):
            found = _usable(value.get("hypotheses"), max_items)
            if found is not None:
                return found
    raise VerdictParseError("the reply holds no hypothesis list")
