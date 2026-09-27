"""Profiler CSV exports detected by their header row.

- WPA "CPU Usage (Sampled)" tables (``Module``/``Function`` or ``Stack`` with
  ``Weight``/``% Weight``/``Count``) -> sampled self weights (``wpa_csv``).
- PIX timing-capture event lists (``Name`` + ``Duration`` [+ ``Start``,
  ``Thread``]) -> zones, frames and spikes (``pix_csv``).
- Superluminal function lists (``Inclusive`` + ``Exclusive`` columns)
  -> self/total per function (``superluminal_csv``).
- Tracy csvexport headers are handed to ``tracy_csv``.

The WPA/PIX/Superluminal header names follow the vendors' documentation and
are flagged for replacement with real exports during Windows live validation.
Every physical line is capped at 64 KiB (the ``field_size_limit`` bound), a
``csv.Error`` stops the read, and unknown headers raise ``ProfileFormatUnknown``.
"""
from __future__ import annotations

import re
from typing import Callable

from sonder_runtime.domain.profiling.folded import (
    DEFAULT_FRAME_NAMES,
    FoldedProfile,
    FrameInfo,
    ProjectPredicate,
    digest_folded,
    digest_zones,
    fold_add,
)
from sonder_runtime.domain.profiling.model import (
    DEFAULT_LIMITS,
    MAX_TOP_FUNCTIONS,
    CaptureMetadata,
    CsvStats,
    ProfileDigest,
    ProfileFormatUnknown,
    ProfileFunction,
    ProfileLimits,
    ProfileParseError,
    ProfileSourceKind,
    WorkBudget,
    bounded_csv_rows,
    clip_name,
    finite_number,
    header_key,
    percent,
)
from sonder_runtime.domain.profiling.tracy_csv import parse_tracy_csv, tracy_csv_mode

_UNIT_RE = re.compile(r"\((ns|us|µs|μs|ms|s)\)|\[(ns|us|µs|μs|ms|s)\]")
_THOUSANDS_RE = re.compile(r"^\d{1,3}(?:,\d{3})+(?:\.\d+)?$")
_NS_PER = {"ns": 1.0, "us": 1e3, "µs": 1e3, "μs": 1e3, "ms": 1e6, "s": 1e9}
_STACK_SEPARATORS = (";", " <- ", "/")

DIALECTS = ("auto", "wpa", "pix", "superluminal", "tracy")


def _unit(header: str, default: str = "ms") -> str:
    match = _UNIT_RE.search(header.lower())
    if not match:
        return default
    return match.group(1) or match.group(2)


def _cell_number(value: str) -> float | None:
    text = value.strip().rstrip("%").strip()
    if _THOUSANDS_RE.match(text):
        text = text.replace(",", "")
    number = finite_number(text)
    return number if number is not None and number >= 0 else None


def _find(keys: list[str], *candidates: str, prefix: bool = False) -> int | None:
    for candidate in candidates:
        for position, key in enumerate(keys):
            if key == candidate or (prefix and key.startswith(candidate)):
                return position
    return None


def sniff_profile_csv(header: list[str]) -> str | None:
    """The ProfileSourceKind value a header row belongs to, or None."""
    if tracy_csv_mode(header):
        return ProfileSourceKind.TRACY_CSV.value
    keys = [header_key(cell) for cell in header]
    has = lambda *names, prefix=False: _find(keys, *names, prefix=prefix) is not None  # noqa: E731
    if has("inclusive", prefix=True) and has("exclusive", prefix=True) and has("function", "name", "symbol"):
        return ProfileSourceKind.SUPERLUMINAL_CSV.value
    if (has("weight", "% weight", prefix=True) or has("count", "sample count")) \
            and has("function", "stack", "symbol"):
        return ProfileSourceKind.WPA_CSV.value
    if has("duration", prefix=True) and has("name", "event name", "event", "marker"):
        return ProfileSourceKind.PIX_CSV.value
    return None


