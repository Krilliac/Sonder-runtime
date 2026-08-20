"""Bounded process-local observability over the authoritative EventSink.

This adapter is a disposable inspection cache, not a business ledger.
``operations.db`` remains authoritative for durable audit/recovery events and
``activity_tracker`` remains authoritative for response/tool evidence.  There
is deliberately no exporter, network path, background thread, persistence, or
agent-facing mutation API here.

The EventSink ``summary`` argument is intentionally not retained locally: it is
free text and could contain a prompt. The authoritative delegate is called
first with the original event; local validation can neither rewrite nor
suppress durable audit input.
"""
from __future__ import annotations

import collections
import copy
import itertools
import math
import re
import threading
import time
from collections.abc import Callable

from sonder_runtime.platform.logging import REDACTION_FAILED, Redactor


SEVERITIES = frozenset(("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"))
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,95}$")
_BLOCKED_FIELD_PARTS = (
    "prompt", "content", "body", "message", "reasoning", "thought",
    "request", "instruction", "query", "task", "input",
    "secret", "token", "password", "passwd", "credential", "authorization",
    "api_key", "apikey", "access_key", "auth_key", "private_key",
    "environment", "env", "args", "argv", "command", "process_args", "stdin",
    "stdout", "stderr",
)
_REDACTED_FIELD = "[REDACTED_FIELD]"
_MAX_FIELDS = 32
_MAX_DEPTH = 3
_MAX_LIST_ITEMS = 16
_MAX_TEXT = 240
_MAX_NODES = 256
_MAX_BYTES = 8_192
_UNSAFE_VALUE = "[UNSAFE_VALUE]"
_TRUNCATED_VALUE = "[TRUNCATED_VALUE]"
_BUDGET_EXHAUSTED = "[BUDGET_EXHAUSTED]"


def _safe_identifier(value, fallback: str, redactor: Redactor) -> str:
    if type(value) is not str:
        return fallback
    if len(value) > 96:
        return fallback
    try:
        text = redactor.redact(value).strip()
    except Exception:
        return fallback
    if text == REDACTION_FAILED or text != value.strip():
        return fallback
    return text if _IDENTIFIER.fullmatch(text) else fallback


def _blocked_key(value) -> bool:
    if type(value) is not str:
        return True
    lowered = value.lower().replace("-", "_")
    return any(part in lowered for part in _BLOCKED_FIELD_PARTS)


def _spend(accounting: dict, *, nodes: int = 1, byte_count: int = 0) -> bool:
    if (
        accounting["nodes"] + nodes > _MAX_NODES
        or accounting["bytes"] + byte_count > _MAX_BYTES
    ):
        accounting["budget_exhaustions"] += 1
        return False
    accounting["nodes"] += nodes
    accounting["bytes"] += byte_count
    return True


def _safe_key(key, index: int, redactor: Redactor, accounting: dict) -> str:
    fallback = "field_%d" % index
    if type(key) is not str:
        accounting["rejected_values"] += 1
        return fallback
    if _blocked_key(key):
        accounting["redacted_fields"] += 1
        return "redacted_field_%d" % index
    name = _safe_identifier(key, fallback, redactor)
    if name == fallback and key != fallback:
        accounting["redacted_fields"] += 1
    return name


