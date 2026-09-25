"""Tracy ``tracy-csvexport`` output.

Two shapes, told apart by the header:

- aggregate (default): ``name,src_file,src_line,total_ns,total_perc,counts,
  mean_ns,min_ns,max_ns,std_ns``. Zone times are inclusive, so this gives
  ``top_total`` and zone spikes (a zone whose max is far above its mean).
- unwrap (``-u``): one row per zone instance,
  ``name,src_file,src_line,ns_since_start,exec_time_ns,thread``. Instances are
  nested per thread for self time and hot paths; the configurable frame zone
  gives FrameStats and frame spikes.

csvexport must match the Tracy version that recorded the capture. Its
version-mismatch message (instead of a CSV header) becomes
``ProfileFormatUnknown`` with a hint.
"""
from __future__ import annotations

import re
from typing import Callable

from sonder_runtime.domain.profiling.folded import (
    DEFAULT_FRAME_NAMES,
    ProjectPredicate,
    digest_zones,
)
from sonder_runtime.domain.profiling.model import (
    DEFAULT_LIMITS,
    MAX_SPIKES,
    MAX_TOP_FUNCTIONS,
    CaptureMetadata,
    CsvStats,
    ProfileDigest,
    ProfileFormatUnknown,
    ProfileFunction,
    ProfileLimits,
    ProfileParseError,
    ProfileSourceKind,
    Spike,
    WorkBudget,
    bounded_csv_rows,
    clip_name,
    finite_number,
    header_key,
    percent,
)

AGGREGATE_COLUMNS = ("name", "src_file", "src_line", "total_ns", "total_perc", "counts",
                     "mean_ns", "min_ns", "max_ns", "std_ns")
UNWRAP_COLUMNS = ("name", "src_file", "src_line", "ns_since_start", "exec_time_ns")
VERSION_HINT = ("tracy-csvexport must be built from the same Tracy version that recorded "
                "the capture; re-export with the matching csvexport")
_MISMATCH_RE = re.compile(r"unsupported|not supported|version|legacy|cannot open|incompatible",
                          re.IGNORECASE)
# A zone is a spike when its slowest instance is this many times its mean and
# at least the 0.5 ms floor the frame detector uses.
ZONE_SPIKE_RATIO = 3.0
ZONE_SPIKE_FLOOR_NS = 500_000


def tracy_csv_mode(header: list[str]) -> str | None:
    """"aggregate", "unwrap" or None for a csvexport header row."""
    keys = [header_key(cell) for cell in header]
    if all(column in keys for column in UNWRAP_COLUMNS):
        return "unwrap"
    if all(column in keys for column in ("name", "total_ns", "counts", "mean_ns", "max_ns")):
        return "aggregate"
    return None


def _number(row: list[str], index: dict[str, int], column: str) -> float | None:
    position = index.get(column)
    if position is None or position >= len(row):
        return None
    value = finite_number(row[position])
    return value if value is not None and value >= 0 else None


def _text_cell(row: list[str], index: dict[str, int], column: str) -> str:
    position = index.get(column)
    if position is None or position >= len(row):
        return ""
    return row[position]


def parse_tracy_csv(
    text: str,
    *,
    frame_zone: str = "",
    frame_budget_ms: float | None = None,
    thread: str = "",
    top_n: int = MAX_TOP_FUNCTIONS,
    project: ProjectPredicate | None = None,
    limits: ProfileLimits = DEFAULT_LIMITS,
    clock: Callable[[], float] | None = None,
) -> ProfileDigest:
    head = text.lstrip("﻿ \t\r\n")[:512]
    first_line = head.split("\n", 1)[0]
    if "," not in first_line and _MISMATCH_RE.search(head):
        raise ProfileFormatUnknown("tracy-csvexport reported: %s" % first_line[:160],
                                   hint=VERSION_HINT)
    budget = WorkBudget(limits.max_seconds, clock)
    stats = CsvStats()
    rows = bounded_csv_rows(text, stats, max_line_chars=limits.max_line_chars,
                            max_rows=limits.max_lines, budget=budget)
    header = next(rows, None)
    mode = tracy_csv_mode(header) if header else None
    if mode is None:
        if _MISMATCH_RE.search(head):
            raise ProfileFormatUnknown("tracy-csvexport output has no CSV header",
                                       hint=VERSION_HINT)
        raise ProfileFormatUnknown("not a tracy-csvexport CSV (unknown header)")
    index = {header_key(cell): position for position, cell in enumerate(header)}
    if mode == "aggregate":
        return _aggregate(rows, index, stats, top_n=top_n, project=project, limits=limits)
    return _unwrap(rows, index, stats, frame_zone=frame_zone, frame_budget_ms=frame_budget_ms,
                   thread=thread, top_n=top_n, project=project, limits=limits, budget=budget)


