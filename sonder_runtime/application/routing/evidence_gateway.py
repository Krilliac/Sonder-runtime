"""Production request admission without making missing evidence a chat outage."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace

from sonder_runtime.domain.common.errors import DependencyUnavailable
from sonder_runtime.domain.routing.model_names import tagged_ollama_model

from .request_capabilities import (
    CAPABILITY_ROUTING_MODES,
    check_request_evidence,
    request_requirements,
)

logger = logging.getLogger(__name__)
IDENTITY_CACHE_TTL_SECONDS = 60.0
_MAX_IDENTITY_CACHE_ENTRIES = 64


class CapabilityEvidenceGateway:
    """Keep legacy text/embedding transport and gate explicit protocol features.

    Host identity comes from an adapter, never from a model claim or saved file.
    The resolved route is pinned for dispatch. Strict mode rechecks the cached
    identity after dispatch; changes are observable at TTL expiry or an evidence
    refresh, not necessarily during every generation. The physical Ollama pool
    separately checks the selected worker.
    """

    def __init__(self, gateway, evidence, identity_for, *, mode="advisory",
                 identity_key_for=None, clock=time.monotonic):
        if mode not in CAPABILITY_ROUTING_MODES:
            raise ValueError("capability routing must be advisory, strict or off")
        self._gateway = gateway
        self._evidence = evidence
        self._identity_for = identity_for
        self._mode = mode
        self._identity_key_for = identity_key_for
        self._clock = clock
        self._identity_cache = {}
        self._identity_lock = threading.Lock()

    @property
    def capabilities(self):
        return getattr(self._gateway, "capabilities", frozenset())

    def resolve_route(self, request, context):
        return self._gateway.resolve_route(request, context)

    def generate(self, request, context):
        if self._mode == "off":
            return self._gateway.generate(request, context)
        payload = dict(request.options)
        payload["messages"] = [*request.history, {"role": "user", "content": request.prompt}]
        payload["system"] = request.system
        required = request_requirements(payload)
        if not required:
            return self._gateway.generate(request, context)
        resolver = getattr(self._gateway, "resolve_route", None)
        if not callable(resolver):
            if self._mode == "strict":
                raise DependencyUnavailable("capability route refused: backend_identity_missing")
            logger.info("capability route unverified: backend_identity_missing")
            return self._gateway.generate(request, context)
        route = resolver(request, context)
        identity = None
        if self._mode == "strict" or (
            self._evidence is not None
            and self._evidence.has_fresh_failure(route.provider_id, route.model, required)
        ):
            identity = self._observe_identity(route, payload)
        allowed, reason = check_request_evidence(
            self._evidence, route.model, required,
            backend=route.provider_id, identity=identity, mode=self._mode,
        )
        if not allowed:
            if self._mode == "strict":
                raise DependencyUnavailable(f"capability route refused: {reason}")
            # This gateway has one resolved model route. The physical pool can
            # still select an alternative worker without changing that route.
            logger.warning("capability fallback_used: retaining configured route; %s", reason)
        else:
            logger.info("capability route: %s", reason)
        response = self._gateway.generate(replace(request, _resolved_route=route), context)
        if self._mode == "strict":
            current_identity = self._observe_identity(route, payload)
            if current_identity != identity:
                raise DependencyUnavailable("backend identity changed during model dispatch")
        actual_model, expected_model = response.model, route.model
        if route.provider_id == "ollama":
            actual_model = tagged_ollama_model(actual_model)
            expected_model = tagged_ollama_model(expected_model)
        if actual_model != expected_model:
            if self._mode == "strict":
                raise DependencyUnavailable("backend answered with a different model")
            logger.warning("backend answered with a different model; retaining advisory response")
        return response

    def _observe_identity(self, route, payload):
        try:
            if self._identity_key_for is not None:
                key = self._identity_key_for(route, payload)
            else:
                # Injected gateways without adapter configuration still isolate
                # providers/models and explicitly requested context windows.
                options = payload.get("options") or {}
                key = (route.provider_id, route.model, getattr(route, "origin", None),
                       getattr(route, "cloud", False), options.get("num_ctx", payload.get("num_ctx")))
            with self._identity_lock:
                revision = self._evidence.revision if self._evidence is not None else None
                now = self._clock()
                cached = self._identity_cache.get(key)
                if cached is not None:
                    observed_at, saved_revision, identity = cached
                    if saved_revision == revision and 0 <= now - observed_at < IDENTITY_CACHE_TTL_SECONDS:
                        return identity
                try:
                    identity = self._identity_for(route, payload)
                except (AttributeError, TypeError, ValueError, OSError, RuntimeError):
                    identity = None
                if len(self._identity_cache) >= _MAX_IDENTITY_CACHE_ENTRIES:
                    self._identity_cache.pop(next(iter(self._identity_cache)))
                # Cache unavailable observations too, so outages do not restore
                # per-decision HTTP timeouts. A concurrent refresh invalidates
                # this entry on the next lookup via the pre-observation revision.
                self._identity_cache[key] = (self._clock(), revision, identity)
                return identity
        except (AttributeError, TypeError, ValueError, OSError, RuntimeError):
            return None

    def embed(self, texts, context):
        # Embeddings are not certified by this chat protocol battery. Preserve
        # the established embedding path; explicit strict role routing retains
        # its separate EMBEDDING requirement.
        return self._gateway.embed(texts, context)
