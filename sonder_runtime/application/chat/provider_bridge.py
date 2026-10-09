"""Route the legacy HTTP chat path through the ModelGateway for non-Ollama rungs.

``server.py``'s user-facing chat (``POST /v1/chat/completions``) builds
Ollama-shaped ``/api/chat`` payloads.  When the rung's tier is bound to a
provider other than Ollama, the local branch of ``_chat_request`` hands that
payload to this module instead of posting it to Ollama.  This module:

* holds the per-rung provider in a ContextVar (here, not in ``server.py``, so a
  live reload of ``server.py`` cannot orphan an in-flight binding);
* converts the Ollama payload into a provider-neutral ``ModelRequest`` and
  refuses what the gateway cannot carry (decoder schemas, native tools,
  images, and thinking except on ``THINKING_PROVIDERS``, whose gateway
  decides from what the server advertises);
* claims the turn's live token stream (``stream_sink``) for the call when
  the provider can stream (``STREAMING_PROVIDERS``), so a streamed HTTP turn
  shows tokens as they are generated;
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

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType

from ...domain.thinking_controls import with_local_thinking_budget
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
from . import stream_sink

LEGACY_PROVIDER = "ollama"
# The ModelCallError kind for a provider that is down.  It is terminal: the
# escalation ladder must not treat a missing provider as a weak answer.
PROVIDER_UNAVAILABLE_KIND = "provider_unavailable"
# A feature the bound provider cannot carry is the caller's error (400); a
# stronger rung would only hide it, so it is terminal as well.
UNSUPPORTED_FEATURE_KIND = "unsupported_feature"
_FORWARDED_OPTIONS = (
    "temperature", "num_predict", "num_ctx", "top_p", "top_k", "min_p",
    "repeat_penalty", "seed", "stop",
)
# Providers whose gateway forwards ``think`` itself (as chat_template_kwargs
# when the server advertises support, refusing True otherwise).
THINKING_PROVIDERS = frozenset({"sonder_inference"})
# Providers whose gateway can forward content deltas to a live turn stream.
STREAMING_PROVIDERS = frozenset({"sonder_inference"})
_ROLES = frozenset({"system", "user", "assistant", "tool"})

_RUNG: ContextVar["RungBinding | None"] = ContextVar(
    "sonder_chat_rung_binding", default=None,
)
_HELPER_CONTEXT: ContextVar[OperationContext | None] = ContextVar(
    "sonder_tier_helper_context", default=None,
)
_DEGRADATIONS: ContextVar[list[str] | None] = ContextVar(
    "sonder_chat_turn_degradations", default=None,
)


class UnsupportedProviderFeature(InvalidInput):
    """The request needs an Ollama-only feature the bound provider lacks."""


def provider_for_tier(tier_label: object, bindings: object) -> str:
    """The provider a rung uses.

    ``tier_label`` is the *resolved* rung label from ``server._serve_target``,
    not the caller's ``model`` field.  The HTTP default route (``sonder``,
    ``local`` or blank) resolves to the chat policy tier, so it follows that
    tier's binding -- exactly as A2A's ChatService does.

    * The five provider tiers follow ``bindings.tier_providers``.
    * A resolved ``sonder`` label is the operator's local Ollama alias (strict
      mode, or no chat tier model): like an exact ``model:*`` pin it always
      stays on Ollama, matching ``ProviderDispatchGateway.generate_strict_alias``
      so HTTP and A2A never serve the same alias from different providers.
    * Hosted/cloud tiers always stay on Ollama.
    """
    tier = str(tier_label or "").strip().lower()
    tiers = getattr(bindings, "tier_providers", None) or {}
    if tier in tiers:
        return str(tiers[tier])
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
    options: Mapping[str, object] | None = None


@contextmanager
def bind_rung(
    provider: str | None, tier: str, *, options: Mapping[str, object] | None = None,
) -> Iterator[RungBinding | None]:
    """Bind the rung's provider for the enclosed block; Ollama binds nothing."""
    binding = (
        RungBinding(
            provider, str(tier or "sonder"),
            options=MappingProxyType(dict(options)) if options is not None else None,
        )
        if is_bridged(provider) else None
    )
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