def _csv_notes(stats: CsvStats) -> list[str]:
    notes = []
    if stats.oversize:
        notes.append("%d CSV lines over 64 KiB skipped" % stats.oversize)
    if stats.error:
        notes.append("CSV read stopped: %s" % stats.error)
    return notes


def _aggregate(rows, index, stats: CsvStats, *, top_n, project, limits) -> ProfileDigest:
    zones: list[tuple[str, str, int | None, float, float | None, int, float, float]] = []
    skipped = 0
    for row in rows:
        name = clip_name(_text_cell(row, index, "name"))
        total_ns = _number(row, index, "total_ns")
        counts = _number(row, index, "counts")
        mean_ns = _number(row, index, "mean_ns")
        max_ns = _number(row, index, "max_ns")
        if total_ns is None or counts is None or mean_ns is None or max_ns is None:
            skipped += 1
            continue
        line = _number(row, index, "src_line")
        if len(zones) >= limits.max_functions:
            stats.truncated = True
            break
        zones.append((name, _text_cell(row, index, "src_file")[:1024],
                      int(line) if line is not None else None, total_ns,
                      _number(row, index, "total_perc"), int(counts), mean_ns, max_ns))
    if not zones:
        raise ProfileParseError("tracy aggregate CSV has no readable zone rows")
    grand = sum(zone[3] for zone in zones)
    top_n = max(1, min(int(top_n), MAX_TOP_FUNCTIONS))
    ordered = sorted(zones, key=lambda zone: (-zone[3], zone[0]))[:top_n]
    top_total = tuple(
        ProfileFunction(
            name=name, file=src_file or None, line=line,
            self_pct=0.0, self_value=0,
            total_pct=perc if perc is not None else percent(total_ns, grand),
            total_value=int(total_ns), calls=counts,
            in_project=bool(project(name, None, src_file or None)) if project is not None else False,
        )
        for name, src_file, line, total_ns, perc, counts, _mean, _max in ordered
    )
    spikes = []
    for name, _file, _line, _total, _perc, counts, mean_ns, max_ns in zones:
        if counts >= 2 and mean_ns > 0 and max_ns >= ZONE_SPIKE_FLOOR_NS \
                and max_ns >= ZONE_SPIKE_RATIO * mean_ns:
            spikes.append(Spike(kind="zone", label=name, start_ns=None, duration_ns=int(max_ns),
                                ratio_to_median=max_ns / mean_ns, thread=None))
    spikes.sort(key=lambda spike: (-spike.ratio_to_median, spike.label))
    notes = ["tracy aggregate CSV has inclusive zone times only (no self time)"]
    if skipped:
        notes.append("%d malformed zone rows skipped" % skipped)
    return ProfileDigest(
        source_kind=ProfileSourceKind.TRACY_CSV.value,
        metric="zone_time",
        unit="ns",
        metadata=CaptureMetadata(tool="tracy-csvexport", event="zones",
                                 sample_count=sum(zone[5] for zone in zones)),
        top_total=top_total,
        spikes=tuple(spikes[:MAX_SPIKES]),
        notes=tuple(notes + _csv_notes(stats)),
        truncated=stats.truncated,
    )


def _unwrap(rows, index, stats: CsvStats, *, frame_zone, frame_budget_ms, thread, top_n,
            project, limits, budget) -> ProfileDigest:
    zones_by_thread: dict[str, list[tuple[int, int, str]]] = {}
    count = skipped = 0
    first = last = None
    for row in rows:
        start = _number(row, index, "ns_since_start")
        duration = _number(row, index, "exec_time_ns")
        if start is None or duration is None:
            skipped += 1
            continue
        if count >= limits.max_events:
            stats.truncated = True
            break
        key = clip_name(_text_cell(row, index, "thread") or "0")[:64]
        if key not in zones_by_thread and len(zones_by_thread) >= 4096:
            skipped += 1
            continue
        name = clip_name(_text_cell(row, index, "name"))
        zones_by_thread.setdefault(key, []).append((int(start), int(duration), name))
        count += 1
        end = int(start) + int(duration)
        first = int(start) if first is None else min(first, int(start))
        last = end if last is None else max(last, end)
    if not count:
        raise ProfileParseError("tracy unwrap CSV has no readable zone rows")
    notes = _csv_notes(stats)
    if skipped:
        notes.append("%d malformed zone rows skipped" % skipped)
    metadata = CaptureMetadata(tool="tracy-csvexport", event="zones", sample_count=count,
                               threads=len(zones_by_thread),
                               duration_ns=(last - first) if first is not None else None)
    return digest_zones(
        zones_by_thread, source_kind=ProfileSourceKind.TRACY_CSV.value, metadata=metadata,
        frame_names=DEFAULT_FRAME_NAMES, frame_zone=frame_zone, frame_budget_ms=frame_budget_ms,
        thread=thread, top_n=top_n, project=project, limits=limits, budget=budget,
        notes=notes, truncated=stats.truncated,
    )
