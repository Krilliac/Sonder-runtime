"""``perf report --stdio`` text: folded call chains and the flat children/self table.

The host-owned templates (lane A/C) run two reports over one perf.data:

- folded: ``--no-children -g folded,0.5,caller,function,percent --sort dso,sym``
  prints an entry line per symbol (``94.52%  hot  [.] integrate_physics(int)``)
  followed by root-first chains (``94.52% _start;main;integrate_physics(int)``);
- flat: ``--children -g none --sort dso,sym`` prints ``Children Self dso [x] sym``.

Weights are converted to samples when the ``# Samples:`` header is present,
otherwise kept in thousandths of a percent. Plain folded text (``a;b 12``)
is accepted too, for captures folded elsewhere.
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Callable

from sonder_runtime.domain.profiling.folded import (
    FoldedProfile,
    FrameInfo,
    ProjectPredicate,
    digest_folded,
    fold_add,
    parse_folded_text,
)
from sonder_runtime.domain.profiling.model import (
    DEFAULT_LIMITS,
    MAX_TOP_FUNCTIONS,
    CaptureMetadata,
    ProfileDigest,
    ProfileFormatUnknown,
    ProfileFunction,
    ProfileLimits,
    ProfileSourceKind,
    WorkBudget,
    clip_name,
    finite_number,
    iter_bounded_lines,
)

_SAMPLES_RE = re.compile(r"^#\s*Samples:\s*([0-9.]{1,16})\s*([KMG]?)\s+of event\s+'([^']{0,128})'")
_EVENT_COUNT_RE = re.compile(r"^#\s*Event count \(approx\.\):\s*(\d{1,20})")
_LOST_RE = re.compile(r"^#\s*Total Lost Samples:\s*(\d{1,20})")
_ENTRY_RE = re.compile(r"^\s*(\d{1,3}\.\d{1,4})%\s+(\S(?:.{0,1024}?\S)?)\s+\[([.a-zA-Z])\]\s+(\S.*)$")
_FLAT_RE = re.compile(
    r"^\s*(\d{1,3}\.\d{1,4})%\s+(\d{1,3}\.\d{1,4})%\s+(\S(?:.{0,1024}?\S)?)\s+\[([.a-zA-Z])\]\s+(\S.*)$")
_CHAIN_PCT_RE = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,4})?)%\s+(\S.{0,65536})$")
_CHAIN_COUNT_RE = re.compile(r"^\s*(\d{1,18})\s+(\S.{0,65536})$")

_MULTIPLIER = {"": 1, "K": 1_000, "M": 1_000_000, "G": 1_000_000_000}
_PCT_SCALE = 1000  # weights in thousandths of a percent when samples are unknown


class _Header:
    __slots__ = ("samples", "event", "event_count", "lost", "seen")

    def __init__(self) -> None:
        self.samples: int | None = None
        self.event = ""
        self.event_count: int | None = None
        self.lost = 0
        self.seen = False

    def feed(self, line: str) -> bool:
        match = _SAMPLES_RE.match(line)
        if match:
            number = finite_number(match.group(1)) or 0.0
            self.samples = int(number * _MULTIPLIER[match.group(2)])
            self.event = match.group(3)
            self.seen = True
            return True
        match = _EVENT_COUNT_RE.match(line)
        if match:
            self.event_count = int(match.group(1))
            self.seen = True
            return True
        match = _LOST_RE.match(line)
        if match:
            self.lost = int(match.group(1))
            self.seen = True
            return True
        return line.startswith("#")

    def metadata(self) -> CaptureMetadata:
        return CaptureMetadata(tool="perf", event=self.event or "samples",
                               sample_count=self.samples)

    def notes(self) -> list[str]:
        return ["perf lost %d samples" % self.lost] if self.lost else []


def _weight(pct: float, header: _Header) -> int:
    if header.samples:
        return int(round(pct / 100.0 * header.samples))
    return int(round(pct * _PCT_SCALE))


def _unit(header: _Header) -> str:
    return "samples" if header.samples else "0.001%"


def parse_perf_folded(
    text: str,
    *,
    limits: ProfileLimits = DEFAULT_LIMITS,
    top_n: int = MAX_TOP_FUNCTIONS,
    project: ProjectPredicate | None = None,
    clock: Callable[[], float] | None = None,
) -> ProfileDigest:
    """``perf report -g folded`` output (or plain folded lines) to a digest."""
    header = _Header()
    folded = FoldedProfile.from_limits(limits)
    budget = WorkBudget(limits.max_seconds, clock)
    entries = 0
    current: tuple[str, str, float] | None = None  # (dso, sym, pct)
    chained = 0.0
    oversize = 0

    def close_entry() -> None:
        if current is None:
            return
        dso, sym, pct = current
        rest = pct - chained
        if rest > 0.01:
            fold_add(folded, (sym,), _weight(rest, header), info={sym: FrameInfo(module=dso)})

    for count, line in enumerate(iter_bounded_lines(text, limits.max_line_chars)):
        if count >= limits.max_lines or not budget.tick():
            folded.truncated = True
            folded.note("perf report text clipped at %d lines" % count)
            break
        if line is None:
            oversize += 1
            continue
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            header.feed(line.strip())
            continue
        entry = _ENTRY_RE.match(line)
        if entry and ";" not in entry.group(2):
            close_entry()
            dso, sym = clip_name(entry.group(2)), clip_name(entry.group(4))
            current = (dso, sym, float(entry.group(1)))
            chained = 0.0
            entries += 1
            continue
        chain = _CHAIN_PCT_RE.match(line)
        is_pct = chain is not None
        if chain is None:
            chain = _CHAIN_COUNT_RE.match(line)
        if chain is None or current is None:
            continue
        number = finite_number(chain.group(1))
        if number is None:
            continue
        frames = [frame for frame in chain.group(2).strip().split(";") if frame]
        if not frames:
            continue
        dso, sym, pct = current
        if frames[-1] != sym:
            frames.append(sym)
        if is_pct:
            chained += number
            weight = _weight(number, header)
        else:
            weight = int(number)
        fold_add(folded, frames, weight, info={sym: FrameInfo(module=dso)})
    close_entry()
    if not entries:
        if header.seen:
            raise ProfileFormatUnknown("perf report text has a header but no symbol entries",
                                       hint="re-run perf report with --stdio --sort dso,sym")
        plain = parse_folded_text(text, limits=limits, clock=clock)
        return digest_folded(plain, metric="samples", unit="samples",
                             source_kind=ProfileSourceKind.PERF_TEXT.value,
                             metadata=CaptureMetadata(tool="folded", event="samples"),
                             top_n=top_n, project=project)
    if header.samples:
        folded.total = max(folded.total, header.samples)
    else:
        folded.total = max(folded.total, 100 * _PCT_SCALE)
    notes = header.notes()
    if oversize:
        notes.append("%d oversize lines skipped" % oversize)
    return digest_folded(folded, metric=header.event or "samples", unit=_unit(header),
                         source_kind=ProfileSourceKind.PERF_TEXT.value,
                         metadata=header.metadata(), top_n=top_n, project=project,
                         notes=notes, engines=("perf",))


def parse_perf_flat(
    text: str,
    *,
    limits: ProfileLimits = DEFAULT_LIMITS,
    top_n: int = MAX_TOP_FUNCTIONS,
    project: ProjectPredicate | None = None,
    clock: Callable[[], float] | None = None,
) -> ProfileDigest:
    """``perf report --children -g none --sort dso,sym`` (or a self-only table)."""
    header = _Header()
    budget = WorkBudget(limits.max_seconds, clock)
    rows: dict[tuple[str, str], tuple[float, float | None]] = {}
    truncated = False
    for count, line in enumerate(iter_bounded_lines(text, limits.max_line_chars)):
        if count >= limits.max_lines or not budget.tick():
            truncated = True
            break
        if line is None or not line.strip():
            continue
        if line.lstrip().startswith("#"):
            header.feed(line.strip())
            continue
        match = _FLAT_RE.match(line)
        if match:
            total_pct, self_pct = float(match.group(1)), float(match.group(2))
            key = (clip_name(match.group(3)), clip_name(match.group(5)))
        else:
            match = _ENTRY_RE.match(line)
            if not match:
                continue
            total_pct, self_pct = None, float(match.group(1))
            key = (clip_name(match.group(2)), clip_name(match.group(4)))
        if key not in rows and len(rows) >= limits.max_functions:
            truncated = True
            continue
        rows[key] = (self_pct, total_pct)
    if not rows:
        raise ProfileFormatUnknown("no perf report symbol rows found",
                                   hint="expected perf report --stdio --sort dso,sym output")
    top_n = max(1, min(int(top_n), MAX_TOP_FUNCTIONS))

    def function(key: tuple[str, str]) -> ProfileFunction:
        dso, sym = key
        self_pct, total_pct = rows[key]
        return ProfileFunction(
            name=sym, module=dso, self_pct=self_pct, total_pct=total_pct,
            self_value=_weight(self_pct, header),
            total_value=None if total_pct is None else _weight(total_pct, header),
            in_project=bool(project(sym, dso, None)) if project is not None else False,
        )

    by_self = sorted(rows, key=lambda k: (-rows[k][0], k))
    has_children = any(value[1] is not None for value in rows.values())
    by_total = (sorted(rows, key=lambda k: (-(rows[k][1] or 0.0), k)) if has_children else [])
    return ProfileDigest(
        source_kind=ProfileSourceKind.PERF_TEXT.value,
        engines=("perf",),
        metric=header.event or "samples",
        unit=_unit(header),
        metadata=header.metadata(),
        top_self=tuple(function(k) for k in by_self[:top_n] if rows[k][0] > 0),
        top_total=tuple(function(k) for k in by_total[:top_n]),
        notes=tuple(header.notes()),
        truncated=truncated,
    )


def merge_perf_digests(folded: ProfileDigest, flat: ProfileDigest | None) -> ProfileDigest:
    """Folded chains give self time and hot paths; the flat table gives children totals."""
    if flat is None or not flat.top_total:
        return folded
    return replace(folded, top_total=flat.top_total,
                   notes=tuple(folded.notes) + tuple(n for n in flat.notes if n not in folded.notes),
                   truncated=folded.truncated or flat.truncated)