def active_helper_context() -> OperationContext | None:
    return _HELPER_CONTEXT.get()


@contextmanager
def bind_helper_context(context: OperationContext) -> Iterator[None]:
    token = _HELPER_CONTEXT.set(context)
    try:
        yield
    finally:
        _HELPER_CONTEXT.reset(token)


# Bridged providers that run off this machine on a third party's service.  A
# rung bound to one gets the same hosted-data boundary as a ``cloud-*`` tier:
# only request-scoped instructions, never the disk-backed local profile,
# emotion vectors, goal, or recalled memory/lessons.
HOSTED_PROVIDERS = frozenset({"openrouter"})


def is_hosted(provider: object) -> bool:
    return provider in HOSTED_PROVIDERS


def hosted_rung_active() -> bool:
    """Whether the rung bound on this thread sends prompts to a hosted provider."""
    binding = _RUNG.get()
    return binding is not None and is_hosted(binding.provider)


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


def ollama_only_feature(
    payload: Mapping[str, object], *, provider: str | None = None,
) -> tuple[str, str] | None:
    """The first Ollama-only feature ``payload`` asks for, as (feature, message).

    The single predicate the bridge refuses on (``model_request_from_ollama_payload``)
    and the legacy hook reroutes on (``legacy_chat_bridge.ollama_only_reroute``),
    so the two can never disagree.  ``think=True`` is a feature only for a
    provider outside ``THINKING_PROVIDERS``; ``think=False`` never is.
    """
    if not isinstance(payload, Mapping):
        return None
    if payload.get("format") is not None:
        return ("format",
                "response_format/schema decoding is only available on Ollama tiers")
    if payload.get("think") is True and provider not in THINKING_PROVIDERS:
        return ("think", "model thinking is only available on Ollama tiers")
    if payload.get("tools"):
        return ("tools", "native tool calls are only available on Ollama tiers")
    messages = payload.get("messages")
    for message in messages if isinstance(messages, list) else ():
        if isinstance(message, Mapping) and (
                message.get("images") or message.get("tool_calls")):
            return ("images" if message.get("images") else "tool_calls",
                    "images and tool calls are only available on Ollama tiers")
    return None


