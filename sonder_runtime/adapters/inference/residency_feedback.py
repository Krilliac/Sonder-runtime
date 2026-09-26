"""Measured-residency feedback for automatic context-window selection.

The context policy picks a window from model metadata and the declared KV
cache type.  Both can be wrong in ways that never raise: the cache type is a
server-side setting Sonder cannot query, and metadata cannot see what else is
using the GPU.  This tracker closes the loop with the one authoritative
signal Ollama exposes, ``/api/ps`` ``size`` versus ``size_vram``:

1. The host records the actual window dispatched in each local model request.
2. At most once per ``check_interval`` per model, and only when the host asks
   for a window again, the tracker reads ``/api/ps``.  It never adds a probe to
   a first request and never probes a model that is not loaded.
3. If the model is split between VRAM and system RAM, the tracker derives a
   smaller window from the measured overflow (see ``domain.kv_budget``) and
   serves it as a ceiling until ``ceiling_ttl`` expires, so a later driver,
   model or workload change can recover the larger window.

Attribution: when Ollama reports the loaded ``context_length`` the reading is
attributed to that window.  Older servers omit it; the reading is then
attributed to the last window this process selected, which is correct unless
another client loaded the same model with a different window.  A wrong
attribution can only lower the window (never raise it past the policy), and
it expires with the TTL.

Fully CPU-resident models are never clamped: shrinking a window does not move
a CPU-placed model onto a GPU, and a CPU-only host is a legitimate topology.
The transport (``fetch_ps``) and clock are injected; this module performs no
network I/O itself.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ...domain.kv_budget import (
    HYBRID,
    ModelGeometry,
    ResidencyReading,
    reading_from_ps_row,
    window_after_spill,
)

logger = logging.getLogger(__name__)

DEFAULT_CHECK_INTERVAL_SECONDS = 60.0
DEFAULT_CEILING_TTL_SECONDS = 1800.0


@dataclass(frozen=True)
class ResidencyVerdict:
    """The most recent measurement for one model, for diagnostics."""

    model: str
    placement: str
    observed_context: int | None
    attributed_by: str
    spilled_bytes: int
    ceiling: int | None
    unfixable: bool
    measured_at: float


@dataclass
class _ModelState:
    last_selected: int | None = None
    last_checked: float | None = None
    ceiling: int | None = None
    ceiling_expires: float = 0.0
    verdict: ResidencyVerdict | None = None


class ResidencyFeedback:
    """Thread-safe per-model spill tracker feeding ``residency_ceiling``."""

    def __init__(
        self,
        fetch_ps: Callable[[], Mapping[str, Any]],
        *,
        minimum_context: int,
        origin: str = "",
        clock: Callable[[], float] = time.monotonic,
        check_interval: float = DEFAULT_CHECK_INTERVAL_SECONDS,
        ceiling_ttl: float = DEFAULT_CEILING_TTL_SECONDS,
    ) -> None:
        if check_interval < 0 or ceiling_ttl <= 0:
            raise ValueError("check_interval must be >= 0 and ceiling_ttl > 0")
        self._fetch_ps = fetch_ps
        self.origin = origin
        self._minimum = max(1, int(minimum_context))
        self._clock = clock
        self._interval = float(check_interval)
        self._ttl = float(ceiling_ttl)
        self._lock = threading.Lock()
        # Serializes probes so concurrent requests cannot issue parallel
        # /api/ps calls for the same stale entry.
        self._probe_lock = threading.Lock()
        self._states: dict[str, _ModelState] = {}

    @staticmethod
    def _key(model: str) -> str:
        return str(model or "").strip().casefold()

    def ceiling(self, model: str) -> int | None:
        """Return the unexpired measured ceiling for ``model``, if any."""
        key = self._key(model)
        now = self._clock()
        with self._lock:
            state = self._states.get(key)
            if state is None or state.ceiling is None:
                return None
            if now >= state.ceiling_expires:
                state.ceiling = None
                return None
            return state.ceiling

    def note_selection(self, model: str, context: int) -> None:
        """Record a dispatched window; planning-only calls must not use this."""
        key = self._key(model)
        if not key:
            return
        with self._lock:
            self._states.setdefault(key, _ModelState()).last_selected = int(context)

    def verdict(self, model: str) -> ResidencyVerdict | None:
        with self._lock:
            state = self._states.get(self._key(model))
            return state.verdict if state else None

    def forget_selection(self, model: str) -> None:
        """Discard attribution and stale measurements for an unknown-window load."""
        with self._lock:
            self._states.pop(self._key(model), None)

    def refresh(
        self,
        model: str,
        *,
        geometry: ModelGeometry | None,
        kv_type: str,
    ) -> ResidencyVerdict | None:
        """Probe ``/api/ps`` if due and update the ceiling for ``model``.

        Returns the new verdict, or ``None`` when no probe was due, the model
        is not loaded, nothing was ever selected for it, or the transport
        failed.  Failures are logged and never propagate: residency feedback
        is an optimization and must not make a model unusable.
        """
        key = self._key(model)
        if not key:
            return None
        now = self._clock()
        with self._lock:
            state = self._states.get(key)
            if state is None or state.last_selected is None:
                return None
            if state.last_checked is not None and now - state.last_checked < self._interval:
                return None
            selected = state.last_selected
        if not self._probe_lock.acquire(blocking=False):
            return None
        try:
            # Do not consume the throttle window until this caller owns the
            # probe slot.  A concurrent model that loses the non-blocking
            # acquisition must remain immediately eligible for a later probe.
            # Recheck under the state lock because another caller may have
            # completed a probe while this caller waited for the slot.
            with self._lock:
                state = self._states.get(key)
                now = self._clock()
                if state is None or state.last_selected is None:
                    return None
                if state.last_checked is not None and now - state.last_checked < self._interval:
                    return None
                state.last_checked = now
                selected = state.last_selected
            reading = self._read(key)
        finally:
            self._probe_lock.release()
        if reading is None:
            return None
        return self._apply(key, reading, selected, geometry, kv_type, now, state)

    def _read(self, key: str) -> ResidencyReading | None:
        try:
            payload = self._fetch_ps()
        except Exception:
            logger.debug("residency probe failed", exc_info=True)
            return None
        rows = payload.get("models") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            logger.debug("residency probe returned no model list")
            return None
        for row in rows:
            reading = reading_from_ps_row(row if isinstance(row, Mapping) else None)
            if reading is not None and self._key(reading.model) == key:
                return reading
        return None

    def _apply(
        self,
        key: str,
        reading: ResidencyReading,
        selected: int,
        geometry: ModelGeometry | None,
        kv_type: str,
        now: float,
        expected_state: _ModelState,
    ) -> ResidencyVerdict | None:
        if reading.context_length is not None:
            observed, attributed_by = reading.context_length, "server-reported"
        else:
            observed, attributed_by = selected, "last-selection"
        ceiling = None
        unfixable = False
        if reading.placement == HYBRID:
            ceiling = window_after_spill(
                observed,
                reading.spilled_bytes,
                geometry=geometry,
                kv_type=kv_type,
                minimum=self._minimum,
            )
            unfixable = ceiling is None
        verdict = ResidencyVerdict(
            model=reading.model,
            placement=reading.placement,
            observed_context=observed,
            attributed_by=attributed_by,
            spilled_bytes=reading.spilled_bytes,
            ceiling=ceiling,
            unfixable=unfixable,
            measured_at=now,
        )
        with self._lock:
            state = self._states.get(key)
            if state is not expected_state or state.last_selected != selected:
                return None
            state.verdict = verdict
            if ceiling is not None:
                previous = state.ceiling if now < state.ceiling_expires else None
                state.ceiling = min(ceiling, previous) if previous is not None else ceiling
                state.ceiling_expires = now + self._ttl
        if ceiling is not None:
            logger.warning(
                "model %s spilled %.2f GB to system RAM at %d tokens (%s); "
                "capping automatic context at %d",
                reading.model, reading.spilled_bytes / 1e9, observed,
                attributed_by, ceiling,
            )
        elif unfixable:
            logger.warning(
                "model %s spilled %.2f GB to system RAM and a smaller context "
                "cannot remove it; the weights exceed available VRAM",
                reading.model, reading.spilled_bytes / 1e9,
            )
        return verdict


def record_dispatched_context(path, payload, feedback_provider, is_cloud_model):
    """Attribute only an actual model request's serialized context window.

    The transport invokes this after opening the response, inside provider
    admission. Discovery, prewarm, invalid windows and cloud requests do not
    supply a local context observation. Advisory bookkeeping cannot fail a call.
    """
    if path not in {"/api/chat", "/api/generate"} or not isinstance(payload, Mapping):
        return
    model = payload.get("model")
    options = payload.get("options")
    context = options.get("num_ctx") if isinstance(options, Mapping) else None
    if not isinstance(model, str) or not model.strip():
        return
    try:
        if is_cloud_model(model):
            return
        feedback = feedback_provider()
        if feedback is not None:
            if type(context) is int and context > 0:
                feedback.note_selection(model, context)
            else:
                feedback.forget_selection(model)
    except Exception:
        logger.debug("residency dispatch attribution failed", exc_info=True)


__all__ = ["ResidencyFeedback", "ResidencyVerdict", "record_dispatched_context"]
