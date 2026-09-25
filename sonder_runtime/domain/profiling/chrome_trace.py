"""Chrome trace-event JSON (chrome://tracing, Perfetto UI JSON, Tracy/Unreal exports).

Decoding is incremental and bounded through ``domain.common.bounded_json.
iter_array_objects``: at most 2M events, at most 64 KiB per event (larger ones
are skipped and counted), JSON nesting capped. Both the ``{"traceEvents":
[...]}`` object form and the bare-array form are read.

- ``X`` complete events and ``B``/``E`` pairs (rebuilt per pid/tid) become
  zones, nested per thread for self/total time and hot paths.
- Frames come from zones named in a configurable set (``Frame``,
  ``FrameMark``, ``GameFrame``) or the explicit ``frame_zone``; failing that,
  from instant events (``ph: I/i/R``) with one of those names.
- ``M`` metadata supplies thread and process names.

Native Perfetto traces are protobuf, not JSON: they raise
``ProfileFormatUnknown`` with a ``traceconv`` hint.
"""
from __future__ import annotations

import codecs
import math
from array import array
from itertools import chain
from typing import Any, Callable, Iterable, Iterator

from sonder_runtime.domain.common import bounded_json
from sonder_runtime.domain.profiling.folded import (
    DEFAULT_FRAME_NAMES,
    ProjectPredicate,
    digest_zones,
)
from sonder_runtime.domain.profiling.model import (
    DEFAULT_LIMITS,
    MAX_HOT_PATHS,
    MAX_TOP_FUNCTIONS,
    TRUNCATED_FRAME,
    CaptureMetadata,
    ProfileDigest,
    ProfileFormatUnknown,
    ProfileLimits,
    ProfileParseError,
    ProfileSourceKind,
    WorkBudget,
    clip_name,
)

PERFETTO_HINT = "native Perfetto traces are protobuf; convert first with `traceconv json <trace> <out.json>`"
_MAX_PEEK_CHARS = 1 << 20
_MAX_OPEN_B = 4096
_MAX_THREADS = 4096
_MAX_TS_US = 1e15
_INSTANT_PHASES = frozenset({"I", "i", "R"})


def looks_like_perfetto_protobuf(head: bytes | str) -> bool:
    """A protobuf ``Trace`` starts with field 1 (0x0a) and carries binary bytes."""
    if isinstance(head, str):
        data = head[:64].encode("latin-1", "replace")
    else:
        data = bytes(head[:64])
    if not data or data[0] != 0x0A:
        return False
    return any(byte < 0x09 or 0x0E <= byte < 0x20 or byte >= 0x80 for byte in data[1:])


def _text_chunks(chunks: Iterable[str | bytes]) -> Iterator[str]:
    decoder = None
    for chunk in chunks:
        if isinstance(chunk, (bytes, bytearray, memoryview)):
            if decoder is None:
                decoder = codecs.getincrementaldecoder("utf-8")("replace")
            text = decoder.decode(bytes(chunk))
        else:
            text = str(chunk)
        if text:
            yield text
    if decoder is not None:
        tail = decoder.decode(b"", final=True)
        if tail:
            yield tail


def _peek(chunks: Iterable[str | bytes]) -> tuple[str, list[str | bytes], Iterator[str | bytes]]:
    """First non-whitespace character, the chunks consumed to find it, and the rest."""
    source = iter(chunks)
    consumed: list[str | bytes] = []
    seen = 0
    for chunk in source:
        consumed.append(chunk)
        if isinstance(chunk, (bytes, bytearray, memoryview)):
            text = bytes(chunk[:4096]).decode("utf-8", "replace")
        else:
            text = str(chunk)[:4096]
        stripped = text.lstrip("﻿ \t\r\n")
        if stripped:
            return stripped[0], consumed, source
        seen += len(text)
        if seen > _MAX_PEEK_CHARS:
            break
    return "", consumed, source


