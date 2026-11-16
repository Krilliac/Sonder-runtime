"""Parse a model reply into a verdict (item mode) or a list of hypotheses (chunk mode).

The reply is untrusted text. ``<think>`` blocks are discarded first. A JSON object that carries a
valid ``verdict`` always wins; a bare ``VERDICT: X`` line is accepted only when the reply holds no
JSON object at all, and is flagged ``label_line`` so the report can count it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .errors import DatasetRecordError
from .records import LineRange, parse_line_range

VERDICTS = ("CONFIRMED", "REFUTED", "UNCERTAIN")
MAX_REASON_CHARS = 500
MAX_TEXT_CHARS = 200_000
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```[^\n`]*\n(.*?)```", re.DOTALL)
_LABEL_RE = re.compile(r"^[ \t>*_#-]*VERDICT[ \t*_]*[:=][ \t*_]*(CONFIRMED|REFUTED|UNCERTAIN)\b",
                       re.IGNORECASE | re.MULTILINE)
_DECODER = json.JSONDecoder()


class VerdictParseError(ValueError):
    """The reply holds no usable verdict; the runner records it as UNPARSED."""


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


def _clean(text: str) -> str:
    if not isinstance(text, str):
        raise VerdictParseError("the reply is not text")
    text = text[:MAX_TEXT_CHARS]
    text = _THINK_RE.sub("", text)
    start = text.lower().find("<think>")
    if start >= 0:
        text = text[:start]
    if not text.strip():
        raise VerdictParseError("the reply is empty")
    return text


def _fence_spans(text: str) -> list[tuple[int, int]]:
    return [match.span(1) for match in _FENCE_RE.finditer(text)]


def _objects(text: str, opener: str):
    """Yield ``(start, value)`` for each decodable JSON value starting at ``opener``, left to right."""
    index = text.find(opener)
    while index >= 0:
        try:
            value, end = _DECODER.raw_decode(text, index)
        except ValueError:
            index = text.find(opener, index + 1)
            continue
        yield index, value
        index = text.find(opener, end)


def _reason(value) -> str:
    return value.strip()[:MAX_REASON_CHARS] if isinstance(value, str) else ""


def _confidence(value) -> float | None:
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


def _hypothesis(item) -> ParsedHypothesis | None:
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


def parse_hypotheses(text: str, max_items: int = 50) -> tuple[ParsedHypothesis, ...]:
    """Return the hypotheses a model listed for a source chunk (a JSON array, or ``{"hypotheses": [...]}``)."""
    cleaned = _clean(text)
    candidates = []
    for _start, value in _objects(cleaned, "["):
        candidates.append(value)
    for _start, value in _objects(cleaned, "{"):
        if isinstance(value, dict) and isinstance(value.get("hypotheses"), list):
            candidates.append(value["hypotheses"])
    for value in candidates:
        if isinstance(value, list):
            found = [parsed for parsed in (_hypothesis(item) for item in value) if parsed is not None]
            if found or not value:
                return tuple(found[:max_items])
    raise VerdictParseError("the reply holds no hypothesis list")
