"""Legacy HTTP chat steps served through the ModelGateway for non-Ollama rungs.

The legacy chat path in ``server.py`` is Ollama-shaped.  When a rung's tier is
bound to another provider (``SONDER_*_PROVIDER`` / ``SONDER_MODEL_BACKEND``),
the local branch of ``_chat_request`` hands the payload to the application's
``model_gateway`` through this module; conversion, shaping and error
classification live in ``sonder_runtime/application/chat/provider_bridge.py``.
With every tier on Ollama none of this runs.  ``server.py`` keeps only thin
wrappers that inject its live policy (cloud consent, Ollama locality, the
application graph), so a live reload of ``server.py`` never forks this state.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import time
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.application.chat import provider_bridge
from sonder_runtime.application.context import (
    OperationContext,
    current_operation_context,
    local_owner_context,
)
from sonder_runtime.domain.common.errors import SonderError

_logger = logging.getLogger("sonder.server")


def provider_bindings(graph: object) -> object:
    """Bindings of the live graph when one exists, else the same env parse."""
    bindings = getattr(graph, "provider_bindings", None)
    if bindings is not None:
        return bindings
    from sonder_runtime.adapters.provider_bindings import provider_bindings_from_env

    try:
        return provider_bindings_from_env()
    except ValueError as exc:
        # The gateway composition would refuse the same configuration; fail
        # the turn loudly rather than silently serving it from Ollama.
        raise ModelCallError("configuration", str(exc), status=503) from exc


def provider_for_tier(tier_label: object, cloud: bool, graph: object) -> str | None:
    """The non-Ollama provider serving a local rung, or None for Ollama."""
    if cloud:
        return None
    provider = provider_bridge.provider_for_tier(tier_label, provider_bindings(graph))
    return provider if provider_bridge.is_bridged(provider) else None


def prompt_identity_model(model: str, tier_label: object, provider: str | None,
                          graph: object) -> str:
    """Only name a serving model when the bound provider cannot fall back."""
    if (provider == "sonder_inference"
            and provider_bindings(graph).fallbacks.get(provider) == "ollama"):
        # The same prompt may be sent to local Ollama with a different model.
        return ""
    from .inference.served_tier_models import served_prompt_model

    return served_prompt_model(model, tier_label, provider)


class BridgeCancellation:
    """Cancellation for one bridged model step: the legacy ``cancel_check`` only.

    The ambient HTTP context carries the lifecycle coordinator's token, which
    ``drain()`` cancels the moment a drain starts.  An admitted turn on the
    Ollama path is never interrupted by that token -- drain waits for it --
    so a bridged turn must not be either: honouring the coordinator token here
    would throw away an answer the provider already produced (and billed).
    Client disconnects and explicit cancels still arrive via ``cancel_check``.
    """

    def __init__(self, cancel_check: Callable[[], object] | None):
        self._check = cancel_check

    @property
    def cancelled(self) -> bool:
        return bool(self._check()) if callable(self._check) else False

    def wait(self, timeout: float | None = None) -> bool:
        if timeout:
            time.sleep(max(timeout, 0.0))
        return self.cancelled


def operation_context(
    timeout: float | None,
    cancel_check: Callable[[], object] | None,
    *,
    cloud_allowed: bool,
    remote_ollama_allowed: bool,
) -> OperationContext:
    """The ambient turn context (correlation id R), bounded by this call.

    The HTTP context's own 30 s default is an admission budget, not a model
    budget; the legacy call's timeout is the deadline, as on the Ollama path.
    Its cancellation is the legacy ``cancel_check`` only (see
    ``BridgeCancellation``): a drain that starts after admission lets the
    turn finish, exactly as it does for an Ollama rung.  Consent follows the
    same host policy ``_gateway_generate_text`` applies (injected by caller).
    """
    helper = provider_bridge.active_helper_context()
    if helper is not None:
        return helper
    deadline = time.monotonic() + float(timeout) if timeout else None
    ambient = current_operation_context()
    if ambient is not None:
        return dataclasses.replace(
            ambient,
            deadline_monotonic=deadline,
            cancellation=BridgeCancellation(cancel_check),
            cloud_allowed=cloud_allowed,
            remote_ollama_allowed=remote_ollama_allowed,
        )
    return local_owner_context(
        correlation_id="chat-%s" % os.urandom(6).hex(),
        timeout_seconds=float(timeout) if timeout else None,
        cancellation=BridgeCancellation(cancel_check),
        cloud_allowed=cloud_allowed,
        remote_ollama_allowed=remote_ollama_allowed,
    )


def _provider_display_url(gateway: object, provider: str) -> str:
    """Read safe endpoint metadata from the bound gateway, without probing it."""
    from .provider_dispatch.fallback import PreSendFallbackGateway
    from .provider_dispatch.gateway import ProviderDispatchGateway

    try:
        seen: set[int] = set()
        while id(gateway) not in seen:
            seen.add(id(gateway))
            if isinstance(gateway, ProviderDispatchGateway):
                gateway = gateway._providers.get(provider)
                continue
            if isinstance(gateway, PreSendFallbackGateway):
                # The binding describes the primary; a failed fallback already
                # retains both causes in the classified error's detail.
                gateway = gateway.primary
                continue
            settings = getattr(gateway, "settings", None)
            if not callable(settings):
                settings = getattr(gateway, "_resolved_config", None)
            if not callable(settings):
                break
            config = settings()
            display = getattr(config, "display_base_url", None)
            raw = display or getattr(config, "base_url", "")
            parts = urlsplit(raw)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                break
            host = parts.hostname
            if ":" in host:
                host = "[%s]" % host
            if parts.port is not None:
                host += ":%d" % parts.port
            # Provider display properties already apply their path policy.
            # Generic configs have no such guarantee: show only their origin.
            return urlunsplit((parts.scheme, host, parts.path if display else "", "", ""))
    except Exception:  # noqa: BLE001 - display metadata must not mask the provider failure
        pass
    return "(endpoint unavailable)"


def chat_request(gateway: object, payload: dict, rung, *, context: OperationContext):
    """Serve one local chat step through the gateway for a non-Ollama rung."""
    try:
        out, response = provider_bridge.generate_via_gateway(
            gateway, payload, tier=rung.tier, context=context,
        )
    except SonderError as exc:
        failure = provider_bridge.classify_failure(exc, provider=rung.provider)
        error = ModelCallError(
            failure.kind, failure.detail, status=failure.status,
            transient=failure.transient, attempts=1, cloud=False,
        )
        error.provider = rung.provider
        error.provider_display_url = _provider_display_url(gateway, rung.provider)
        raise error from exc
    return out, response.text


def note_degradation(step: str, detail: str) -> None:
    """An Ollama-only step a non-Ollama rung ran without: loud, never silent."""
    _logger.warning("non-Ollama chat turn degraded: %s (%s)", step, detail)
    provider_bridge.record_degradation(step)


# The receipt/route reason for a request rerouted by ``ollama_only_reroute``.
OLLAMA_ONLY_FEATURE_REASON = "ollama_only_feature"
_FALLBACK_HINT = (
    "set SONDER_INFERENCE_FALLBACK=ollama to serve schema/tool requests on "
    "local Ollama"
)


def ollama_only_reroute(
    gateway: object, payload: dict, rung, *, model: str, graph: object,
    loopback_endpoint: bool, context_probe: Callable[[], object],
) -> dict | None:
    """The payload to serve on local Ollama instead of the bridge, or None.

    Sonder Inference v1 cannot carry Ollama-only features (decoder schemas,
    native tools, images, tool calls; the predicate is
    ``provider_bridge.ollama_only_feature``, the same one the bridge refuses
    on).  When the operator has declared ``SONDER_INFERENCE_FALLBACK=ollama``
    -- the existing statement that local Ollama may serve prompts bound for
    Inference -- such a step returns its payload (with the Ollama context
    window the bridged rung skipped, via ``context_probe``) and the caller
    runs its ordinary Ollama path, *loopback only* (``local_only``), with the
    payload's own tier model.  Consent never widens: a hosted ``-cloud`` model or a
    non-loopback Ollama endpoint is refused here, exactly as the pre-send
    fallback's narrowed context would refuse it.

    Returns None for every other step (it stays on the bridge, which refuses
    the feature for providers without a declared fallback).  Without the
    declaration a Sonder Inference step is refused with a message naming the
    fix.  Every reroute is logged, announced as ``route.changed`` and noted on
    the turn receipt (``served by ollama (ollama-only feature: ...)``).
    """
    provider = getattr(rung, "provider", None)
    if provider != "sonder_inference":
        return None
    found = provider_bridge.ollama_only_feature(payload, provider=provider)
    if found is None:
        return None
    feature, message = found

    def refuse(detail: str) -> ModelCallError:
        error = ModelCallError(
            provider_bridge.UNSUPPORTED_FEATURE_KIND, detail,
            status=400, attempts=0, cloud=False,
        )
        error.provider = provider
        error.provider_display_url = _provider_display_url(gateway, provider)
        return error

    if provider_bindings(graph).fallbacks.get(provider) != "ollama":
        raise refuse("%s; %s" % (message, _FALLBACK_HINT))
    from sonder_runtime.domain.model_routing import is_cloud_model_name

    names = {str(model or "").strip(), str(payload.get("model") or "").strip()}
    names.discard("")
    if not names:
        raise refuse("%s; the tier has no local Ollama model to serve it" % message)
    if any(is_cloud_model_name(name) for name in names):
        raise refuse(
            "%s; SONDER_INFERENCE_FALLBACK=ollama serves it only on a local "
            "Ollama model, and this tier's model is hosted" % message
        )
    if not loopback_endpoint:
        raise refuse(
            "%s; SONDER_INFERENCE_FALLBACK=ollama serves it only on the "
            "loopback Ollama endpoint, and this one is remote" % message
        )
    step = "served by ollama (ollama-only feature: %s)" % feature
    _logger.warning(
        "sonder_inference chat step %s; model=%s tier=%s",
        step, sorted(names)[0], getattr(rung, "tier", ""),
    )
    provider_bridge.record_degradation(step)
    from sonder_runtime.application.session.provider_attempts import (
        report_provider_fallback,
    )

    report_provider_fallback(provider, "ollama", OLLAMA_ONLY_FEATURE_REASON)
    return with_local_context(payload, context_probe)


def with_local_context(payload: dict, context_probe: Callable[[], object]) -> dict:
    """A rerouted payload with the Ollama context window a bridged rung skipped.

    A bridged rung builds its payload without probing Ollama for the model's
    window (``num_ctx`` is left unset); the rerouted Ollama call restores it
    so a schema prompt is not truncated at the daemon's default.
    """
    options = payload.get("options")
    if not isinstance(options, dict):
        return payload
    current = options.get("num_ctx")
    if isinstance(current, int) and not isinstance(current, bool) and current > 0:
        return payload
    probed = context_probe()
    if not isinstance(probed, int) or isinstance(probed, bool) or probed <= 0:
        return payload
    return {**payload, "options": {**options, "num_ctx": probed}}
