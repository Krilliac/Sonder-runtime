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


def chat_request(gateway: object, payload: dict, rung, *, context: OperationContext):
    """Serve one local chat step through the gateway for a non-Ollama rung."""
    try:
        out, response = provider_bridge.generate_via_gateway(
            gateway, payload, tier=rung.tier, context=context,
        )
    except SonderError as exc:
        failure = provider_bridge.classify_failure(exc, provider=rung.provider)
        raise ModelCallError(
            failure.kind, failure.detail, status=failure.status,
            transient=failure.transient, attempts=1, cloud=False,
        ) from exc
    return out, response.text


def ollama_agent_refusal(step: str, tier_label: object, provider: str) -> ModelCallError:
    """The 503 for an Ollama-only HTTP chat step on a tier bound elsewhere.

    The tool-using agent (web research) drives Ollama's native tool calls,
    which the gateway cannot carry.  When the tier it would run on is bound
    to another provider, answering from Ollama would silently ignore the
    binding -- and with Ollama absent it would return a transport error as a
    200 answer -- so the turn ends with a 503 that names the binding.
    """
    return ModelCallError(
        "configuration",
        "%s runs a tool-using agent that only Ollama serves, but tier %r is "
        "bound to provider %s; bind that tier to ollama "
        "(SONDER_%s_PROVIDER=ollama) to use it over HTTP chat"
        % (step, tier_label, provider, str(tier_label).upper()),
        status=503, attempts=0,
    )


def note_degradation(step: str, detail: str) -> None:
    """An Ollama-only step a non-Ollama rung ran without: loud, never silent."""
    _logger.warning("non-Ollama chat turn degraded: %s (%s)", step, detail)
    provider_bridge.record_degradation(step)
