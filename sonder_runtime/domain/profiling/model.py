"""Typed profile digest (``sonder.profile_digest/1``) and its parts.

Pure domain module: no I/O, no environment. Profiler captures are hostile
input, so every string field passes through ``clean_text(value, 240)`` at
construction and every list is capped here, whatever a parser produced.
Wall-clock budgets are checked through an injected clock (``WorkBudget``),
never by reading the clock implicitly inside parsing loops.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import time
from dataclasses import dataclass, field, fields, replace
from enum import Enum
from typing import Any, Callable, Iterator, Literal

from sonder_runtime.domain.diagnostics.model import clean_text


SCHEMA = "sonder.profile_digest/1"
WIRE_TEXT_CHARS = 240
MAX_TOP_FUNCTIONS = 25
MAX_HOT_PATHS = 10
MAX_HOT_PATH_FRAMES = 24
MAX_SPIKES = 10
MAX_ALLOCATIONS = 15
MAX_CONTEXT_SWITCHES = 10
MAX_NOTES = 16
MAX_ENGINES = 4
ELISION = "…"
TRUNCATED_FRAME = "[truncated]"

# Raw frame names are clipped to this before they become dictionary keys, so a
# 10 MB "function name" never multiplies through the aggregation tables.
MAX_RAW_NAME_CHARS = 512

_HEX64 = frozenset("0123456789abcdef")


class ProfileSourceKind(str, Enum):
    PERF_DATA = "perf_data"
    PERF_TEXT = "perf_text"
    CALLGRIND = "callgrind"
    CHROME_TRACE = "chrome_trace"
    TRACY_CAPTURE = "tracy_capture"
    TRACY_CSV = "tracy_csv"
    WPA_CSV = "wpa_csv"
    ETW_ETL = "etw_etl"
    PIX_CSV = "pix_csv"
    SUPERLUMINAL_CSV = "superluminal_csv"
    HEAPTRACK_CAPTURE = "heaptrack_capture"
    HEAPTRACK_TEXT = "heaptrack_text"


# Formats a pure parser in this package reads; the others need a host tool.
PURE_SOURCE_KINDS = frozenset({
    ProfileSourceKind.PERF_TEXT.value,
    ProfileSourceKind.CALLGRIND.value,
    ProfileSourceKind.CHROME_TRACE.value,
    ProfileSourceKind.TRACY_CSV.value,
    ProfileSourceKind.WPA_CSV.value,
    ProfileSourceKind.PIX_CSV.value,
    ProfileSourceKind.SUPERLUMINAL_CSV.value,
    ProfileSourceKind.HEAPTRACK_TEXT.value,
})


class ProfileEngine(str, Enum):
    PURE = "pure"
    PERF = "perf"
    HEAPTRACK_PRINT = "heaptrack_print"
    TRACY_CSVEXPORT = "tracy_csvexport"
    XPERF = "xperf"
    WPAEXPORTER = "wpaexporter"


class ProfileFormatUnknown(ValueError):
    """The input is not a format this reader understands (CAPTURE_FORMAT_UNKNOWN)."""

    code = "CAPTURE_FORMAT_UNKNOWN"

    def __init__(self, message: str, *, hint: str = "") -> None:
        self.message = clean_text(message, WIRE_TEXT_CHARS)
        self.hint = clean_text(hint, WIRE_TEXT_CHARS)
        super().__init__(self.message + (" (%s)" % self.hint if self.hint else ""))


class ProfileParseError(ValueError):
    """The input looked like the format but could not be read (PARSE_FAILED)."""

    code = "PARSE_FAILED"

    def __init__(self, message: str) -> None:
        self.message = clean_text(message, WIRE_TEXT_CHARS)
        super().__init__(self.message)


# --------------------------------------------------------------------------
# Construction helpers


def _text(value: object, limit: int = WIRE_TEXT_CHARS) -> str:
    raw = "" if value is None else str(value)
    # clean_text is linear, but pre-clipping keeps a hostile 64 MiB value from
    # being regex-scanned in full just to keep 240 characters of it.
    return clean_text(raw[: limit * 4 + 64], limit)


def _opt_text(value: object, limit: int = WIRE_TEXT_CHARS) -> str | None:
    if value is None:
        return None
    text = _text(value, limit)
    return text or None


def _int(value: object, *, minimum: int = 0) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return minimum
    return max(minimum, number)


def _opt_int(value: object, *, minimum: int = 0) -> int | None:
    if value is None:
        return None
    return _int(value, minimum=minimum)


def _float(value: object, *, minimum: float = 0.0, ndigits: int = 3) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return minimum
    if not math.isfinite(number):
        return minimum
    return round(max(minimum, number), ndigits)


def _opt_float(value: object, *, ndigits: int = 3) -> float | None:
    if value is None:
        return None
    return _float(value, ndigits=ndigits)


def _set(obj: object, name: str, value: object) -> None:
    object.__setattr__(obj, name, value)


def elide_frames(frames: tuple[str, ...] | list[str], max_frames: int = MAX_HOT_PATH_FRAMES) -> tuple[str, ...]:
    """Root-to-leaf frames capped at ``max_frames`` with a middle ``…`` marker.

    The root side keeps a third of the budget and the leaf side the rest,
    because the leaf end is where the time is spent.
    """
    frames = tuple(frames)
    if len(frames) <= max_frames:
        return frames
    head = max(1, (max_frames - 1) // 3)
    tail = max_frames - 1 - head
    return frames[:head] + (ELISION,) + frames[len(frames) - tail:]


def percent(value: float, total: float) -> float:
    if total <= 0 or not math.isfinite(value) or not math.isfinite(total):
        return 0.0
    return round(min(100.0, max(0.0, 100.0 * value / total)), 3)


# --------------------------------------------------------------------------
# Parts


@dataclass(frozen=True, slots=True)
class ProfileFunction:
    name: str
    module: str | None = None
    file: str | None = None
    line: int | None = None
    self_pct: float = 0.0
    total_pct: float | None = None
    self_value: int = 0
    total_value: int | None = None
    calls: int | None = None
    in_project: bool = False

    def __post_init__(self) -> None:
        _set(self, "name", _text(self.name) or "?")
        _set(self, "module", _opt_text(self.module))
        _set(self, "file", _opt_text(self.file))
        _set(self, "line", _opt_int(self.line))
        _set(self, "self_pct", _float(self.self_pct))
        _set(self, "total_pct", _opt_float(self.total_pct))
        _set(self, "self_value", _int(self.self_value))
        _set(self, "total_value", _opt_int(self.total_value))
        _set(self, "calls", _opt_int(self.calls))
        _set(self, "in_project", bool(self.in_project))


@dataclass(frozen=True, slots=True)
class HotPath:
    frames: tuple[str, ...]
    pct: float = 0.0
    value: int = 0

    def __post_init__(self) -> None:
        cleaned = tuple(_text(frame) or "?" for frame in tuple(self.frames)[:4096])
        _set(self, "frames", elide_frames(cleaned))
        _set(self, "pct", _float(self.pct))
        _set(self, "value", _int(self.value))


@dataclass(frozen=True, slots=True)
class Spike:
    kind: Literal["frame", "zone", "sample_density"]
    label: str
    start_ns: int | None
    duration_ns: int
    ratio_to_median: float
    thread: str | None = None

    def __post_init__(self) -> None:
        kind = str(self.kind)
        _set(self, "kind", kind if kind in ("frame", "zone", "sample_density") else "zone")
        _set(self, "label", _text(self.label))
        _set(self, "start_ns", _opt_int(self.start_ns))
        _set(self, "duration_ns", _int(self.duration_ns))
        _set(self, "ratio_to_median", _float(self.ratio_to_median))
        _set(self, "thread", _opt_text(self.thread))


@dataclass(frozen=True, slots=True)
class FrameStats:
    count: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    budget_ms: float | None = None
    over_budget: int = 0

    def __post_init__(self) -> None:
        _set(self, "count", _int(self.count))
        for name in ("p50_ms", "p95_ms", "p99_ms", "max_ms"):
            _set(self, name, _float(getattr(self, name)))
        _set(self, "budget_ms", _opt_float(self.budget_ms))
        _set(self, "over_budget", _int(self.over_budget))


@dataclass(frozen=True, slots=True)
class AllocationHotspot:
    function: str
    bytes_total: int = 0
    allocations: int = 0
    peak_bytes: int | None = None
    leaked_bytes: int | None = None
    # Additive to the spec'd shape: where the allocation site is, when the
    # tool reported it (heaptrack_print does). Both are untrusted strings.
    file: str | None = None
    line: int | None = None
    temporary: int | None = None

    def __post_init__(self) -> None:
        _set(self, "function", _text(self.function) or "?")
        _set(self, "bytes_total", _int(self.bytes_total))
        _set(self, "allocations", _int(self.allocations))
        _set(self, "peak_bytes", _opt_int(self.peak_bytes))
        _set(self, "leaked_bytes", _opt_int(self.leaked_bytes))
        _set(self, "file", _opt_text(self.file))
        _set(self, "line", _opt_int(self.line))
        _set(self, "temporary", _opt_int(self.temporary))


@dataclass(frozen=True, slots=True)
class ContextSwitchHotspot:
    thread: str
    switches: int = 0
    wait_ms: float = 0.0
    readying_function: str | None = None

    def __post_init__(self) -> None:
        _set(self, "thread", _text(self.thread) or "?")
        _set(self, "switches", _int(self.switches))
        _set(self, "wait_ms", _float(self.wait_ms))
        _set(self, "readying_function", _opt_text(self.readying_function))


@dataclass(frozen=True, slots=True)
class CaptureMetadata:
    tool: str = "unknown"
    tool_version: str | None = None
    duration_ns: int | None = None
    sample_count: int | None = None
    event: str = ""
    threads: int | None = None
    process: str | None = None

    def __post_init__(self) -> None:
        _set(self, "tool", _text(self.tool) or "unknown")
        _set(self, "tool_version", _opt_text(self.tool_version))
        _set(self, "duration_ns", _opt_int(self.duration_ns))
        _set(self, "sample_count", _opt_int(self.sample_count))
        _set(self, "event", _text(self.event))
        _set(self, "threads", _opt_int(self.threads))
        _set(self, "process", _opt_text(self.process))


def _enum_value(value: object, enum: type[Enum], default: str) -> str:
    raw = value.value if isinstance(value, Enum) else str(value or "")
    allowed = {member.value for member in enum}
    return raw if raw in allowed else default


@dataclass(frozen=True, slots=True)
class ProfileDigest:
    schema: str = SCHEMA
    source_kind: str = ProfileSourceKind.PERF_TEXT.value
    engines: tuple[str, ...] = (ProfileEngine.PURE.value,)
    source_label: str = ""
    input_sha256: str = ""
    metric: str = ""
    unit: str = ""
    metadata: CaptureMetadata = field(default_factory=CaptureMetadata)
    top_self: tuple[ProfileFunction, ...] = ()
    top_total: tuple[ProfileFunction, ...] = ()
    hot_paths: tuple[HotPath, ...] = ()
    spikes: tuple[Spike, ...] = ()
    frames: FrameStats | None = None
    allocations: tuple[AllocationHotspot, ...] = ()
    context_switches: tuple[ContextSwitchHotspot, ...] = ()
    egress_isolation: Literal["netns", "none", "n/a"] = "n/a"
    notes: tuple[str, ...] = ()
    truncated: bool = False
    # sha256 over the canonical content (every field but this one). Always
    # recomputed at construction, so a caller can never pass a stale value.
    digest: str = ""
    untrusted_strings: bool = True

    def __post_init__(self) -> None:
        _set(self, "schema", SCHEMA)
        _set(self, "source_kind", _enum_value(self.source_kind, ProfileSourceKind,
                                              ProfileSourceKind.PERF_TEXT.value))
        engines = tuple(_enum_value(item, ProfileEngine, ProfileEngine.PURE.value)
                        for item in tuple(self.engines)[:MAX_ENGINES])
        _set(self, "engines", tuple(dict.fromkeys(engines)) or (ProfileEngine.PURE.value,))
        _set(self, "source_label", _text(self.source_label))
        sha = str(self.input_sha256 or "").lower()
        _set(self, "input_sha256", sha if len(sha) == 64 and set(sha) <= _HEX64 else "")
        _set(self, "metric", _text(self.metric, 64))
        _set(self, "unit", _text(self.unit, 32))
        if not isinstance(self.metadata, CaptureMetadata):
            _set(self, "metadata", CaptureMetadata())
        _set(self, "top_self", _typed(self.top_self, ProfileFunction, MAX_TOP_FUNCTIONS))
        _set(self, "top_total", _typed(self.top_total, ProfileFunction, MAX_TOP_FUNCTIONS))
        _set(self, "hot_paths", _typed(self.hot_paths, HotPath, MAX_HOT_PATHS))
        _set(self, "spikes", _typed(self.spikes, Spike, MAX_SPIKES))
        if self.frames is not None and not isinstance(self.frames, FrameStats):
            _set(self, "frames", None)
        _set(self, "allocations", _typed(self.allocations, AllocationHotspot, MAX_ALLOCATIONS))
        _set(self, "context_switches", _typed(self.context_switches, ContextSwitchHotspot,
                                              MAX_CONTEXT_SWITCHES))
        isolation = str(self.egress_isolation)
        _set(self, "egress_isolation", isolation if isolation in ("netns", "none", "n/a") else "n/a")
        notes = tuple(_text(note) for note in tuple(self.notes)[: MAX_NOTES * 4])
        _set(self, "notes", tuple(dict.fromkeys(n for n in notes if n))[:MAX_NOTES])
        _set(self, "truncated", bool(self.truncated))
        _set(self, "untrusted_strings", True)
        _set(self, "digest", content_digest(self))


def _typed(items: object, kind: type, limit: int) -> tuple:
    try:
        values = tuple(items)[: limit]  # type: ignore[arg-type]
    except TypeError:
        return ()
    return tuple(item for item in values if isinstance(item, kind))


def part_to_dict(value: Any) -> Any:
    """Plain JSON-ready structure for a digest part (dataclasses, tuples, scalars)."""
    if hasattr(value, "__dataclass_fields__"):
        return {f.name: part_to_dict(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (tuple, list)):
        return [part_to_dict(item) for item in value]
    return value


def content_digest(digest: ProfileDigest) -> str:
    payload = {f.name: part_to_dict(getattr(digest, f.name))
               for f in fields(digest) if f.name != "digest"}
    text = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def with_source(
    digest: ProfileDigest,
    *,
    source_label: str | None = None,
    input_sha256: str | None = None,
    engines: tuple[str, ...] | None = None,
    egress_isolation: str | None = None,
    extra_notes: tuple[str, ...] = (),
) -> ProfileDigest:
    """Bind caller-known identity (label, sha256, engines) to a parser result."""
    changes: dict[str, Any] = {}
    if source_label is not None:
        changes["source_label"] = source_label
    if input_sha256 is not None:
        changes["input_sha256"] = input_sha256
    if engines is not None:
        changes["engines"] = tuple(engines)
    if egress_isolation is not None:
        changes["egress_isolation"] = egress_isolation
    if extra_notes:
        changes["notes"] = tuple(digest.notes) + tuple(extra_notes)
    return replace(digest, **changes)


# --------------------------------------------------------------------------
# Parser bounds shared by every reader in this package


@dataclass(frozen=True, slots=True)
class ProfileLimits:
    max_stacks: int = 200_000
    max_depth: int = 128
    max_lines: int = 2_000_000
    max_functions: int = 100_000
    max_events: int = 2_000_000
    max_event_bytes: int = 65_536
    max_line_chars: int = 65_536
    max_json_depth: int = 64
    max_seconds: float = 20.0


DEFAULT_LIMITS = ProfileLimits()


class WorkBudget:
    """Wall-clock budget polled every ``every`` ticks through an injected clock."""

    __slots__ = ("_clock", "_deadline", "_every", "_count", "exceeded")

    def __init__(self, max_seconds: float, clock: Callable[[], float] | None = None,
                 *, every: int = 1000) -> None:
        self._clock = clock or time.monotonic
        self._deadline = self._clock() + max(0.0, float(max_seconds))
        self._every = max(1, int(every))
        self._count = 0
        self.exceeded = False

    def tick(self) -> bool:
        """True while within budget. Checks the clock once per ``every`` ticks."""
        self._count += 1
        if self._count % self._every == 0 and self._clock() > self._deadline:
            self.exceeded = True
        return not self.exceeded


def iter_bounded_lines(text: str, max_line_chars: int) -> Iterator[str | None]:
    """Lines of ``text`` without copying the whole input; ``None`` for an oversize line.

    An oversize line (more than ``max_line_chars``) is reported as ``None`` so
    callers can count and skip it without ever materialising it.
    """
    size = len(text)
    start = 0
    limit = max(1, int(max_line_chars))
    while start < size:
        end = text.find("\n", start)
        if end < 0:
            end = size
        if end - start > limit:
            yield None
        else:
            line = text[start:end]
            if line.endswith("\r"):
                line = line[:-1]
            yield line
        start = end + 1


class CsvStats:
    __slots__ = ("oversize", "rows", "truncated", "error")

    def __init__(self) -> None:
        self.oversize = 0
        self.rows = 0
        self.truncated = False
        self.error = ""


def bounded_csv_rows(text: str, stats: CsvStats, *, max_line_chars: int = 65_536,
                     max_rows: int = DEFAULT_LIMITS.max_lines,
                     budget: WorkBudget | None = None) -> Iterator[list[str]]:
    """CSV rows with every physical line (and so every field) at most 64 KiB.

    Lines over ``max_line_chars`` are skipped and counted without being
    materialised, which is the 64 KiB ``field_size_limit`` applied without
    mutating the process-global ``csv.field_size_limit``. A ``csv.Error``
    stops the read and is recorded in ``stats.error``.
    """
    def accepted() -> Iterator[str]:
        for line in iter_bounded_lines(text, max_line_chars):
            if line is None:
                stats.oversize += 1
                continue
            yield line

    reader = csv.reader(accepted())
    while True:
        if stats.rows >= max_rows or (budget is not None and not budget.tick()):
            stats.truncated = True
            return
        try:
            row = next(reader)
        except StopIteration:
            return
        except csv.Error as exc:
            stats.error = clean_text(str(exc), WIRE_TEXT_CHARS)
            return
        stats.rows += 1
        yield row


def header_key(value: str) -> str:
    """Lower-case header cell without BOM, quotes or surrounding space."""
    return value.replace("﻿", "").strip().strip('"').strip().lower()


def clip_name(name: object) -> str:
    """A raw frame name clipped for use as an aggregation key."""
    text = "" if name is None else str(name)
    text = text[:MAX_RAW_NAME_CHARS].strip()
    return text or "?"


def program_name(command: object) -> str | None:
    """Basename of the program in a recorded command line, arguments dropped.

    Profilers record the full argv (callgrind ``cmd:``, heaptrack ``Debuggee
    command was:``). Its directories carry user names and its arguments can
    carry secrets, so only the program's file name reaches the digest.
    """
    text = "" if command is None else str(command)[:4096].strip()
    if not text:
        return None
    if text[0] in "\"'":
        quote = text[0]
        end = text.find(quote, 1)
        program = text[1:end] if end > 0 else text[1:]
    else:
        program = text.split(None, 1)[0]
    name = program.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return name[:MAX_RAW_NAME_CHARS] or None


def finite_number(text: str, *, max_chars: int = 64) -> float | None:
    """A finite float from ``text`` or None; refuses giant digit strings cheaply."""
    value = text.strip()
    if not value or len(value) > max_chars:
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None
