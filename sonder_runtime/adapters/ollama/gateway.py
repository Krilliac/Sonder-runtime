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

import time
from typing import Sequence

from ...application.context import OperationContext
from ...application.ports.model_gateway import (
    Embedding,
    ModelRequest,
    ModelResponse,
)
from ...domain.common.errors import (
    Cancelled,
    DeadlineExceeded,
    DependencyUnavailable,
    Forbidden,
    InternalFailure,
    InvalidInput,
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
    """ModelGateway over the local Ollama transport."""

    def generate(
        self, request: ModelRequest, context: OperationContext
    ) -> ModelResponse:
        import model_transport
        import server

        if not (request.prompt or "").strip():
            raise InvalidInput("model request prompt is empty")
        model, cloud, _augment, tier_label = server._serve_target(
            request.tier or "sonder", None,
        )
        if tier_label == "cloud-disabled":
            raise Forbidden("cloud tiers are disabled on this runtime")
        if tier_label is None:
            raise InvalidInput("unknown model tier %r" % (request.tier,))
        if model is None:
            raise DependencyUnavailable(
                "the sonder:latest alias is not available; run setup_alias.py"
            )
        # The port-level consent gate: an explicitly cloud-classified tier
        # needs the caller's context to allow it — regardless of how the
        # request reached this lane (R: consent cannot be bypassed).
        if cloud and not context.cloud_allowed:
            raise Forbidden(
                "tier %r routes to a hosted model but this operation context "
                "does not allow cloud" % (request.tier,)
            )

        options = dict(request.options or {})
        timeout = context.remaining_seconds
        gen = server._make_generate(
            model,
            request.system or server._build_system("", False, ""),
            float(options.get("temperature", 0.2)),
            int(options.get("num_predict", 1024)),
            int(options.get("num_ctx", server.SESSION_NUM_CTX)),
            cloud=cloud,
            timeout=int(timeout) if timeout else None,
            cancel_check=(
                (lambda: context.cancellation.cancelled)
                if context.cancellation is not None else None
            ),
        )
        started = time.monotonic()
        try:
            text = gen(request.prompt, list(request.history) or None)
        except model_transport.ModelCallError as exc:
            raise _map_model_error(exc) from exc
        usage = getattr(gen, "last_usage", None) or {}
        return ModelResponse(
            text=text,
            model=model,
            tier=tier_label,
            duration_ms=int((time.monotonic() - started) * 1000),
            tokens_in=usage.get("prompt_eval_count"),
            tokens_out=usage.get("eval_count"),
        )

    def embed(
        self, texts: Sequence[str], context: OperationContext
    ) -> Sequence[Embedding]:
        import embeddings

        del context  # local-only embedding path; no consent surface
        results = []
        for text in texts:
            vector = embeddings.embed(text)
            if not vector:
                raise DependencyUnavailable(
                    "the local embedding model returned no vector"
                )
            results.append(
                Embedding(vector=tuple(vector), model=embeddings.EMBED_MODEL)
            )
        return results
