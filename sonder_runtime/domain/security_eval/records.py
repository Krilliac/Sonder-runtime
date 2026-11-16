"""Labelled hypothesis records and their validation (pure, no I/O)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

from .errors import DatasetError, DatasetRecordError

LABELS = ("CONFIRMED", "REFUTED", "UNCERTAIN")
STAGES = ("r1", "new")
MAX_RANGE_SPAN = 400
MAX_FIELD_CHARS = {"hid": 128, "repo_root": 1024, "file": 1024, "category": 64,
                   "hypothesis": 4000, "severity": 32, "reason": 2000, "label": 16, "stage": 8}
_RANGE_RE = re.compile(r"^\s*(\d{1,9})\s*(?:-\s*(\d{1,9}))?\s*$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True)
class LineRange:
    """An inclusive, 1-based line range."""

    start: int
    end: int

    @property
    def span(self) -> int:
        return self.end - self.start + 1

    def as_text(self) -> str:
        return str(self.start) if self.start == self.end else "%d-%d" % (self.start, self.end)


@dataclass(frozen=True)
class HypothesisRecord:
    hid: str
    repo_root: str
    file: str
    lines: LineRange
    category: str
    hypothesis: str
    label: str
    severity: str
    stage: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"hid": self.hid, "repo_root": self.repo_root, "file": self.file,
                "lines": self.lines.as_text(), "category": self.category,
                "hypothesis": self.hypothesis, "label": self.label, "severity": self.severity,
                "stage": self.stage, "reason": self.reason}


def parse_line_range(value: object, *, max_span: int = MAX_RANGE_SPAN, line_no: int = 0) -> LineRange:
    """Accept ``"a-b"``, ``"a"``, an int or ``[a, b]``; ``1 <= a <= b`` and span <= ``max_span``."""
    if isinstance(value, bool):
        raise DatasetRecordError("lines must be a range, not a boolean", line_no)
    if isinstance(value, int):
        start = end = value
    elif isinstance(value, (list, tuple)) and len(value) == 2 \
            and all(isinstance(item, int) and not isinstance(item, bool) for item in value):
        start, end = value
    elif isinstance(value, str):
        match = _RANGE_RE.match(value)
        if not match:
            raise DatasetRecordError("lines %r is not 'a-b' or 'a'" % value[:40], line_no)
        start = int(match.group(1))
        end = int(match.group(2) or match.group(1))
    else:
        raise DatasetRecordError("lines must be 'a-b', 'a', an integer or [a, b]", line_no)
    if start < 1 or end < start:
        raise DatasetRecordError("lines must satisfy 1 <= start <= end", line_no)
    if end - start + 1 > max_span:
        raise DatasetRecordError("lines span more than %d lines" % max_span, line_no)
    return LineRange(start, end)


def check_relative_path(path: object, *, line_no: int = 0) -> str:
    """Return the normalised relative path or raise: no ``..``, absolute, drive, UNC, ADS or NUL."""
    if not isinstance(path, str):
        raise DatasetRecordError("file must be a string", line_no)
    text = path.strip()
    if not text:
        raise DatasetRecordError("file is empty", line_no)
    if "\x00" in text:
        raise DatasetRecordError("file contains a NUL character", line_no)
    if len(text) > MAX_FIELD_CHARS["file"]:
        raise DatasetRecordError("file is too long", line_no)
    text = text.replace("\\", "/")
    if text.startswith("/"):
        raise DatasetRecordError("file must be relative to the repo root", line_no)
    if _DRIVE_RE.match(text) or ":" in text:
        raise DatasetRecordError("file may not carry a drive letter or an alternate data stream", line_no)
    if text.endswith("/"):
        raise DatasetRecordError("file names a directory", line_no)
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts:
        raise DatasetRecordError("file names no file", line_no)
    if any(part == ".." for part in parts):
        raise DatasetRecordError("file may not contain '..'", line_no)
    if any(part != part.rstrip(" .") for part in parts):
        raise DatasetRecordError("file components may not end in a space or a dot", line_no)
    return "/".join(parts)


def _text_field(obj: dict[str, Any], name: str, line_no: int, *, required: bool) -> str:
    value = obj.get(name, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise DatasetRecordError("%s must be a string" % name, line_no)
    value = value.strip()
    if required and not value:
        raise DatasetRecordError("%s is required" % name, line_no)
    if len(value) > MAX_FIELD_CHARS[name]:
        if name == "reason":
            return value[:MAX_FIELD_CHARS[name]]
        raise DatasetRecordError("%s is longer than %d characters" % (name, MAX_FIELD_CHARS[name]), line_no)
    if "\x00" in value:
        raise DatasetRecordError("%s contains a NUL character" % name, line_no)
    return value


def parse_record(obj: object, line_no: int) -> HypothesisRecord:
    """Validate one decoded JSON object; raises ``DatasetRecordError`` carrying ``line_no``."""
    if not isinstance(obj, dict):
        raise DatasetRecordError("a record must be a JSON object", line_no)
    hid = _text_field(obj, "hid", line_no, required=True)
    repo_root = _text_field(obj, "repo_root", line_no, required=True)
    file = check_relative_path(obj.get("file"), line_no=line_no)
    if "lines" not in obj:
        raise DatasetRecordError("lines is required", line_no)
    lines = parse_line_range(obj["lines"], line_no=line_no)
    category = _text_field(obj, "category", line_no, required=True)
    hypothesis = _text_field(obj, "hypothesis", line_no, required=True)
    label = _text_field(obj, "label", line_no, required=True).upper()
    if label not in LABELS:
        raise DatasetRecordError("label must be one of %s" % ", ".join(LABELS), line_no)
    stage = _text_field(obj, "stage", line_no, required=True).lower()
    if stage not in STAGES:
        raise DatasetRecordError("stage must be one of %s" % ", ".join(STAGES), line_no)
    severity = _text_field(obj, "severity", line_no, required=False)
    reason = _text_field(obj, "reason", line_no, required=False)
    return HypothesisRecord(hid=hid, repo_root=repo_root, file=file, lines=lines, category=category,
                            hypothesis=hypothesis, label=label, severity=severity, stage=stage,
                            reason=reason)


def collect_records(lines: Iterable[tuple[int, str]]) -> tuple[list[HypothesisRecord], list[dict[str, Any]]]:
    """Decode ``(line_no, text)`` pairs into ``(records, invalid)``.

    ``invalid`` is a list of ``{"line", "hid"?, "reason"}``. Blank lines are skipped. A repeated
    ``hid`` is invalid (the first one wins). Zero valid records raises ``DatasetError``.
    """
    records: list[HypothesisRecord] = []
    invalid: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_no, text in lines:
        if not text.strip():
            continue
        hid = ""
        try:
            try:
                obj = json.loads(text)
            except ValueError as exc:
                raise DatasetRecordError("not valid JSON (%s)" % exc.__class__.__name__, line_no) from None
            if isinstance(obj, dict) and isinstance(obj.get("hid"), str):
                hid = obj["hid"].strip()[:128]
            record = parse_record(obj, line_no)
            if record.hid in seen:
                raise DatasetRecordError("duplicate hid %r" % record.hid, line_no)
            seen.add(record.hid)
            records.append(record)
        except DatasetRecordError as exc:
            entry = {"line": line_no, "reason": str(exc)}
            if hid:
                entry["hid"] = hid
            invalid.append(entry)
    if not records:
        raise DatasetError("the dataset holds no valid record (%d invalid line(s))" % len(invalid))
    return records, invalid
