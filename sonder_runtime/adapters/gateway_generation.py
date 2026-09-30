"""Compatibility offloads through the application ChatService."""
import os

from .model_transport import ModelCallError
from ..application.chat import provider_bridge


def gateway_generate_text(application, prompt, tier="fast", system="", temperature=0.2,
                           num_predict=256, num_ctx=None, timeout=None, *,
                          cloud_allowed=False, remote_ollama_allowed=False):
    """offload_fn routed through the SPEC-3 ChatService over the ModelGateway.

    The port enforces the operation-context cloud-consent gate and returns
    domain-typed errors; this edge translates them back to ModelCallError
    (a urllib.error.URLError subclass) so existing callers that catch
    URLError — session summarization/titling — keep their exact behavior.
    An explicit num_ctx is forwarded through the port; when omitted the
    gateway resolves the native session context via _make_generate.
    """
    from sonder_runtime.application.chat.handle_chat import ChatCommand
    from sonder_runtime.application.context import (
        current_operation_context,
        local_owner_context,
    )
    from sonder_runtime.domain.common import errors as _errors

    # An offload made inside a turn joins that turn's run (same correlation
    # id R) so the next producer's events group with it.
    ambient = current_operation_context()
    context = local_owner_context(
        correlation_id=(
            ambient.correlation_id if ambient is not None
            else "offload-%s" % os.urandom(4).hex()
        ),
        source="system",
        cloud_allowed=cloud_allowed,
        remote_ollama_allowed=remote_ollama_allowed,
        timeout_seconds=float(timeout) if timeout else None,
    )
    try:
        # The offload asks for its own tier; the gateway routes it, never the
        # enclosing chat rung's binding (the Ollama gateway re-enters
        # _chat_request, which must take the ordinary path).
        with provider_bridge.suspend_rung():
            result = application().chat.complete(
                ChatCommand(
                    content=prompt, tier=tier, system=system,
                    temperature=temperature, num_predict=num_predict,
                    num_ctx=num_ctx,
                ),
                context,
            )
    except _errors.SonderError as exc:
        # Translate the domain taxonomy back to the legacy transport error
        # at the adapter edge so callers' URLError handling is unchanged.
        kind = {
            "DEADLINE_EXCEEDED": "timeout",
            "CANCELLED": "cancelled",
            "DEPENDENCY_UNAVAILABLE": "request",
            "FORBIDDEN": "configuration",
            "INVALID_INPUT": "configuration",
        }.get(getattr(exc, "code", ""), "request")
        raise ModelCallError(kind, str(exc)) from exc
    return result.response_text