def parse_profile_csv(
    text: str,
    dialect_hint: str = "auto",
    *,
    frame_zone: str = "",
    frame_budget_ms: float | None = None,
    thread: str = "",
    top_n: int = MAX_TOP_FUNCTIONS,
    project: ProjectPredicate | None = None,
    limits: ProfileLimits = DEFAULT_LIMITS,
    clock: Callable[[], float] | None = None,
) -> ProfileDigest:
    budget = WorkBudget(limits.max_seconds, clock)
    stats = CsvStats()
    rows = bounded_csv_rows(text, stats, max_line_chars=limits.max_line_chars,
                            max_rows=limits.max_lines, budget=budget)
    header = None
    for row in rows:
        if any(cell.strip() for cell in row):
            header = row
            break
        if stats.rows > 16:
            break
    if header is None:
        if stats.oversize:
            raise ProfileFormatUnknown("CSV header line is over 64 KiB")
        raise ProfileFormatUnknown("empty or unreadable CSV")
    kind = sniff_profile_csv(header)
    hint = str(dialect_hint or "auto").lower()
    forced = {"wpa": ProfileSourceKind.WPA_CSV.value, "pix": ProfileSourceKind.PIX_CSV.value,
              "superluminal": ProfileSourceKind.SUPERLUMINAL_CSV.value,
              "tracy": ProfileSourceKind.TRACY_CSV.value}.get(hint)
    if forced is not None and kind is not None and kind != forced:
        raise ProfileFormatUnknown("CSV header does not match the %s dialect" % hint)
    if kind is None:
        raise ProfileFormatUnknown("unknown profiler CSV header",
                                   hint="expected WPA, PIX, Superluminal or Tracy csvexport columns")
    if kind == ProfileSourceKind.TRACY_CSV.value:
        return parse_tracy_csv(text, frame_zone=frame_zone, frame_budget_ms=frame_budget_ms,
                               thread=thread, top_n=top_n, project=project, limits=limits,
                               clock=clock)
    keys = [header_key(cell) for cell in header]
    if kind == ProfileSourceKind.SUPERLUMINAL_CSV.value:
        return _superluminal(rows, keys, stats, top_n=top_n, project=project, limits=limits)
    if kind == ProfileSourceKind.WPA_CSV.value:
        return _wpa(rows, keys, stats, top_n=top_n, project=project, limits=limits)
    return _pix(rows, keys, stats, frame_zone=frame_zone, frame_budget_ms=frame_budget_ms,
                thread=thread, top_n=top_n, project=project, limits=limits, budget=budget)


def _csv_notes(stats: CsvStats, skipped: int) -> list[str]:
    notes = []
    if stats.oversize:
        notes.append("%d CSV lines over 64 KiB skipped" % stats.oversize)
    if stats.error:
        notes.append("CSV read stopped: %s" % stats.error)
    if skipped:
        notes.append("%d malformed rows skipped" % skipped)
    return notes


def _cell(row: list[str], position: int | None) -> str:
    if position is None or position >= len(row):
        return ""
    return row[position]


def _superluminal(rows, keys, stats: CsvStats, *, top_n, project, limits) -> ProfileDigest:
    name_col = _find(keys, "function", "name", "symbol")
    module_col = _find(keys, "module", "image")
    file_col = _find(keys, "file", "source file")
    incl_col = _find(keys, "inclusive", prefix=True)
    excl_col = _find(keys, "exclusive", prefix=True)
    calls_col = _find(keys, "count", "calls", "hit count")
    percent_incl = "%" in keys[incl_col]
    percent_excl = "%" in keys[excl_col]
    # Prefer time columns over percentage columns when both exist.
    for position, key in enumerate(keys):
        if key.startswith("inclusive") and "%" not in key:
            incl_col, percent_incl = position, False
        if key.startswith("exclusive") and "%" not in key:
            excl_col, percent_excl = position, False
    unit = _unit(keys[excl_col]) if not percent_excl else "%"
    scale = _NS_PER.get(unit, 1.0) if unit != "%" else 1000.0
    merged: dict[tuple[str, str], list] = {}
    skipped = 0
    for row in rows:
        name = clip_name(_cell(row, name_col))
        inclusive = _cell_number(_cell(row, incl_col))
        exclusive = _cell_number(_cell(row, excl_col))
        if inclusive is None or exclusive is None or not _cell(row, name_col).strip():
            skipped += 1
            continue
        module = clip_name(_cell(row, module_col)) if module_col is not None else ""
        key = (name, module)
        if key not in merged and len(merged) >= limits.max_functions:
            stats.truncated = True
            continue
        calls = _cell_number(_cell(row, calls_col)) if calls_col is not None else None
        entry = merged.setdefault(key, [0.0, 0.0, None, _cell(row, file_col)[:1024] or None])
        entry[0] += exclusive
        entry[1] += inclusive
        if calls is not None:
            entry[2] = (entry[2] or 0) + int(calls)
    if not merged:
        raise ProfileParseError("Superluminal CSV has no readable function rows")
    grand = max(sum(value[0] for value in merged.values()),
                max(value[1] for value in merged.values()))
    if percent_excl:
        grand = 100.0

    def function(key) -> ProfileFunction:
        name, module = key
        exclusive, inclusive, calls, file = merged[key]
        return ProfileFunction(
            name=name, module=module or None, file=file,
            self_pct=percent(exclusive, grand), total_pct=percent(inclusive, grand),
            self_value=int(round(exclusive * scale)), total_value=int(round(inclusive * scale)),
            calls=calls,
            in_project=bool(project(name, module or None, file)) if project is not None else False,
        )

    top_n = max(1, min(int(top_n), MAX_TOP_FUNCTIONS))
    by_self = sorted(merged, key=lambda k: (-merged[k][0], k))[:top_n]
    by_total = sorted(merged, key=lambda k: (-merged[k][1], k))[:top_n]
    return ProfileDigest(
        source_kind=ProfileSourceKind.SUPERLUMINAL_CSV.value,
        metric="cpu_time" if unit != "%" else "cpu_share",
        unit="ns" if unit != "%" else "0.001%",
        metadata=CaptureMetadata(tool="superluminal", event="sampled cpu"),
        top_self=tuple(function(k) for k in by_self if merged[k][0] > 0),
        top_total=tuple(function(k) for k in by_total),
        notes=tuple(["Superluminal CSV columns follow vendor docs (unverified export)"]
                    + _csv_notes(stats, skipped)),
        truncated=stats.truncated,
    )


