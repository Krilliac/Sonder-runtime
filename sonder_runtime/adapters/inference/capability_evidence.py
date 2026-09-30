"""Production evidence location and current, local Ollama identity discovery."""
from __future__ import annotations

import os
from pathlib import Path

from sonder_runtime.application.routing.backend_conformance import (
    RecentCapabilityEvidence,
)
from sonder_runtime.application.routing.request_capabilities import (
    CAPABILITY_ROUTING_MODES,
)
from sonder_runtime.platform import context_policy, paths

EVIDENCE_FILENAME = "capability_evidence.json"


def capability_routing_mode(environment=None) -> str:
    """Read the production compatibility flag; default to availability."""
    env = os.environ if environment is None else environment
    mode = str(env.get("SONDER_CAPABILITY_ROUTING", "advisory")).strip().lower() or "advisory"
    if mode not in CAPABILITY_ROUTING_MODES:
        raise ValueError("SONDER_CAPABILITY_ROUTING must be advisory, strict or off")
    return mode


def load_production_evidence(home=None) -> RecentCapabilityEvidence:
    """Opening a missing/corrupt store is safe and never creates state at startup."""
    root = Path(home).expanduser() if home else paths.default_home()
    return RecentCapabilityEvidence(root / EVIDENCE_FILENAME)


def identity_context_tokens(payload=None):
    options = (payload or {}).get("options") or {}
    return options.get("num_ctx", (payload or {}).get(
        "num_ctx", context_policy.default_requested(),
    ))


def request_identity_key(route, payload):
    """Bind the gateway cache to the same origin and context as observation."""
    from .ollama_endpoint import normalize

    origin = normalize() if route.provider_id == "ollama" and not route.cloud else None
    return (route.provider_id, route.cloud, origin, route.model, identity_context_tokens(payload))


def ollama_identity(origin: str, model: str, payload=None):
    """Re-observe the serving identity; never trust the persisted identity as current.

    No inference is done here. Failed/remote identity discovery remains unknown.
    The concrete adapter disables redirects and proxies and permits loopback only.
    """
    from .ollama_conformance import OllamaConformanceProbe

    try:
        return OllamaConformanceProbe(
            origin, model, context_tokens=identity_context_tokens(payload),
        ).read_identity()
    except (OSError, ValueError, TypeError, RuntimeError):
        return None


def request_identity(route, payload):
    """The resolved route must be local Ollama; other providers stay unmeasured."""
    if route.provider_id != "ollama" or route.cloud:
        return None
    from .ollama_endpoint import normalize

    return ollama_identity(normalize(), route.model, payload)