def _sanitize_value(
    value,
    redactor: Redactor,
    accounting: dict,
    depth: int = 0,
    seen: set[int] | None = None,
    skip_keys: frozenset[str] = frozenset(),
):
    if not _spend(accounting):
        return _BUDGET_EXHAUSTED
    if depth > _MAX_DEPTH:
        accounting["truncated_fields"] += 1
        return "[NESTED_TRUNCATED]"
    if value is None:
        return value if _spend(accounting, nodes=0, byte_count=4) else _BUDGET_EXHAUSTED
    if type(value) is bool:
        return value if _spend(accounting, nodes=0, byte_count=5) else _BUDGET_EXHAUSTED
    if type(value) is int:
        if value.bit_length() > 128:
            accounting["rejected_values"] += 1
            return _UNSAFE_VALUE
        if not _spend(accounting, nodes=0, byte_count=len(str(value))):
            return _BUDGET_EXHAUSTED
        return value
    if type(value) is float:
        if not math.isfinite(value):
            return None
        if not _spend(accounting, nodes=0, byte_count=len(repr(value))):
            return _BUDGET_EXHAUSTED
        return value
    if type(value) is dict:
        seen = seen or set()
        if id(value) in seen:
            accounting["truncated_fields"] += 1
            return "[RECURSION_TRUNCATED]"
        seen.add(id(value))
        result = {}
        items = itertools.islice(value.items(), _MAX_FIELDS + len(skip_keys) + 1)
        retained = 0
        for key, child in items:
            if type(key) is str and key in skip_keys:
                continue
            if retained >= _MAX_FIELDS:
                accounting["truncated_fields"] += 1
                break
            name = _safe_key(key, retained, redactor, accounting)
            if not _spend(accounting, nodes=0, byte_count=len(name.encode("utf-8"))):
                result["truncated"] = _BUDGET_EXHAUSTED
                break
            if _blocked_key(key):
                result[name] = _REDACTED_FIELD
            else:
                result[name] = _sanitize_value(
                    child, redactor, accounting, depth + 1, seen,
                )
            retained += 1
        seen.remove(id(value))
        return result
    if type(value) in (list, tuple):
        seen = seen or set()
        if id(value) in seen:
            accounting["truncated_fields"] += 1
            return "[RECURSION_TRUNCATED]"
        seen.add(id(value))
        items = value[:_MAX_LIST_ITEMS + 1]
        if len(items) > _MAX_LIST_ITEMS:
            accounting["truncated_fields"] += 1
        result = [
            _sanitize_value(item, redactor, accounting, depth + 1, seen)
            for item in items[:_MAX_LIST_ITEMS]
        ]
        seen.remove(id(value))
        return result
    if type(value) is not str:
        accounting["rejected_values"] += 1
        return _UNSAFE_VALUE
    if len(value) > _MAX_TEXT:
        # Never retain a prefix that may be part of a configured secret.  This
        # also keeps adversarial values bounded without asking the redactor to
        # allocate over an arbitrarily large string.
        accounting["truncated_fields"] += 1
        return _TRUNCATED_VALUE
    try:
        # Redact the original value before display-only normalization so exact
        # secret matching still works for values containing NUL characters.
        text = redactor.redact(value).replace("\x00", "\\0")
    except Exception:
        text = REDACTION_FAILED
    if text == REDACTION_FAILED:
        accounting["redaction_failures"] += 1
        return REDACTION_FAILED
    encoded = text.encode("utf-8")
    if len(encoded) > _MAX_TEXT:
        accounting["truncated_fields"] += 1
        encoded = encoded[:_MAX_TEXT]
        text = encoded.decode("utf-8", errors="ignore") + "..."
    if not _spend(accounting, nodes=0, byte_count=len(text.encode("utf-8"))):
        return _BUDGET_EXHAUSTED
    return text


def _percentile(samples: list[int], percentile: float) -> int:
    if not samples:
        return 0
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return int(ordered[index])


