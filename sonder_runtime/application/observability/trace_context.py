"""W3C Trace Context (`traceparent`) parsing and formatting. Pure, stdlib only.

Status: **experimental seam**. The runtime threads ``trace_id`` /
``correlation_id`` values manually and speaks no W3C Trace Context at its
protocol boundaries, so a trace started by an OpenTelemetry-instrumented
caller breaks at Sonder's front door. This module is the validated
building block for the interface layer (``interfaces/http``,
``interfaces/mcp``, ``interfaces/a2a``) to accept and emit ``traceparent``
headers when that propagation is wired; nothing imports it on the hot path
yet, and it deliberately imports no OpenTelemetry SDK.

Grounding: the field grammar, version handling, and validity rules follow
the W3C Trace Context recommendation (level 1) — 2-hex-digit version
(``ff`` forbidden), 32-hex trace-id and 16-hex parent-id (all-zero
forbidden), 2-hex flags, lowercase hex only, and forward compatibility:
a header with a higher version parses when its level-1 prefix is valid
and is followed by ``-`` plus additional data.

Security note: an inbound header is attacker-controlled text. Parsing is
strict and total (returns ``None`` rather than raising on any malformed
input), and nothing here ever executes, logs, or stores header content.
Randomness for new ids is the caller's responsibility (inject
``secrets.token_hex``); keeping this module deterministic keeps it
testable and replay-safe.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID = re.compile(r"^[0-9a-f]{16}$")
_VERSION = re.compile(r"^[0-9a-f]{2}$")
_FLAGS = re.compile(r"^[0-9a-f]{2}$")

SAMPLED_FLAG = 0x01
MAX_HEADER_LENGTH = 512


@dataclass(frozen=True, slots=True)
class TraceParent:
    """One validated ``traceparent`` value."""

    trace_id: str
    parent_id: str
    flags: int = 0
    version: int = 0

    @property
    def sampled(self) -> bool:
        return bool(self.flags & SAMPLED_FLAG)


def parse_traceparent(header) -> TraceParent | None:
    """Strictly parse an inbound ``traceparent`` header. Total: never raises.

    Returns ``None`` for anything malformed — wrong field count for the
    declared version, uppercase hex, bad lengths, forbidden values
    (version ``ff``, all-zero ids), or an oversized header.
    """
    if not isinstance(header, str) or not header or len(header) > MAX_HEADER_LENGTH:
        return None
    parts = header.strip().split("-")
    if len(parts) < 4:
        return None
    version_field, trace_id, parent_id, flags_field = parts[:4]
    if not _VERSION.fullmatch(version_field) or version_field == "ff":
        return None
    version = int(version_field, 16)
    if version == 0 and len(parts) != 4:
        return None
    # Future versions must carry at least one additional dash-separated
    # field; a bare version-0 shape claiming a higher version is malformed.
    if version > 0 and len(parts) == 4:
        return None
    if not _TRACE_ID.fullmatch(trace_id) or trace_id == "0" * 32:
        return None
    if not _SPAN_ID.fullmatch(parent_id) or parent_id == "0" * 16:
        return None
    if not _FLAGS.fullmatch(flags_field):
        return None
    return TraceParent(
        trace_id=trace_id,
        parent_id=parent_id,
        flags=int(flags_field, 16),
        version=version,
    )


def format_traceparent(context: TraceParent) -> str:
    """Render a level-1 (version 00) header for outbound propagation."""
    if not isinstance(context, TraceParent):
        raise TypeError("context must be a TraceParent")
    if not _TRACE_ID.fullmatch(context.trace_id) or context.trace_id == "0" * 32:
        raise ValueError("trace_id must be 32 lowercase hex digits, not all zero")
    if not _SPAN_ID.fullmatch(context.parent_id) or context.parent_id == "0" * 16:
        raise ValueError("parent_id must be 16 lowercase hex digits, not all zero")
    if not 0 <= int(context.flags) <= 0xFF:
        raise ValueError("flags must fit one byte")
    return "00-%s-%s-%02x" % (context.trace_id, context.parent_id, context.flags)


def child_context(context: TraceParent, span_id: str) -> TraceParent:
    """The context a downstream call should carry: same trace, new span.

    ``span_id`` comes from the caller (e.g. ``secrets.token_hex(8)``); it is
    validated, never generated here.
    """
    if not isinstance(span_id, str) or not _SPAN_ID.fullmatch(span_id) or span_id == "0" * 16:
        raise ValueError("span_id must be 16 lowercase hex digits, not all zero")
    return TraceParent(
        trace_id=context.trace_id,
        parent_id=span_id,
        flags=context.flags,
        version=0,
    )


__all__ = [
    "MAX_HEADER_LENGTH",
    "SAMPLED_FLAG",
    "TraceParent",
    "child_context",
    "format_traceparent",
    "parse_traceparent",
]
