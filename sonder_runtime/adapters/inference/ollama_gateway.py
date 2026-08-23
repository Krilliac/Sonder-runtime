"""Ollama gateway adapter implementing the ModelGateway port (SPEC-3 Phase 3).

Centralizes model transport behind the port contract: tier resolution,
endpoint consent classification, bounded local retries, and timeout
handling all flow through the legacy transport (which already owns those
rules), while the port boundary enforces the operation-context consent
gates and maps driver exceptions into the domain error taxonomy — callers
never see ModelCallError, urllib, or HTTP details.

Acceptance properties (SPEC-3 Phase 3):
- Local retries remain bounded (delegated to the legacy transport).
- Remote-Ollama and hosted calls remain single-attempt (ditto).
- Consent gates cannot be bypassed through this lane: a cloud-classified
  tier is refused unless the caller's OperationContext explicitly allows
  cloud, before any request leaves the machine.
"""
from __future__ import annotations

import ipaddress
import importlib
import math
import time
import urllib.parse
from typing import Sequence

from ...application.context import OperationContext
from ...application.ports.model_gateway import (
    Embedding,
    ModelRequest,
    ModelResponse,
    optional_token_count,
    require_embedding_vector,
    require_model_text,
)
from ...application.ports.specialized_lifecycle import EmbeddingRequest
from ...application.ports.model_target import (
    ModelGenerateFactory,
    ModelSystemBuilder,
    ModelTarget,
    ModelTargetResolver,
)
from ...platform import context_policy
from ...platform.metrics import default_registry
from .telemetry import from_ollama
from ..model_transport import ModelCallError
ollama_endpoint = importlib.import_module(
    "sonder_runtime.adapters.inference.ollama_endpoint"
)
from ...domain.common.errors import (
    Cancelled,
    DeadlineExceeded,
    DependencyUnavailable,
    Forbidden,
    InternalFailure,
    InvalidInput,
)
from ...domain.model_capabilities import (
    GATEWAY_CAPABILITY_CHAT,
    GATEWAY_CAPABILITY_EMBEDDINGS,
    GATEWAY_CAPABILITY_TIERED_ROUTING,
)


# Static, provider-shape facts — never a live probe result.  Ollama resolves
# its model identity per request (a tier may select a different local or
# hosted model each call), so it advertises tiered routing rather than one
# fixed endpoint/model.
CAPABILITIES = frozenset({
    GATEWAY_CAPABILITY_CHAT,
    GATEWAY_CAPABILITY_EMBEDDINGS,
    GATEWAY_CAPABILITY_TIERED_ROUTING,
})


def _check_liveness(context: OperationContext) -> float | None:
    if context.expired:
        # Zero remaining time previously became timeout=None, turning an expired
        # request into an unbounded model call.
        raise DeadlineExceeded("operation deadline exceeded before model call")
    if context.cancellation is not None and context.cancellation.cancelled:
        raise Cancelled("operation cancelled before model call")
    return context.remaining_seconds


def _is_loopback(value: str) -> bool:
    """Whether an endpoint points at this machine.

    Deliberately duplicated from ``ollama_endpoint.is_loopback`` rather than
    imported. The architecture check forbids the adapters layer importing root
    modules, and it separately confines ``urllib`` to adapters -- so this cannot
    be shared via domain or platform either, and two copies are the only shape
    the layering permits. Kept behaviourally identical on purpose: if one
    changes, change both.
    """
    try:
        host = urllib.parse.urlparse(value or "").hostname
        if not host:
            return False
        if host.casefold().rstrip(".") == "localhost":
            return True
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _enforce_local_endpoint(base: str, context: OperationContext) -> None:
    if not _is_loopback(base) and not context.remote_ollama_allowed:
        # The adapter used to enforce hosted-tier consent but silently sent local-
        # tier prompts to a remotely configured Ollama endpoint without consent.
        raise Forbidden(
            "Ollama endpoint is non-loopback but this operation context does "
            "not allow remote Ollama"
        )


def _map_model_error(exc) -> Exception:
    kind = getattr(exc, "kind", "unknown")
    detail = getattr(exc, "detail", str(exc))
    if kind == "timeout":
        return DeadlineExceeded(detail)
    if kind == "cancelled":
        return Cancelled(detail)
    if getattr(exc, "transient", False) or kind in (
        "request", "protocol", "empty_response",
    ):
        return DependencyUnavailable(detail)
    return InternalFailure(detail)