def model_request_from_ollama_payload(
    payload: Mapping[str, object], *, tier: str, provider: str | None = None,
) -> ModelRequest:
    """Convert one Ollama ``/api/chat`` payload into a provider-neutral request.

    ``provider`` in ``THINKING_PROVIDERS`` carries a boolean ``think`` as the
    ``think`` option instead of refusing ``True`` and dropping ``False``.
    """
    if not isinstance(payload, Mapping):
        raise InvalidInput("chat payload must be an object")
    refused = ollama_only_feature(payload, provider=provider)
    if refused is not None:
        raise UnsupportedProviderFeature(refused[1])
    think = payload.get("think")
    binding = active_rung()
    bound_options = (
        binding.options
        if binding is not None and binding.provider == provider
        and provider in THINKING_PROVIDERS else None
    )
    # Explicit request fields win over frozen generator defaults.
    if bound_options and think is None and isinstance(bound_options.get("think"), bool):
        think = bound_options["think"]
    payload_options = payload.get("options")
    payload_options = payload_options if isinstance(payload_options, Mapping) else {}
    carry_think = provider in THINKING_PROVIDERS and isinstance(think, bool)
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise InvalidInput("chat payload has no messages")
    system_parts: list[str] = []
    turns: list[dict[str, str]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            raise InvalidInput("chat message %d is not an object" % index)
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
            if key == "stop":
                if isinstance(value, str) and value:
                    options[key] = value
                elif (isinstance(value, (list, tuple))
                      and all(isinstance(item, str) and item for item in value)):
                    options[key] = list(value)
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if key in {"num_predict", "num_ctx"} and int(value) <= 0:
                continue
            if key == "temperature":
                options[key] = float(value)
            elif key == "top_k" or key in {"num_predict", "num_ctx", "seed"}:
                options[key] = int(value)
            else:
                options[key] = float(value)
    if bound_options:
        for key, value in bound_options.items():
            if key == "think" or key in options:
                continue
            if key in {"reasoning_budget_tokens", "reasoning_budget_message"}:
                if value is not None:
                    options[key] = value
            elif key in _FORWARDED_OPTIONS or key == "sampling_profile":
                options[key] = value
    # The bridge accepts reasoning controls in the legacy options object too;
    # the inference gateway maps them onto its request contract.
    if provider in THINKING_PROVIDERS:
        for key in ("reasoning_budget_tokens", "reasoning_budget_message"):
            value = payload.get(key, payload_options.get(key))
            if value is not None:
                options[key] = value
        profile = payload.get("sampling_profile", payload_options.get("sampling_profile"))
        if profile is not None:
            options["sampling_profile"] = profile
    if carry_think:
        options["think"] = think
    return ModelRequest(
        prompt=prompt,
        tier=str(tier or "sonder"),
        system="\n\n".join(system_parts),
        history=tuple(turns),
        options=options,
    )


def ollama_shape(response: ModelResponse, *, finish_reason=None) -> dict[str, object]:
    """Shape a ModelResponse as the Ollama reply legacy callers consume."""
    # Preserve a provider-reported finish reason when available; never invent
    # "stop" for gateways without that evidence (PR #616's budget contract).
    reason = (
        getattr(response, "finish_reason", None)
        or getattr(response, "done_reason", None)
        or finish_reason
    )
    if reason is None:
        telemetry = getattr(response, "telemetry", None)
        reason = getattr(telemetry, "finish_reason", None)
    shaped: dict[str, object] = {
        "model": response.model,
        "message": {"role": "assistant", "content": response.text},
        "done": True,
    }
    if isinstance(reason, str) and reason.strip().lower() in {
        "stop", "length", "content_filter", "tool_calls", "function_call",
    }:
        shaped["done_reason"] = reason.strip().lower()
    if response.tokens_in is not None:
        shaped["prompt_eval_count"] = response.tokens_in
    if response.tokens_out is not None:
        shaped["eval_count"] = response.tokens_out
    # Provider-reported prompt-cache reuse, in the field Ollama uses for it
    # (a subset of prompt_eval_count).  Absent stays absent: never a fake 0.
    cached = getattr(response.telemetry, "prompt_cached_tokens", None)
    if (response.tokens_in is not None and type(cached) is int
            and 0 <= cached <= response.tokens_in):
        shaped["prompt_eval_cached_count"] = cached
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
    binding = active_rung()
    provider = binding.provider if binding is not None else None
    if provider in THINKING_PROVIDERS and payload.get("think") is not False:
        # A reasoning model left free to think (Qwen3.5/3.8 default to it) spends
        # num_predict on thought first; a tight cap returns done_reason=length
        # with no content.  Same headroom the local Ollama path gives a known
        # thinking model; it only raises a cap, so non-thinking models are unaffected.
        payload = with_local_thinking_budget(payload)
    request = model_request_from_ollama_payload(payload, tier=tier, provider=provider)
    with suspend_rung():
        if provider in STREAMING_PROVIDERS:
            # Only the turn's first bridged generation streams (see stream_sink).
            with stream_sink.claimed_for_call():
                response = gateway.generate(request, context)
        else:
            response = gateway.generate(request, context)
    if not isinstance(response, ModelResponse):
        raise DependencyUnavailable("model gateway returned an invalid response")
    if binding is not None and isinstance(response.model, str) and response.model:
        binding.served_model = response.model
    metadata = getattr(gateway, "last_response_meta", None)
    finish_reason = metadata.get("finish_reason") if isinstance(metadata, Mapping) else None
    return ollama_shape(response, finish_reason=finish_reason), response


__all__ = [
    "BridgeFailure",
    "LEGACY_PROVIDER",
    "PROVIDER_UNAVAILABLE_KIND",
    "STREAMING_PROVIDERS",
    "THINKING_PROVIDERS",
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
    "ollama_only_feature",
    "ollama_shape",
    "provider_for_tier",
    "record_degradation",
    "suspend_rung",
]
