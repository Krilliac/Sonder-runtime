"""Route the legacy HTTP chat path through the ModelGateway for non-Ollama rungs.

``server.py``'s user-facing chat (``POST /v1/chat/completions``) builds
Ollama-shaped ``/api/chat`` payloads.  When the rung's tier is bound to a
provider other than Ollama, the local branch of ``_chat_request`` hands that
payload to this module instead of posting it to Ollama.  This module:

* holds the per-rung provider in a ContextVar (here, not in ``server.py``, so a
  live reload of ``server.py`` cannot orphan an in-flight binding);
* converts the Ollama payload into a provider-neutral ``ModelRequest`` and
  refuses what the gateway cannot carry (decoder schemas, thinking, native
  tools, images);
* calls ``model_gateway.generate`` with the rung binding cleared, so the Ollama
  gateway (itself built on ``_chat_request``) and gateway offloads made during
  the call are never intercepted again;
* shapes the ``ModelResponse`` back into the Ollama reply the legacy caller
  already understands, and classifies domain errors into the transport
  failure the ``server.py`` hook raises.

It stays pure: no environment, no I/O, no adapter imports.  ``ModelCallError``
lives in an adapter, so the hook in ``server.py`` builds it from
``BridgeFailure``.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, Mapping

from ...domain.common.errors import (
    Cancelled,
    CapacityExceeded,
    DeadlineExceeded,
    DependencyUnavailable,
    Forbidden,
    InvalidInput,
    SonderError,
)
from ..context import OperationContext
from ..ports.model_gateway import ModelRequest, ModelResponse

LEGACY_PROVIDER = "ollama"
# The ModelCallError kind for a provider that is down.  It is terminal: the
# escalation ladder must not treat a missing provider as a weak answer.
PROVIDER_UNAVAILABLE_KIND = "provider_unavailable"
# A feature the bound provider cannot carry is the caller's error (400); a
# stronger rung would only hide it, so it is terminal as well.
UNSUPPORTED_FEATURE_KIND = "unsupported_feature"
_FORWARDED_OPTIONS = ("temperature", "num_predict", "num_ctx")
_ROLES = frozenset({"system", "user", "assistant", "tool"})

_RUNG: ContextVar["RungBinding | None"] = ContextVar(
    "sonder_chat_rung_binding", default=None,
)
_DEGRADATIONS: ContextVar[list[str] | None] = ContextVar(
    "sonder_chat_turn_degradations", default=None,
)


class UnsupportedProviderFeature(InvalidInput):
    """The request needs an Ollama-only feature the bound provider lacks."""


def provider_for_tier(tier_label: object, bindings: object) -> str:
    """The provider a rung uses, per the contract's routing rules.

    The five provider tiers follow ``bindings.tier_providers``; the default
    ``sonder`` route follows ``default_generation_provider``; exact
    ``model:*`` pins and hosted/cloud tiers always stay on Ollama.
    """
    tier = str(tier_label or "").strip().lower()
    tiers = getattr(bindings, "tier_providers", None) or {}
    if tier in tiers:
        return str(tiers[tier])
    if tier == "sonder":
        return str(getattr(bindings, "default_generation_provider", LEGACY_PROVIDER))
    return LEGACY_PROVIDER


def is_bridged(provider: object) -> bool:
    return isinstance(provider, str) and bool(provider) and provider != LEGACY_PROVIDER


@dataclass(slots=True)
class RungBinding:
    """The non-Ollama provider and tier serving the current escalation rung.

    ``served_model`` is the model the provider reports it used for the rung's
    last successful call (``ModelResponse.model``), for the turn receipt.
    """

    provider: str
    tier: str
    served_model: str | None = None


@contextmanager
def bind_rung(provider: str | None, tier: str) -> Iterator[RungBinding | None]:
    """Bind the rung's provider for the enclosed block; Ollama binds nothing."""
    binding = RungBinding(provider, str(tier or "sonder")) if is_bridged(provider) else None
    token = _RUNG.set(binding)
    try:
        yield binding
    finally:
        _RUNG.reset(token)


@contextmanager
def suspend_rung() -> Iterator[None]:
    """Clear the rung binding (gateway calls and offloads take their own route)."""
    token = _RUNG.set(None)
    try:
        yield None
    finally:
        _RUNG.reset(token)


def active_rung() -> RungBinding | None:
    """The non-Ollama rung bound on this thread, or None for the legacy path."""
    return _RUNG.get()


@contextmanager
def degradation_scope() -> Iterator[list[str]]:
    """Collect Ollama-only steps a non-Ollama turn had to run without."""
    notes: list[str] = []
    token = _DEGRADATIONS.set(notes)
    try:
        yield notes
    finally:
        _DEGRADATIONS.reset(token)


def record_degradation(step: str) -> bool:
    """Record a degraded step for the current turn; False when none is bound."""
    notes = _DEGRADATIONS.get()
    if notes is None:
        return False
    if step not in notes:
        notes.append(step)
    return True


