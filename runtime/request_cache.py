"""Bounded request/result cache for deterministic-eligible chat generation.

A stored response may be replayed only when the whole turn is a pure function
of the bound identity.  ``eligible`` is therefore a default-deny gate: every
axis must be asserted by the caller, and any unknown or unproven surface stays
uncached.  The first slice admits exactly one shape of turn:

- a **local** target on a **loopback** Ollama endpoint (never cloud, and
  never a configured remote OLLAMA_HOST — the HTTP layer asserts locality
  from ``ollama_endpoint.is_loopback``; a local call also never uses the
  cloud availability fallback, so the bound model is the serving model);
- **greedy decoding** (temperature 0.0) — the only sampling mode where
  replaying a stored response is equivalent to generating again;
- the **non-learning, non-augmented** route — the learning route captures
  interactions and injects retrieved memory, both of which are side effects a
  replay would silently skip;
- a **request-owner scope** — an opaque per-principal digest supplied by the
  HTTP layer, so two accounts can never share entries even for byte-identical
  requests;
- **no** streaming, structured-output, tool, web, slash, or agent surface
  (the serve layer routes those before consulting this module, and denies the
  first two explicitly).

Identity binds the normalized prompt, history, and system prompt, the exact
resolved model and tier, every generation option actually sent to the backend
(including the live env performance knobs), the request-owner scope, and the
cloud-consent policy state.  Any model, configuration, or policy change that
can alter generation output changes one of those inputs, so stale entries
stop matching immediately and age out through the explicit TTL and LRU cap.

Privacy: entries live only in process memory — nothing is written to disk —
and the observable surface (``stats``, the serve layer's receipt field and
metric) is bounded counters and closed-set labels, never prompt or answer
text.  Keys are SHA-256 digests, so no content is recoverable from them.

Environment knobs: ``SONDER_REQUEST_CACHE=1`` opts in (default off);
``SONDER_REQUEST_CACHE_TTL`` seconds per entry (default 300, max 86400);
``SONDER_REQUEST_CACHE_MAX`` entry cap (default 256, max 4096).
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from collections import OrderedDict
from time import monotonic as _monotonic

_KEY_DOMAIN = "sonder-request-cache-v1"

_DEFAULT_TTL_SECONDS = 300.0
_MAX_TTL_SECONDS = 86_400.0
_DEFAULT_MAX_ENTRIES = 256
_MAX_MAX_ENTRIES = 4096

_lock = threading.Lock()
_entries: OrderedDict[str, tuple[float, str]] = OrderedDict()
_counters = {"hit": 0, "miss": 0, "store": 0, "evict": 0, "expire": 0}


def _now() -> float:
    """Monotonic clock; a seam so tests can drive TTL expiry."""
    return _monotonic()


def enabled() -> bool:
    return os.environ.get("SONDER_REQUEST_CACHE", "").strip().lower() in (
        "1", "true", "yes", "on"
    )


def ttl_seconds() -> float:
    raw = os.environ.get("SONDER_REQUEST_CACHE_TTL", "").strip()
    if not raw:
        return _DEFAULT_TTL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_TTL_SECONDS
    if not (value > 0):  # also rejects NaN
        return _DEFAULT_TTL_SECONDS
    return min(_MAX_TTL_SECONDS, value)


def max_entries() -> int:
    raw = os.environ.get("SONDER_REQUEST_CACHE_MAX", "").strip()
    if not raw:
        return _DEFAULT_MAX_ENTRIES
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_MAX_ENTRIES
    return max(1, min(_MAX_MAX_ENTRIES, value))


def eligible(*, scope, cloud, temperature, learning, augmented,
             streaming=False, structured=False, remote_endpoint=True) -> bool:
    """Default-deny admission gate for one generation turn.

    Callers must assert every axis explicitly; anything unknown, unparseable,
    or unproven is a denial.  Returning False means the turn is generated
    fresh with no cache consultation at all.

    ``remote_endpoint`` covers the serving endpoint's locality: a non-cloud
    target can still be served by a remote Ollama endpoint (a configured
    non-loopback OLLAMA_HOST), and the cache is promised to be local-only.
    The axis defaults to remote, so a caller that has not proven loopback —
    via the authoritative ollama_endpoint.is_loopback — is denied.
    """
    if not enabled():
        return False
    if not isinstance(scope, str) or not scope:
        return False
    if cloud or remote_endpoint or learning or augmented or streaming or structured:
        return False
    try:
        temp = float(temperature)
    except (TypeError, ValueError):
        return False
    # Only greedy decoding is replay-equivalent to fresh generation.  Any
    # sampled turn (including the serve default of 0.2) stays uncached.
    if temp != 0.0:
        return False
    return True


def identity_key(*, scope, model, model_revision="", tier, system, prompt, history, options,
                 cloud_allowed) -> str:
    """Opaque SHA-256 identity binding every generation-relevant input."""
    normalized_history = [
        {
            "role": str((message or {}).get("role", "")),
            "content": str((message or {}).get("content", "")),
        }
        for message in (history or [])
    ]
    material = json.dumps(
        {
            "v": _KEY_DOMAIN,
            "scope": str(scope),
            "model": str(model),
            # A mutable Ollama tag is not an identity.  The serving layer
            # supplies the catalog digest and refuses cache admission when it
            # cannot establish one.
            "model_revision": str(model_revision),
            "tier": str(tier),
            "system": str(system),
            "prompt": str(prompt),
            "history": normalized_history,
            "options": options or {},
            "cloud_allowed": bool(cloud_allowed),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def get(key):
    """Return the stored response for ``key``, or None on miss/expiry."""
    if not key:
        return None
    now = _now()
    with _lock:
        entry = _entries.get(key)
        if entry is None:
            _counters["miss"] += 1
            return None
        expires, response = entry
        if now >= expires:
            del _entries[key]
            _counters["expire"] += 1
            _counters["miss"] += 1
            return None
        _entries.move_to_end(key)
        _counters["hit"] += 1
        return response


def put(key, response) -> bool:
    """Store one successful response; refuse anything that is not one."""
    if not key or not isinstance(response, str) or not response.strip():
        return False
    # Success-only by contract.  The generation layer already skips storing
    # raised model errors; this refuses the legacy in-band error shape too.
    if response.startswith("ERROR"):
        return False
    limit = max_entries()
    with _lock:
        _entries[key] = (_now() + ttl_seconds(), response)
        _entries.move_to_end(key)
        _counters["store"] += 1
        while len(_entries) > limit:
            _entries.popitem(last=False)
            _counters["evict"] += 1
    return True


def clear() -> None:
    """Drop every entry (explicit invalidation hook)."""
    with _lock:
        _entries.clear()


def stats() -> dict:
    """Bounded, content-free counters for diagnostics and tests."""
    with _lock:
        snapshot = dict(_counters)
        snapshot["entries"] = len(_entries)
    return snapshot


def reset_for_tests() -> None:
    with _lock:
        _entries.clear()
        for name in _counters:
            _counters[name] = 0