class OllamaGateway:
    """ModelGateway over provider-owned Ollama dependencies.

    ``target_resolver``, ``system_builder``, and ``generate_factory`` are
    injected because tier policy and system context belong to the application
    composition boundary, not this transport adapter.
    """

    _default_target_resolver = None
    _default_generate_factory = None

    @classmethod
    def configure_default_providers(cls, *, target_resolver, generate_factory) -> None:
        """Install providers from an explicit composition boundary."""
        if target_resolver is None or generate_factory is None:
            raise ValueError("default Ollama providers must be callable")
        cls._default_target_resolver = target_resolver
        cls._default_generate_factory = generate_factory

    def __init__(
        self,
        *,
        target_resolver: ModelTargetResolver | None = None,
        system_builder: ModelSystemBuilder | None = None,
        generate_factory: ModelGenerateFactory | None = None,
        embedding_provider=None,
        session_num_ctx: int | None = None,
    ):
        self._target_resolver = target_resolver or type(self)._default_target_resolver
        self._system_builder = system_builder
        self._generate_factory = generate_factory or type(self)._default_generate_factory
        self._embedding_provider = embedding_provider
        self._session_num_ctx = (
            context_policy.default_requested()
            if session_num_ctx is None else int(session_num_ctx)
        )

    @property
    def capabilities(self) -> frozenset[str]:
        """Typed capability metadata; shape matches ``ProviderHealth.capabilities``."""
        return CAPABILITIES

    def generate(
        self, request: ModelRequest, context: OperationContext
    ) -> ModelResponse:
        if not (request.prompt or "").strip():
            raise InvalidInput("model request prompt is empty")
        if self._target_resolver is None or self._generate_factory is None:
            raise DependencyUnavailable(
                "Ollama gateway requires injected target and generate providers"
            )
        target = self._target_resolver(request.tier or "sonder", False)
        if not isinstance(target, ModelTarget):
            raise DependencyUnavailable("model target provider returned invalid target")
        model, cloud, tier_label = target.model, target.cloud, target.tier_label
        if tier_label == "cloud-disabled":
            raise Forbidden("cloud tiers are disabled on this runtime")
        if tier_label is None:
            raise InvalidInput("unknown model tier %r" % (request.tier,))
        if model is None:
            raise DependencyUnavailable(
                "the sonder:latest alias is not available; run setup_alias.py"
            )
        _enforce_local_endpoint(ollama_endpoint.normalize(), context)
        # The port-level consent gate: an explicitly cloud-classified tier
        # needs the caller's context to allow it — regardless of how the
        # request reached this lane (R: consent cannot be bypassed).
        if cloud and not context.cloud_allowed:
            raise Forbidden(
                "tier %r routes to a hosted model but this operation context "
                "does not allow cloud" % (request.tier,)
            )

        options = dict(request.options or {})
        timeout = _check_liveness(context)
        effective_system = request.system
        if not effective_system and self._system_builder is not None:
            effective_system = self._system_builder(
                "", False, "", model=model, cloud=cloud
            )
        gen = self._generate_factory(
            model,
            effective_system,
            float(options.get("temperature", 0.2)),
            int(options.get("num_predict", 1024)),
            int(options.get("num_ctx", self._session_num_ctx)),
            cloud=cloud,
            # Keep a positive sub-second deadline bounded.  ``int(0.5)`` used
            # to become zero and the legacy transport interpreted zero as no
            # timeout at all.
            timeout=max(1, math.ceil(timeout)) if timeout is not None else None,
            cancel_check=(
                (lambda: context.cancellation.cancelled)
                if context.cancellation is not None else None
            ),
        )
        started = time.monotonic()
        try:
            text = gen(request.prompt, list(request.history) or None)
        except ModelCallError as exc:
            raise _map_model_error(exc) from exc
        usage = getattr(gen, "last_usage", None)
        if usage is None:
            usage = {}
        if not isinstance(usage, dict):
            raise DependencyUnavailable("model provider returned an invalid usage object")
        response_meta = getattr(gen, "last_response_meta", None) or {}
        telemetry = from_ollama(response_meta)
        prompt_count = usage.get("tokens_in")
        if prompt_count is None:
            prompt_count = usage.get("prompt_eval_count")
        output_count = usage.get("tokens_out")
        if output_count is None:
            output_count = usage.get("eval_count")
        response = ModelResponse(
            text=require_model_text(text),
            model=model,
            tier=tier_label,
            duration_ms=int((time.monotonic() - started) * 1000),
            tokens_in=optional_token_count(prompt_count, "prompt token count"),
            tokens_out=optional_token_count(output_count, "completion token count"),
            telemetry=telemetry,
        )
        default_registry().observe_inference("ollama", telemetry)
        return response

    def embed(
        self, texts: Sequence[str], context: OperationContext
    ) -> Sequence[Embedding]:
        provider = self._embedding_provider
        if provider is None:
            provider = importlib.import_module(
                "sonder_runtime.adapters.embeddings"
            )

        _enforce_local_endpoint(getattr(provider, "BASE", ollama_endpoint.normalize()), context)
        # The composition root may publish a typed lifecycle provider.  Keep
        # the legacy module-shaped provider path intact for compatibility, but
        # make the live model path consume the typed contract when present.
        if getattr(provider, "provider_id", None) and callable(getattr(provider, "health", None)):
            typed = provider.embed(
                EmbeddingRequest(tuple(texts), getattr(provider, "EMBED_MODEL", "")),
                context,
            )
            return tuple(item for result in (typed,) for item in result.embeddings)
        results = []
        for text in texts:
            _check_liveness(context)
            embed = getattr(provider, "embed", provider)
            vector = embed(text)
            results.append(
                Embedding(
                    vector=require_embedding_vector(vector),
                    model=getattr(provider, "EMBED_MODEL", "nomic-embed-text"),
                )
            )
        return results