class _ZoneColumns:
    """One thread's zones stored compactly; iterates as (start_ns, dur_ns, name)."""

    __slots__ = ("starts", "durations", "name_ids", "names")

    def __init__(self, names: list[str]) -> None:
        self.starts = array("q")
        self.durations = array("q")
        self.name_ids = array("l")
        self.names = names

    def add(self, start: int, duration: int, name_id: int) -> None:
        self.starts.append(start)
        self.durations.append(duration)
        self.name_ids.append(name_id)

    def __len__(self) -> int:
        return len(self.starts)

    def __iter__(self) -> Iterator[tuple[int, int, str]]:
        names = self.names
        for start, duration, name_id in zip(self.starts, self.durations, self.name_ids):
            yield start, duration, names[name_id]


def _ns(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or abs(number) > _MAX_TS_US:
        return None
    return int(round(number * 1000.0))


def _ident(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return "?"
    return str(value)[:32]


def _drain(source: Iterable[Any], box: dict) -> Iterator[Any]:
    box["result"] = yield from source


def _skipped_from(result: Any, source: Any) -> tuple[int, bool]:
    """Skipped-element count and truncation flag reported by the JSON scanner."""
    skipped, truncated = 0, False
    for holder in (result, source):
        if holder is None:
            continue
        if isinstance(holder, bool):
            continue
        if isinstance(holder, int):
            skipped = max(skipped, holder)
        elif isinstance(holder, dict):
            skipped = max(skipped, int(holder.get("skipped", 0) or 0))
            truncated = truncated or bool(holder.get("truncated"))
        else:
            value = getattr(holder, "skipped", 0)
            if isinstance(value, int) and not isinstance(value, bool):
                skipped = max(skipped, value)
            truncated = truncated or bool(getattr(holder, "truncated", False) is True)
    return skipped, truncated


def parse_chrome_trace(
    chunks: Iterable[str | bytes],
    limits: ProfileLimits = DEFAULT_LIMITS,
    *,
    frame_names: Iterable[str] = DEFAULT_FRAME_NAMES,
    frame_zone: str = "",
    frame_budget_ms: float | None = None,
    thread: str = "",
    top_n: int = MAX_TOP_FUNCTIONS,
    path_n: int = MAX_HOT_PATHS,
    project: ProjectPredicate | None = None,
    clock: Callable[[], float] | None = None,
) -> ProfileDigest:
    first, consumed, rest = _peek(chunks)
    if first not in ("{", "["):
        head = consumed[0] if consumed else b""
        if looks_like_perfetto_protobuf(head if isinstance(head, (bytes, str)) else bytes(head)):
            raise ProfileFormatUnknown("not a JSON trace (protobuf bytes)", hint=PERFETTO_HINT)
        raise ProfileFormatUnknown("not a Chrome trace JSON document")
    text = _text_chunks(chain(consumed, rest))
    if first == "[":
        # Bare-array form: wrap it so the scanner always reads one keyed array.
        text = chain(['{"traceEvents":'], text, ["}"])
    frame_set = {frame_zone} if frame_zone else set(frame_names)
    budget = WorkBudget(limits.max_seconds, clock)
    try:
        scanner = bounded_json.iter_array_objects(
            text, key="traceEvents", max_items=limits.max_events,
            max_item_bytes=limits.max_event_bytes,
        )
        box: dict = {}
        return _consume(_drain(scanner, box), box, scanner, limits, budget, frame_set,
                        frame_zone=frame_zone, frame_budget_ms=frame_budget_ms, thread=thread,
                        top_n=top_n, path_n=path_n, project=project)
    except (ProfileFormatUnknown, ProfileParseError):
        raise
    except (ValueError, TypeError, RecursionError, OverflowError, KeyError,
            IndexError, UnicodeError) as exc:
        raise ProfileParseError("Chrome trace JSON could not be read: %s"
                                % type(exc).__name__) from None


def _consume(events, box, scanner, limits: ProfileLimits, budget: WorkBudget, frame_set,
             *, frame_zone, frame_budget_ms, thread, top_n, path_n, project) -> ProfileDigest:
    names: list[str] = []
    name_ids: dict[str, int] = {}
    threads: dict[str, _ZoneColumns] = {}
    open_b: dict[str, list[tuple[int, int]]] = {}
    thread_names: dict[str, str] = {}
    process_name: str | None = None
    marks: list[int] = []
    count = non_objects = bad = dropped_threads = 0
    lo: int | None = None
    hi: int | None = None
    truncated = False

    def intern(name: str) -> int:
        found = name_ids.get(name)
        if found is None:
            if len(names) >= limits.max_functions:
                name = TRUNCATED_FRAME
                found = name_ids.get(name)
                if found is not None:
                    return found
            found = name_ids[name] = len(names)
            names.append(name)
        return found

    def columns(key: str) -> _ZoneColumns | None:
        nonlocal dropped_threads
        found = threads.get(key)
        if found is None:
            if len(threads) >= _MAX_THREADS:
                dropped_threads += 1
                return None
            found = threads[key] = _ZoneColumns(names)
        return found

    def span(start: int, end: int) -> None:
        nonlocal lo, hi
        lo = start if lo is None else min(lo, start)
        hi = end if hi is None else max(hi, end)

    for event in events:
        count += 1
        if not budget.tick():
            truncated = True
            break
        if not isinstance(event, dict):
            non_objects += 1
            continue
        phase = event.get("ph")
        if not isinstance(phase, str):
            bad += 1
            continue
        key = "%s:%s" % (_ident(event.get("pid")), _ident(event.get("tid")))
        raw_name = event.get("name")
        name = clip_name(raw_name) if isinstance(raw_name, str) else "?"
        if phase == "M":
            args = event.get("args")
            value = args.get("name") if isinstance(args, dict) else None
            if isinstance(value, str):
                if raw_name == "thread_name" and len(thread_names) < _MAX_THREADS:
                    thread_names[key] = clip_name(value)[:64]
                elif raw_name == "process_name" and process_name is None:
                    process_name = clip_name(value)
            continue
        start = _ns(event.get("ts"))
        if start is None:
            if phase in ("X", "B", "E") or phase in _INSTANT_PHASES:
                bad += 1
            continue
        if phase == "X":
            duration = _ns(event.get("dur"))
            if duration is None or duration < 0:
                bad += 1
                continue
            target = columns(key)
            if target is not None:
                target.add(start, duration, intern(name))
                span(start, start + duration)
        elif phase == "B":
            stack = open_b.setdefault(key, [])
            if len(stack) >= _MAX_OPEN_B:
                bad += 1
                continue
            stack.append((start, intern(name)))
        elif phase == "E":
            stack = open_b.get(key)
            if not stack:
                bad += 1
                continue
            begin, name_id = stack.pop()
            if start < begin:
                bad += 1
                continue
            target = columns(key)
            if target is not None:
                target.add(begin, start - begin, name_id)
                span(begin, start)
        elif phase in _INSTANT_PHASES and name in frame_set:
            if len(marks) < limits.max_events:
                marks.append(start)
                span(start, start)
    skipped, scanner_truncated = _skipped_from(box.get("result"), scanner)
    truncated = truncated or scanner_truncated or count >= limits.max_events
    if not count and not skipped:
        raise ProfileParseError("Chrome trace has no events")
    notes = []
    if skipped:
        notes.append("%d events over %d bytes (or too deeply nested) skipped"
                     % (skipped, limits.max_event_bytes))
    if non_objects or bad:
        notes.append("%d malformed events ignored" % (non_objects + bad))
    unterminated = sum(len(stack) for stack in open_b.values())
    if unterminated:
        notes.append("%d B events without a matching E ignored" % unterminated)
    if dropped_threads:
        notes.append("events on more than %d threads ignored" % _MAX_THREADS)
    if truncated:
        notes.append("trace clipped at the event or time budget")
    metadata = CaptureMetadata(
        tool="chrome_trace", event="trace events", sample_count=count,
        threads=len(threads) or None, process=process_name,
        duration_ns=(hi - lo) if lo is not None and hi is not None else None,
    )
    return digest_zones(
        {key: value for key, value in threads.items() if len(value)},
        source_kind=ProfileSourceKind.CHROME_TRACE.value, metadata=metadata,
        thread_names=thread_names, frame_names=frame_set, frame_zone=frame_zone,
        frame_marks=marks, frame_budget_ms=frame_budget_ms, thread=thread, top_n=top_n,
        path_n=path_n, project=project, limits=limits, budget=budget, notes=notes,
        truncated=truncated,
    )
