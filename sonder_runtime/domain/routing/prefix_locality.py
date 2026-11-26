"""Pure bounded prefix-locality tracking and completion-cost endpoint scoring.

Shared by endpoint selectors (``SonderInferenceGateway`` and
``OllamaWorkerPool``) so locality accounting and queue/prefill/decode tradeoffs
stay consistent without unbounded state growth or layer-boundary leaks.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
from collections import OrderedDict
from dataclasses import dataclass

DEFAULT_MAX_LOCALITY_ENTRIES = 512
DEFAULT_LOCALITY_TTL_SECONDS = 900.0
DEFAULT_LOCALITY_HALF_LIFE_SECONDS = 120.0
_DEFAULT_INFERRED_CONFIDENCE = 0.35
_FIRST_WRITE_CONFIDENCE = 0.45
_MISS_DECAY_FACTOR = 0.35


@dataclass(frozen=True)
class PrefixLocalityKey:
    """Canonical key ensuring model or adapter/preprocessing mismatch never hits."""

    endpoint: str
    model_identity: str
    adapter_key: str = ""
    prefix_key: str = ""

    def as_tuple(self) -> tuple[str, str, str, str]:
        return (
            self.endpoint,
            self.model_identity,
            self.adapter_key,
            self.prefix_key,
        )


@dataclass(frozen=True)
class LocalityMatch:
    """Decayed cache-overlap estimate for one candidate endpoint."""

    effective_cached_tokens: float
    hit_ratio: float
    confidence: float
    decay: float
    measured: bool
    age_seconds: float
    estimated_restore_ms: float


@dataclass(frozen=True)
class CandidateCompletionEstimate:
    """Predicted request completion latency decomposition for one endpoint."""

    endpoint: str
    total_cost_ms: float
    projected_queue_delay_ms: float
    uncached_prompt_work_ms: float
    expected_decode_work_ms: float
    restore_cost_ms: float
    locality_bonus_ms: float
    effective_cached_tokens: float
    warm_hit: bool


@dataclass
class _LocalityEntry:
    warm_tokens: float
    last_prompt_tokens: int
    confidence: float
    measured: bool
    last_observed_at: float
    hits: int = 0
    observations: int = 0
    consecutive_misses: int = 0


def digest_key_parts(namespace: str, parts: object) -> str:
    """Return a short deterministic SHA-256 tag without leaking prompt text."""
    encoded = json.dumps(
        parts,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:24]
    return f"{namespace}:{digest}"


class PrefixLocalityTable:
    """Thread-safe, LRU-bounded per-endpoint prefix locality observation table."""

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_MAX_LOCALITY_ENTRIES,
        ttl_seconds: float = DEFAULT_LOCALITY_TTL_SECONDS,
        half_life_seconds: float = DEFAULT_LOCALITY_HALF_LIFE_SECONDS,
    ) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
            raise ValueError("max_entries must be a positive integer")
        ttl = float(ttl_seconds)
        half_life = float(half_life_seconds)
        if not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("ttl_seconds must be positive and finite")
        if not math.isfinite(half_life) or half_life < 0:
            raise ValueError("half_life_seconds must be non-negative and finite")
        self._max_entries = max_entries
        self._ttl_seconds = ttl
        self._half_life_seconds = half_life
        self._lock = threading.RLock()
        self._entries: OrderedDict[tuple[str, str, str, str], _LocalityEntry] = OrderedDict()
        # Bounded per-(endpoint, prefix_key) compatibility tracker so lookups on
        # a mismatched model/adapter can record incompatible_skips without scanning.
        self._recent_compat: OrderedDict[tuple[str, str], tuple[str, str]] = OrderedDict()
        self._observations = 0
        self._lookups = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._incompatible_skips = 0

    @property
    def max_entries(self) -> int:
        return self._max_entries

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def _decay_at(self, age_seconds: float) -> float:
        if age_seconds >= self._ttl_seconds:
            return 0.0
        if self._half_life_seconds == 0:
            return 1.0
        return float(0.5 ** (age_seconds / self._half_life_seconds))

    def observe(
        self,
        *,
        endpoint: str,
        model_identity: str,
        adapter_key: str,
        prefix_key: str,
        now: float,
        prompt_tokens: int | None = None,
        cached_tokens: int | None = None,
        estimated_prompt_tokens: int | None = None,
    ) -> None:
        """Record one completed call's prefix-locality observation."""
        clean_ep = str(endpoint or "").strip()
        clean_model = str(model_identity or "").strip()
        clean_prefix = str(prefix_key or "").strip()
        clean_adapter = str(adapter_key or "").strip()
        if not clean_ep or not clean_model or not clean_prefix:
            return
        if not math.isfinite(now):
            return
        key = (clean_ep, clean_model, clean_adapter, clean_prefix)
        valid_prompt = (
            prompt_tokens
            if isinstance(prompt_tokens, int) and not isinstance(prompt_tokens, bool) and prompt_tokens >= 0
            else None
        )
        valid_cached = (
            cached_tokens
            if isinstance(cached_tokens, int) and not isinstance(cached_tokens, bool) and cached_tokens >= 0
            else None
        )
        valid_est = (
            estimated_prompt_tokens
            if isinstance(estimated_prompt_tokens, int)
            and not isinstance(estimated_prompt_tokens, bool)
            and estimated_prompt_tokens >= 0
            else 0
        )
        observed_prompt = valid_prompt if valid_prompt is not None else valid_est

        with self._lock:
            self._observations += 1
            compat_key = (clean_ep, clean_prefix)
            self._recent_compat[compat_key] = (clean_model, clean_adapter)
            self._recent_compat.move_to_end(compat_key)
            while len(self._recent_compat) > self._max_entries:
                self._recent_compat.popitem(last=False)

            existing = self._entries.get(key)
            if valid_cached is not None and valid_cached > 0:
                new_warm = float(valid_cached)
                if existing is not None:
                    age = max(0.0, now - existing.last_observed_at)
                    decayed_prev = existing.warm_tokens * self._decay_at(age)
                    blended = 0.65 * new_warm + 0.35 * max(decayed_prev, 0.0)
                    existing.warm_tokens = max(new_warm if decayed_prev == 0 else blended, 1.0)
                    existing.last_prompt_tokens = max(observed_prompt, valid_cached)
                    existing.confidence = 1.0
                    existing.measured = True
                    existing.last_observed_at = now
                    existing.hits += 1
                    existing.observations += 1
                    existing.consecutive_misses = 0
                    self._entries.move_to_end(key)
                    return
                entry = _LocalityEntry(
                    warm_tokens=new_warm,
                    last_prompt_tokens=max(observed_prompt, valid_cached),
                    confidence=1.0,
                    measured=True,
                    last_observed_at=now,
                    hits=1,
                    observations=1,
                    consecutive_misses=0,
                )
            elif valid_cached == 0:
                if existing is not None and existing.observations >= 1:
                    existing.consecutive_misses += 1
                    if existing.consecutive_misses >= 2:
                        self._entries.pop(key, None)
                        return
                    existing.confidence *= _MISS_DECAY_FACTOR
                    existing.last_observed_at = now
                    existing.observations += 1
                    if observed_prompt > 0:
                        existing.last_prompt_tokens = observed_prompt
                    self._entries.move_to_end(key)
                    return
                if observed_prompt <= 0:
                    return
                entry = _LocalityEntry(
                    warm_tokens=float(observed_prompt),
                    last_prompt_tokens=observed_prompt,
                    confidence=_FIRST_WRITE_CONFIDENCE,
                    measured=False,
                    last_observed_at=now,
                    hits=0,
                    observations=1,
                    consecutive_misses=0,
                )
            else:
                if observed_prompt <= 0:
                    return
                if existing is not None:
                    existing.warm_tokens = max(existing.warm_tokens, float(observed_prompt))
                    existing.last_prompt_tokens = observed_prompt
                    existing.last_observed_at = now
                    existing.observations += 1
                    if not existing.measured:
                        existing.confidence = min(0.5, max(existing.confidence, _DEFAULT_INFERRED_CONFIDENCE))
                    self._entries.move_to_end(key)
                    return
                entry = _LocalityEntry(
                    warm_tokens=float(observed_prompt),
                    last_prompt_tokens=observed_prompt,
                    confidence=_DEFAULT_INFERRED_CONFIDENCE,
                    measured=False,
                    last_observed_at=now,
                    hits=0,
                    observations=1,
                    consecutive_misses=0,
                )

            self._entries[key] = entry
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
                self._evictions += 1

    def lookup(
        self,
        *,
        endpoint: str,
        model_identity: str,
        adapter_key: str,
        prefix_key: str,
        now: float,
        estimated_prompt_tokens: int = 0,
        restore_ms_per_token: float = 0.0,
        restore_penalty_ms: float = 0.0,
    ) -> LocalityMatch:
        """Look up decayed warm-cache overlap for the exact compatibility key."""
        clean_ep = str(endpoint or "").strip()
        clean_model = str(model_identity or "").strip()
        clean_prefix = str(prefix_key or "").strip()
        clean_adapter = str(adapter_key or "").strip()
        if not clean_ep or not clean_model or not clean_prefix or not math.isfinite(now):
            return LocalityMatch(0.0, 0.0, 0.0, 0.0, False, 0.0, 0.0)

        key = (clean_ep, clean_model, clean_adapter, clean_prefix)
        with self._lock:
            self._lookups += 1
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                compat = self._recent_compat.get((clean_ep, clean_prefix))
                if compat is not None and compat != (clean_model, clean_adapter):
                    self._incompatible_skips += 1
                return LocalityMatch(0.0, 0.0, 0.0, 0.0, False, 0.0, 0.0)

            age = max(0.0, now - entry.last_observed_at)
            decay = self._decay_at(age)
            if decay <= 0.0 or entry.confidence <= 0.0 or entry.warm_tokens <= 0.0:
                self._entries.pop(key, None)
                self._misses += 1
                return LocalityMatch(0.0, 0.0, 0.0, 0.0, False, age, 0.0)

            self._entries.move_to_end(key)
            compat_key = (clean_ep, clean_prefix)
            if compat_key in self._recent_compat:
                self._recent_compat.move_to_end(compat_key)
            self._hits += 1
            cap = float(estimated_prompt_tokens) if estimated_prompt_tokens > 0 else entry.warm_tokens
            effective = min(cap, entry.warm_tokens) * entry.confidence * decay
            denom = cap if cap > 0 else max(entry.warm_tokens, 1.0)
            hit_ratio = min(1.0, effective / denom)
            restore_cost = (
                max(0.0, float(restore_penalty_ms)) + effective * max(0.0, float(restore_ms_per_token))
                if effective > 0.0
                else 0.0
            )
            return LocalityMatch(
                effective_cached_tokens=effective,
                hit_ratio=hit_ratio,
                confidence=entry.confidence,
                decay=decay,
                measured=entry.measured,
                age_seconds=age,
                estimated_restore_ms=restore_cost,
            )

    def summary(self) -> dict[str, int]:
        """Bounded integer counters for observability (no prompts or URLs)."""
        with self._lock:
            return {
                "entries": len(self._entries),
                "max_entries": self._max_entries,
                "observations": self._observations,
                "lookups": self._lookups,
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "incompatible_skips": self._incompatible_skips,
            }