class LocalObservabilitySink:
    """Thread-safe, bounded local inspection adapter for an EventSink."""

    def __init__(
        self,
        delegate=None,
        *,
        max_events: int = 256,
        max_series: int = 128,
        latency_window: int = 128,
        clock: Callable[[], float] = time.monotonic,
        redactor: Redactor | None = None,
    ) -> None:
        self._delegate = delegate
        self._max_events = max(1, min(int(max_events), 4096))
        self._max_series = max(1, min(int(max_series), 1024))
        self._latency_window = max(1, min(int(latency_window), 1024))
        self._clock = clock
        self._redactor = redactor or Redactor()
        self._lock = threading.RLock()
        self._events = collections.deque()
        self._series: dict[tuple[str, str, str], dict] = {}
        self._sequence = 0
        self._total_observed = 0
        self._drops = {
            "evicted_events": 0,
            "invalid_events": 0,
            "series_observations": 0,
            "latency_samples": 0,
            "delegate_call_exceptions": 0,
            "local_collection_failures": 0,
            "redacted_fields": 0,
            "redaction_failures": 0,
            "truncated_fields": 0,
            "rejected_values": 0,
            "budget_exhaustions": 0,
        }

    def emit(
        self,
        event_code: str,
        *,
        summary: str,
        detail: dict | None = None,
        severity: str = "INFO",
        correlation_id: str | None = None,
        operation_id: str | None = None,
    ) -> None:
        # The durable authority is invoked first with the untouched event.
        # Local validation and sanitization must never suppress or rewrite it.
        if self._delegate is not None:
            try:
                self._delegate.emit(
                    event_code,
                    summary=summary,
                    detail=detail,
                    severity=severity,
                    correlation_id=correlation_id,
                    operation_id=operation_id,
                )
            except Exception:
                with self._lock:
                    # This means only that the delegate call raised. The
                    # production OperationsEventSink intentionally swallows
                    # storage errors, so this is not delivery attestation.
                    self._drops["delegate_call_exceptions"] += 1
        try:
            self._collect_local(
                event_code,
                detail=detail,
                severity=severity,
                correlation_id=correlation_id,
                operation_id=operation_id,
            )
        except Exception:
            with self._lock:
                self._drops["local_collection_failures"] += 1

    def _collect_local(
        self,
        event_code,
        *,
        detail,
        severity,
        correlation_id,
        operation_id,
    ) -> None:
        raw_detail = detail if type(detail) is dict else {}
        category = _safe_identifier(
            raw_detail.get("category", "application"), "application", self._redactor,
        )
        code = _safe_identifier(event_code, "", self._redactor)
        level = severity.upper() if type(severity) is str else ""
        duration = raw_detail.get("duration_ms")
        if not code or level not in SEVERITIES:
            with self._lock:
                self._drops["invalid_events"] += 1
            return
        if type(duration) is int and not isinstance(duration, bool):
            duration_ms = max(0, min(duration, 86_400_000))
        elif type(duration) is float and math.isfinite(duration):
            duration_ms = max(0, min(int(duration), 86_400_000))
        else:
            duration_ms = None
        accounting = {
            "redacted_fields": 0,
            "redaction_failures": 0,
            "truncated_fields": 0,
            "rejected_values": 0,
            "budget_exhaustions": 0,
            "nodes": 0,
            "bytes": 0,
        }
        if type(detail) is not dict and detail is not None:
            accounting["rejected_values"] += 1
        fields = _sanitize_value(
            raw_detail,
            self._redactor,
            accounting,
            skip_keys=frozenset(("category", "duration_ms")),
        )
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
            correlation = _safe_identifier(
                correlation_id, "local-%012d" % sequence, self._redactor,
            )
            operation = _safe_identifier(operation_id, "", self._redactor) or None
            observed_at = self._clock()
            if type(observed_at) not in (int, float) or not math.isfinite(observed_at):
                observed_at = 0.0
                accounting["rejected_values"] += 1
            event = {
                "sequence": sequence,
                "observed_at": float(observed_at),
                "correlation_id": correlation,
                "category": category,
                "severity": level,
                "event_code": code,
                "duration_ms": duration_ms,
                "operation_id": operation,
                "fields": fields,
            }
            if len(self._events) >= self._max_events:
                self._events.popleft()
                self._drops["evicted_events"] += 1
            self._events.append(event)
            self._total_observed += 1
            for key in (
                "redacted_fields", "redaction_failures", "truncated_fields",
                "rejected_values", "budget_exhaustions",
            ):
                self._drops[key] += accounting[key]
            key = (category, code, level)
            series = self._series.get(key)
            if series is None:
                if len(self._series) >= self._max_series:
                    self._drops["series_observations"] += 1
                else:
                    series = {
                        "count": 0,
                        "latencies": collections.deque(),
                    }
                    self._series[key] = series
            if series is not None:
                series["count"] += 1
                if duration_ms is not None:
                    if len(series["latencies"]) >= self._latency_window:
                        series["latencies"].popleft()
                        self._drops["latency_samples"] += 1
                    series["latencies"].append(duration_ms)

    def recent_events(
        self,
        limit: int = 50,
        *,
        correlation_id: str = "",
        category: str = "",
        severity: str = "",
    ) -> list[dict]:
        bounded = (
            max(1, min(limit, self._max_events))
            if type(limit) is int and not isinstance(limit, bool)
            else min(50, self._max_events)
        )
        with self._lock:
            rows = list(reversed(self._events))
            if type(correlation_id) is str and correlation_id:
                rows = [row for row in rows if row["correlation_id"] == correlation_id]
            if type(category) is str and category:
                rows = [row for row in rows if row["category"] == category]
            if type(severity) is str and severity:
                rows = [row for row in rows if row["severity"] == severity.upper()]
            return copy.deepcopy(rows[:bounded])

    def snapshot(self) -> dict:
        with self._lock:
            series_rows = []
            for (category, code, severity), series in sorted(self._series.items()):
                samples = list(series["latencies"])
                series_rows.append({
                    "category": category,
                    "event_code": code,
                    "severity": severity,
                    "count": int(series["count"]),
                    "latency_ms": {
                        "window_count": len(samples),
                        "min": min(samples) if samples else 0,
                        "max": max(samples) if samples else 0,
                        "mean": int(sum(samples) / len(samples)) if samples else 0,
                        "p50": _percentile(samples, 0.50),
                        "p95": _percentile(samples, 0.95),
                    },
                })
            return {
                "total_observed": self._total_observed,
                "retained_events": len(self._events),
                "series": series_rows,
                "drops": dict(sorted(self._drops.items())),
                "boundaries": {
                    "max_events": self._max_events,
                    "max_series": self._max_series,
                    "latency_window": self._latency_window,
                    "durable_authority": "operations.db",
                    "response_activity_authority": "activity_tracker",
                    "export": "disabled",
                    "delegate_delivery_attestation": "unavailable",
                },
            }
