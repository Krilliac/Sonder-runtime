"""Wire payload and human text for a ``ProfileDigest``.

``digest_to_wire`` returns compact JSON-ready data of at most ``max_bytes``
UTF-8 bytes (48 KB by default): lists are halved in a fixed order until it
fits, and every drop sets ``truncated``. ``render_digest`` is the bounded text
a REPL or model brief shows; strings taken from the capture are labelled
untrusted (SEC-006). ``digest_from_wire`` rebuilds the typed digest from a
cached result JSON, re-applying every construction bound.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from sonder_runtime.domain.profiling.model import (
    AllocationHotspot,
    CaptureMetadata,
    ContextSwitchHotspot,
    FrameStats,
    HotPath,
    ProfileDigest,
    ProfileFunction,
    Spike,
    part_to_dict,
)

MAX_WIRE_BYTES = 48_000
MAX_RENDER_CHARS = 12_000
UNTRUSTED_LABEL = "names below come from the profiled program's capture (untrusted)"

# Shrink order: least useful first.
_SHRINK_ORDER = ("context_switches", "notes", "allocations", "spikes", "hot_paths",
                 "top_total", "top_self")


def _size(payload: Mapping) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def digest_payload(digest: ProfileDigest) -> dict:
    return part_to_dict(digest)


def digest_to_wire(digest: ProfileDigest, max_bytes: int = MAX_WIRE_BYTES) -> dict:
    """``digest`` as a dict of at most ``max_bytes`` compact UTF-8 JSON bytes."""
    max_bytes = max(2_000, int(max_bytes))
    payload = digest_payload(digest)
    if _size(payload) <= max_bytes:
        return payload
    payload["truncated"] = True
    for key in _SHRINK_ORDER:
        items = list(payload.get(key) or [])
        while items and _size(payload) > max_bytes:
            items = items[: len(items) // 2]
            payload[key] = items
        if _size(payload) <= max_bytes:
            return payload
    # Last resort: the scalar core only.
    for key in _SHRINK_ORDER:
        payload[key] = []
    return payload


def _function_line(fn: Mapping[str, Any] | ProfileFunction, *, total: bool) -> str:
    item = part_to_dict(fn) if isinstance(fn, ProfileFunction) else fn
    pct = item.get("total_pct") if total else item.get("self_pct")
    where = ""
    if item.get("file"):
        where = " (%s%s)" % (item["file"], ":%d" % item["line"] if item.get("line") else "")
    elif item.get("module"):
        where = " [%s]" % item["module"]
    calls = " calls=%d" % item["calls"] if item.get("calls") else ""
    project = " *project*" if item.get("in_project") else ""
    value = item.get("total_value") if total else item.get("self_value")
    return "  %6.2f%%  %s%s%s%s  (%s)" % (float(pct or 0.0), item.get("name", "?"), where,
                                           calls, project, value if value is not None else "-")


def render_digest(digest: ProfileDigest, max_chars: int = MAX_RENDER_CHARS) -> str:
    """Bounded human-readable summary; capture strings are labelled untrusted."""
    max_chars = max(400, min(int(max_chars), 64_000))
    meta = digest.metadata
    lines = [
        "profile digest (%s) from %s via %s%s" % (
            digest.source_kind, digest.source_label or "input", ", ".join(digest.engines),
            " [truncated]" if digest.truncated else ""),
        "metric: %s (%s)  tool: %s%s" % (
            digest.metric or "-", digest.unit or "-", meta.tool,
            " %s" % meta.tool_version if meta.tool_version else ""),
    ]
    facts = []
    if meta.process:
        facts.append("process: %s" % meta.process)
    if meta.sample_count is not None:
        facts.append("samples/events: %d" % meta.sample_count)
    if meta.threads is not None:
        facts.append("threads: %d" % meta.threads)
    if meta.duration_ns is not None:
        facts.append("duration: %.3f ms" % (meta.duration_ns / 1e6))
    if facts:
        lines.append("  ".join(facts))
    if digest.input_sha256:
        lines.append("input sha256: %s" % digest.input_sha256)
    lines.append("(%s)" % UNTRUSTED_LABEL)
    if digest.frames is not None:
        frames = digest.frames
        budget = (" budget %.2f ms, %d over" % (frames.budget_ms, frames.over_budget)
                  if frames.budget_ms is not None else "")
        lines.append("frames: %d  p50 %.2f ms  p95 %.2f ms  p99 %.2f ms  max %.2f ms%s" % (
            frames.count, frames.p50_ms, frames.p95_ms, frames.p99_ms, frames.max_ms, budget))
    if digest.top_self:
        lines.append("top self:")
        lines.extend(_function_line(fn, total=False) for fn in digest.top_self)
    if digest.top_total:
        lines.append("top total (inclusive):")
        lines.extend(_function_line(fn, total=True) for fn in digest.top_total)
    if digest.hot_paths:
        lines.append("hot paths:")
        for path in digest.hot_paths:
            lines.append("  %6.2f%%  %s" % (path.pct, " > ".join(path.frames)))
    if digest.spikes:
        lines.append("spikes:")
        for spike in digest.spikes:
            at = " at %.3f ms" % (spike.start_ns / 1e6) if spike.start_ns is not None else ""
            thread = " [%s]" % spike.thread if spike.thread else ""
            lines.append("  %s %s: %.2f ms (%.1fx median)%s%s" % (
                spike.kind, spike.label, spike.duration_ns / 1e6, spike.ratio_to_median, at, thread))
    if digest.allocations:
        lines.append("allocations:")
        for hot in digest.allocations:
            where = " (%s:%s)" % (hot.file, hot.line) if hot.file else ""
            parts = ["calls=%d" % hot.allocations]
            if hot.peak_bytes is not None:
                parts.append("peak=%dB" % hot.peak_bytes)
            if hot.leaked_bytes:
                parts.append("leaked=%dB" % hot.leaked_bytes)
            if hot.temporary:
                parts.append("temporary=%d" % hot.temporary)
            if hot.bytes_total:
                parts.append("allocated=%dB" % hot.bytes_total)
            lines.append("  %s%s  %s" % (hot.function, where, " ".join(parts)))
    if digest.context_switches:
        lines.append("context switches:")
        for item in digest.context_switches:
            lines.append("  %s: %d switches, %.2f ms waiting%s" % (
                item.thread, item.switches, item.wait_ms,
                " (readied by %s)" % item.readying_function if item.readying_function else ""))
    if digest.notes:
        lines.append("notes:")
        lines.extend("  - %s" % note for note in digest.notes)
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 14].rstrip() + "\n[truncated]"
    return text


def _items(data: Mapping, key: str, kind: type) -> tuple:
    out = []
    names = set(kind.__dataclass_fields__)
    for item in data.get(key) or ():
        if isinstance(item, Mapping):
            try:
                out.append(kind(**{k: v for k, v in item.items() if k in names}))
            except TypeError:
                continue
    return tuple(out)


def digest_from_wire(data: Mapping[str, Any]) -> ProfileDigest:
    """Rebuild a digest from ``digest_to_wire`` output (bounds re-applied)."""
    metadata = data.get("metadata")
    frames = data.get("frames")
    meta_fields = set(CaptureMetadata.__dataclass_fields__)
    frame_fields = set(FrameStats.__dataclass_fields__)
    built_frames = None
    if isinstance(frames, Mapping):
        try:
            built_frames = FrameStats(**{k: v for k, v in frames.items() if k in frame_fields})
        except TypeError:
            built_frames = None
    return ProfileDigest(
        source_kind=str(data.get("source_kind", "")),
        engines=tuple(str(item) for item in (data.get("engines") or ())),
        source_label=str(data.get("source_label", "")),
        input_sha256=str(data.get("input_sha256", "")),
        metric=str(data.get("metric", "")),
        unit=str(data.get("unit", "")),
        metadata=(CaptureMetadata(**{k: v for k, v in metadata.items() if k in meta_fields})
                  if isinstance(metadata, Mapping) else CaptureMetadata()),
        top_self=_items(data, "top_self", ProfileFunction),
        top_total=_items(data, "top_total", ProfileFunction),
        hot_paths=_items(data, "hot_paths", HotPath),
        spikes=_items(data, "spikes", Spike),
        frames=built_frames,
        allocations=_items(data, "allocations", AllocationHotspot),
        context_switches=_items(data, "context_switches", ContextSwitchHotspot),
        egress_isolation=str(data.get("egress_isolation", "n/a")),
        notes=tuple(str(item) for item in (data.get("notes") or ())),
        truncated=bool(data.get("truncated")),
    )