def score_endpoint_completion(
    *,
    endpoint: str,
    inflight: int,
    max_inflight: int,
    decode_ms_per_token: float,
    prompt_ms_per_token: float,
    estimated_prompt_tokens: int,
    expected_decode_tokens: int,
    match: LocalityMatch,
    observed_request_ms: float | None = None,
) -> CandidateCompletionEstimate:
    """Estimate completion cost combining queue delay, uncached prefill, decode, and restore."""
    safe_inflight = max(0, int(inflight))
    safe_max = max(1, int(max_inflight))
    decode_rate = max(0.01, float(decode_ms_per_token))
    prompt_rate = max(0.01, float(prompt_ms_per_token))
    prompt_tokens = max(0, int(estimated_prompt_tokens))
    decode_tokens = max(1, int(expected_decode_tokens))

    decode_contention = 1.0 + 0.25 * (safe_inflight / safe_max)
    expected_decode_ms = decode_tokens * decode_rate * decode_contention
    cold_prompt_ms = prompt_tokens * prompt_rate
    active_work_ms = (
        max(1.0, float(observed_request_ms))
        if observed_request_ms is not None and math.isfinite(observed_request_ms) and observed_request_ms > 0
        else max(1.0, decode_tokens * decode_rate + cold_prompt_ms * 0.5)
    )
    projected_queue_ms = (
        (safe_inflight / safe_max) * safe_inflight * active_work_ms
        if safe_inflight > 0
        else 0.0
    )

    capped_cached = min(float(prompt_tokens), max(0.0, match.effective_cached_tokens)) if prompt_tokens > 0 else 0.0
    gross_saving_ms = capped_cached * prompt_rate
    restore_ms = max(0.0, match.estimated_restore_ms)
    net_saving_ms = gross_saving_ms - restore_ms

    if capped_cached > 0.0 and net_saving_ms > 0.0:
        uncached_prompt_ms = max(0.0, float(prompt_tokens) - capped_cached) * prompt_rate
        used_restore_ms = restore_ms
        bonus_ms = net_saving_ms
        used_cached = capped_cached
        warm_hit = True
    else:
        uncached_prompt_ms = cold_prompt_ms
        used_restore_ms = 0.0
        bonus_ms = 0.0
        used_cached = 0.0
        warm_hit = False

    total_ms = projected_queue_ms + uncached_prompt_ms + expected_decode_ms + used_restore_ms
    return CandidateCompletionEstimate(
        endpoint=endpoint,
        total_cost_ms=round(total_ms, 3),
        projected_queue_delay_ms=round(projected_queue_ms, 3),
        uncached_prompt_work_ms=round(uncached_prompt_ms, 3),
        expected_decode_work_ms=round(expected_decode_ms, 3),
        restore_cost_ms=round(used_restore_ms, 3),
        locality_bonus_ms=round(bonus_ms, 3),
        effective_cached_tokens=round(used_cached, 2),
        warm_hit=warm_hit,
    )


__all__ = [
    "DEFAULT_LOCALITY_HALF_LIFE_SECONDS",
    "DEFAULT_LOCALITY_TTL_SECONDS",
    "DEFAULT_MAX_LOCALITY_ENTRIES",
    "CandidateCompletionEstimate",
    "LocalityMatch",
    "PrefixLocalityKey",
    "PrefixLocalityTable",
    "digest_key_parts",
    "score_endpoint_completion",
]