def _split_stack(value: str) -> list[str]:
    for separator in _STACK_SEPARATORS:
        if separator in value:
            return [frame.strip() for frame in value.split(separator) if frame.strip()]
    return [value.strip()] if value.strip() else []


def _wpa(rows, keys, stats: CsvStats, *, top_n, project, limits) -> ProfileDigest:
    stack_col = _find(keys, "stack")
    function_col = _find(keys, "function", "symbol")
    module_col = _find(keys, "module", "image")
    weight_col = None
    for position, key in enumerate(keys):
        if key.startswith("weight"):
            weight_col = position
            break
    unit = "ms"
    if weight_col is not None:
        unit = _unit(keys[weight_col])
        scale, out_unit, metric = _NS_PER.get(unit, 1e6), "ns", "cpu_time"
    else:
        weight_col = _find(keys, "count", "sample count")
        scale, out_unit, metric = 1.0, "samples", "samples"
        if weight_col is None:
            weight_col = _find(keys, "% weight")
            scale, out_unit, metric = 1000.0, "0.001%", "cpu_share"
    folded = FoldedProfile.from_limits(limits)
    skipped = 0
    for row in rows:
        weight = _cell_number(_cell(row, weight_col))
        if weight is None:
            skipped += 1
            continue
        module = clip_name(_cell(row, module_col)) if module_col is not None else None
        frames: list[str] = []
        if stack_col is not None:
            frames = _split_stack(_cell(row, stack_col))
        if len(frames) <= 1 and function_col is not None and _cell(row, function_col).strip():
            frames = [_cell(row, function_col).strip()]
        if not frames:
            skipped += 1
            continue
        leaf = clip_name(frames[-1])
        info = {leaf: FrameInfo(module=module)} if module else None
        fold_add(folded, frames, int(round(weight * scale)), info=info)
    if not folded.stacks:
        raise ProfileParseError("WPA CSV has no readable weighted rows")
    notes = ["WPA CSV columns follow vendor docs (unverified export)"] + _csv_notes(stats, skipped)
    return digest_folded(folded, metric=metric, unit=out_unit,
                         source_kind=ProfileSourceKind.WPA_CSV.value,
                         metadata=CaptureMetadata(tool="wpa", event="CPU Usage (Sampled)"),
                         top_n=top_n, project=project, notes=notes, truncated=stats.truncated)


def _pix(rows, keys, stats: CsvStats, *, frame_zone, frame_budget_ms, thread, top_n, project,
         limits, budget) -> ProfileDigest:
    name_col = _find(keys, "name", "event name", "event", "marker")
    duration_col = _find(keys, "duration", prefix=True)
    start_col = _find(keys, "start", "start time", "begin", "timestamp", prefix=True)
    thread_col = _find(keys, "thread", "thread id", "tid", "queue", prefix=True)
    duration_scale = _NS_PER.get(_unit(keys[duration_col]), 1e6)
    start_scale = _NS_PER.get(_unit(keys[start_col]), 1e6) if start_col is not None else 1.0
    zones_by_thread: dict[str, list[tuple[int, int, str]]] = {}
    skipped = count = 0
    cursor: dict[str, int] = {}
    for row in rows:
        name = _cell(row, name_col).strip()
        duration = _cell_number(_cell(row, duration_col))
        if not name or duration is None:
            skipped += 1
            continue
        if count >= limits.max_events:
            stats.truncated = True
            break
        key = clip_name(_cell(row, thread_col) or "0")[:64] if thread_col is not None else "0"
        if key not in zones_by_thread and len(zones_by_thread) >= 4096:
            skipped += 1
            continue
        start = _cell_number(_cell(row, start_col)) if start_col is not None else None
        duration_ns = int(round(duration * duration_scale))
        if start is None:
            # No start column: lay events end to end so each stays a root zone.
            start_ns = cursor.get(key, 0)
            cursor[key] = start_ns + duration_ns
        else:
            start_ns = int(round(start * start_scale))
        zones_by_thread.setdefault(key, []).append((start_ns, duration_ns, clip_name(name)))
        count += 1
    if not count:
        raise ProfileParseError("PIX CSV has no readable timing rows")
    notes = ["PIX CSV columns follow vendor docs (unverified export)"] + _csv_notes(stats, skipped)
    if start_col is None:
        notes.append("no start column: zone nesting and self time are unavailable")
    return digest_zones(
        zones_by_thread, source_kind=ProfileSourceKind.PIX_CSV.value,
        metadata=CaptureMetadata(tool="pix", event="timing capture", sample_count=count,
                                 threads=len(zones_by_thread)),
        frame_names=DEFAULT_FRAME_NAMES, frame_zone=frame_zone, frame_budget_ms=frame_budget_ms,
        thread=thread, top_n=top_n, project=project, limits=limits, budget=budget,
        notes=notes, truncated=stats.truncated,
    )
