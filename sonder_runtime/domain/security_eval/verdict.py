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

from ..common.bounded_json import JsonBoundsExceeded, container_extent
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


# Failed decodes are charged the characters their bracket walk covered. One scan may spend this
# many times the reply's length plus the floor; past that it is refused. A failed opener in prose
# (``[10-13]``, ``{"k": os.environ}``) walks a few characters, so realistic replies stay far below
# the budget, while nested unclosed openers that each walk to the end of the text exhaust it.
_FAILED_SCAN_FACTOR = 4
_FAILED_SCAN_FLOOR = 65_536
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


def _objects(text: str, opener: str) -> Iterator[tuple[int, Any]]:
    """Yield ``(start, value)`` for each decodable JSON value starting at ``opener``, left to right.

    A container the nesting bound refuses raises ``_JsonScanRefused`` where it stands: values
    yielded before it keep their precedence, and nothing after it or nested inside it is ever
    yielded, because an unbounded decoder might have read the refused container instead. Failed
    decodes are charged the length of their bracket walk; once the charges pass
    ``_FAILED_SCAN_FACTOR`` times the text length plus ``_FAILED_SCAN_FLOOR`` the scan is
    refused, so total work stays linear in the text.
    """
    index = text.find(opener)
    budget = _FAILED_SCAN_FACTOR * len(text) + _FAILED_SCAN_FLOOR
    while index >= 0:
        if not _plausible(text, index, opener):
            index = text.find(opener, index + 1)
            continue
        try:
            _deepest, walked_to = container_extent(text, index)
            value, end = _DECODER.raw_decode(text, index)
        except (JsonBoundsExceeded, RecursionError):
            raise _JsonScanRefused("the reply holds JSON refused by the nesting bound") from None
        except ValueError:
            # The decoder stops inside the walked span, so the walk is what this attempt cost.
            budget -= walked_to - index
            if budget < 0:
                raise _JsonScanRefused("the reply exceeds the JSON scan work limit") from None
            index = text.find(opener, index + 1)
            continue
        yield index, value
        index = text.find(opener, end)


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
    for _start, value in _objects(cleaned, "["):
        found = _usable(value, max_items)
        if found is not None:
            return found
    for _start, value in _objects(cleaned, "{"):
        if isinstance(value, dict):
            found = _usable(value.get("hypotheses"), max_items)
            if found is not None:
                return found
    raise VerdictParseError("the reply holds no hypothesis list")