def _text(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise UnsupportedProviderFeature(
            "%s must be text for the bound provider" % where
        )
    return value


def model_request_from_ollama_payload(
    payload: Mapping[str, object], *, tier: str,
) -> ModelRequest:
    """Convert one Ollama ``/api/chat`` payload into a provider-neutral request."""
    if not isinstance(payload, Mapping):
        raise InvalidInput("chat payload must be an object")
    if payload.get("format") is not None:
        raise UnsupportedProviderFeature(
            "response_format/schema decoding is only available on Ollama tiers"
        )
    if payload.get("think") is True:
        raise UnsupportedProviderFeature(
            "model thinking is only available on Ollama tiers"
        )
    if payload.get("tools"):
        raise UnsupportedProviderFeature(
            "native tool calls are only available on Ollama tiers"
        )
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise InvalidInput("chat payload has no messages")
    system_parts: list[str] = []
    turns: list[dict[str, str]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            raise InvalidInput("chat message %d is not an object" % index)
        if message.get("images") or message.get("tool_calls"):
            raise UnsupportedProviderFeature(
                "images and tool calls are only available on Ollama tiers"
            )
        role = message.get("role")
        if role not in _ROLES:
            raise InvalidInput("chat message %d has an unsupported role" % index)
        content = _text(message.get("content", ""), "message content")
        if role == "system" and not turns:
            if content.strip():
                system_parts.append(content)
            continue
        turns.append({"role": str(role), "content": content})
    if not turns or turns[-1]["role"] != "user":
        raise InvalidInput("chat payload must end with a user message")
    prompt = turns.pop()["content"]
    raw_options = payload.get("options")
    options = {}
    if isinstance(raw_options, Mapping):
        for key in _FORWARDED_OPTIONS:
            value = raw_options.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if key != "temperature" and int(value) <= 0:
                continue
            options[key] = int(value) if key != "temperature" else float(value)
    return ModelRequest(
        prompt=prompt,
        tier=str(tier or "sonder"),
        system="\n\n".join(system_parts),
        history=tuple(turns),
        options=options,
    )


def ollama_shape(response: ModelResponse) -> dict[str, object]:
    """Shape a ModelResponse as the Ollama reply legacy callers consume."""
    shaped: dict[str, object] = {
        "model": response.model,
        "message": {"role": "assistant", "content": response.text},
        "done": True,
        "done_reason": "stop",
    }
    if response.tokens_in is not None:
        shaped["prompt_eval_count"] = response.tokens_in
    if response.tokens_out is not None:
        shaped["eval_count"] = response.tokens_out
    return shaped


@dataclass(frozen=True, slots=True)
class BridgeFailure:
    """A classified provider failure; ``server.py`` turns it into ModelCallError."""

    kind: str
    detail: str
    status: int | None = None
    transient: bool = False


def classify_failure(error: SonderError, *, provider: str) -> BridgeFailure:
    """Map a domain error from a non-Ollama provider to a transport failure.

    ``DependencyUnavailable`` is a 503 that ends the turn (never an
    escalation); unsupported features are the caller's 400.
    """
    detail = str(error) or error.code
    if isinstance(error, DependencyUnavailable):
        return BridgeFailure(
            PROVIDER_UNAVAILABLE_KIND,
            "provider %s is unavailable: %s" % (provider, detail),
            status=503,
        )
    if isinstance(error, UnsupportedProviderFeature):
        return BridgeFailure(UNSUPPORTED_FEATURE_KIND, detail, status=400)
    if isinstance(error, InvalidInput):
        return BridgeFailure("configuration", detail, status=400)
    if isinstance(error, Forbidden):
        return BridgeFailure("configuration", detail, status=403)
    if isinstance(error, DeadlineExceeded):
        return BridgeFailure("timeout", detail, transient=True)
    if isinstance(error, Cancelled):
        return BridgeFailure("cancelled", detail)
    if isinstance(error, CapacityExceeded):
        return BridgeFailure("request", detail, status=429, transient=True)
    return BridgeFailure("request", detail, status=502)


def generate_via_gateway(
    gateway: object,
    payload: Mapping[str, object],
    *,
    tier: str,
    context: OperationContext,
) -> tuple[dict[str, object], ModelResponse]:
    """Send one converted payload through ``gateway`` and shape the reply.

    The rung binding is cleared for the duration of the call: the Ollama
    gateway and any offload it triggers re-enter ``_chat_request`` and must
    take the ordinary Ollama path, not this bridge again.
    """
    request = model_request_from_ollama_payload(payload, tier=tier)
    binding = active_rung()
    with suspend_rung():
        response = gateway.generate(request, context)
    if not isinstance(response, ModelResponse):
        raise DependencyUnavailable("model gateway returned an invalid response")
    if binding is not None and isinstance(response.model, str) and response.model:
        binding.served_model = response.model
    return ollama_shape(response), response


__all__ = [
    "BridgeFailure",
    "LEGACY_PROVIDER",
    "PROVIDER_UNAVAILABLE_KIND",
    "UNSUPPORTED_FEATURE_KIND",
    "UnsupportedProviderFeature",
    "RungBinding",
    "active_rung",
    "bind_rung",
    "classify_failure",
    "degradation_scope",
    "generate_via_gateway",
    "is_bridged",
    "model_request_from_ollama_payload",
    "ollama_shape",
    "provider_for_tier",
    "record_degradation",
    "suspend_rung",
]
