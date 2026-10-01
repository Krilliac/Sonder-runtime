"""Tier-aware legacy generators, without changing the Ollama wire contract.

The host injects its generator factory. Bind during construction (no Ollama
metadata probes for another provider) and again on every call, including calls
from another thread. Model names are deliberately never reverse-mapped to tiers.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import replace
import os
import time
from uuid import uuid4

import sonder_runtime.adapters.legacy_chat_bridge as legacy_chat_bridge
from .model_transport import ModelCallError
from ..application.chat import provider_bridge, stream_sink
from ..application.context import current_operation_context, local_owner_context
from ..domain.agents import sampling_policy

_LOCAL_ONLY = ContextVar("tier_generation_local_only", default=False)


@contextmanager
def local_only():
    token = _LOCAL_ONLY.set(True)
    try:
        yield
    finally:
        _LOCAL_ONLY.reset(token)


def _context(timeout, cancel_check, cloud_allowed, remote_ollama_allowed, origin=None):
    """The context for one bridged helper call.

    Bounded exactly like ``legacy_chat_bridge.operation_context``: by the call's
    own ``timeout`` and ``cancel_check`` only.  The ambient HTTP context's 30 s
    admission deadline and its drain token bound neither an agent step on the
    Ollama path nor a bridged chat turn, so they must not cut a bridged agent
    short either.  Consent can only narrow: the caller's (and, across threads,
    the constructing caller's) restrictions are never widened.
    """
    ambient = current_operation_context() or origin
    if origin is not None:
        cloud_allowed = cloud_allowed and origin.cloud_allowed
        remote_ollama_allowed = remote_ollama_allowed and origin.remote_ollama_allowed
    cancellation = legacy_chat_bridge.BridgeCancellation(cancel_check)
    if ambient is None:
        return local_owner_context(
            correlation_id="tier-helper-" + uuid4().hex, source="worker",
            timeout_seconds=timeout, cancellation=cancellation,
            cloud_allowed=cloud_allowed and not _LOCAL_ONLY.get(),
            remote_ollama_allowed=remote_ollama_allowed,
        )
    return replace(
        ambient,
        deadline_monotonic=time.monotonic() + float(timeout) if timeout is not None else None,
        cancellation=cancellation,
        cloud_allowed=cloud_allowed and ambient.cloud_allowed and not _LOCAL_ONLY.get(),
        remote_ollama_allowed=remote_ollama_allowed and ambient.remote_ollama_allowed,
    )


@contextmanager
def scope(tier, *, graph, consent, timeout=None, cancel_check=None):
    """Bind legacy paths that build their own payload instead of a generator."""
    provider = legacy_chat_bridge.provider_for_tier(tier, False, graph)
    context = _context(timeout, cancel_check, *consent())
    if provider_bridge.is_hosted(provider) and not context.cloud_allowed:
        raise ModelCallError("configuration", "hosted provider requires cloud consent for this operation", status=403, attempts=0)
    with provider_bridge.bind_helper_context(context), stream_sink.armed(None), stream_sink.claimed_for_call():
        with provider_bridge.bind_rung(provider, tier):
            yield provider


class _TierGenerator:
    def __init__(self, raw, provider, tier, options, consent, queue, local, tuning):
        self.raw = raw
        self.provider = provider
        self.tier = tier
        self.options = options
        self.consent = consent
        self.queue = queue
        self.local = local or _LOCAL_ONLY.get()
        self.origin = current_operation_context()
        self.tuning = tuning

    def __getattr__(self, name):
        return getattr(self.raw, name)

    @property
    def num_predict_override(self):
        return self.raw.num_predict_override

    @num_predict_override.setter
    def num_predict_override(self, value):
        self.raw.num_predict_override = value

    def __call__(self, *args, **kwargs):
        from .inference.ollama_pool import local_agent_admission

        cloud_allowed, remote_allowed = self.consent()
        context = _context(
            self.options.get("timeout"), self.options.get("cancel_check"),
            cloud_allowed and not self.local, remote_allowed, self.origin,
        )
        if provider_bridge.is_hosted(self.provider) and not context.cloud_allowed:
            raise ModelCallError("configuration", "hosted provider requires cloud consent for this operation", status=403, attempts=0)
        with provider_bridge.bind_helper_context(context), stream_sink.armed(None), stream_sink.claimed_for_call():
            admission = local_agent_admission(timeout_seconds=context.remaining_seconds) if self.queue else nullcontext()
            with provider_bridge.bind_rung(self.provider, self.tier, options=self.tuning), admission:
                return self.raw(*args, **kwargs)


def decision_temperature(provider):
    return sampling_policy.decision_temperature(os.environ) if provider in provider_bridge.THINKING_PROVIDERS else 0.1


def _thinking_advertised(graph, provider):
    """Read this provider's health once; a missing capability changes no mode.

    The dispatcher has no public gateway accessor. Keep the narrow lookup here
    until it does; never aggregate provider_status (which probes unrelated tiers).
    """
    from .inference.request_tuning import advertised_features, THINKING_MARKERS
    from .provider_dispatch.gateway import ProviderDispatchGateway
    from .provider_dispatch.fallback import PreSendFallbackGateway

    gateway = getattr(graph, "model_gateway", None)
    if isinstance(gateway, ProviderDispatchGateway):
        gateway = gateway._providers.get(provider)
    if isinstance(gateway, PreSendFallbackGateway):
        gateway = gateway.primary
    health = getattr(gateway, "health", None)
    if not callable(health):
        return False
    try:
        snapshot = health()
    except Exception:  # readiness and its errors remain the gateway's responsibility
        return False
    return bool(advertised_features(getattr(snapshot, "document", None)) & THINKING_MARKERS)


def make_generate(factory, tier, args, options, *, graph, consent):
    provider = legacy_chat_bridge.provider_for_tier(tier, False, graph)
    options = dict(options)
    queue = options.pop("queue", True)
    local = options.pop("local_only", False)
    kind = options.pop("generation_kind", None)
    tuning = {}
    if kind in ("decision", "json") and provider in provider_bridge.THINKING_PROVIDERS:
        env = dict(os.environ)
        policy = sampling_policy.sampling_policy(env, generation_kind=kind, temperature=args[2])
        args = (*args[:2], policy.temperature, *args[3:])
        advertised = _thinking_advertised(graph, provider) if policy.thinking_mode != "auto" or kind == "json" else False
        tuning = policy.options(thinking_advertised=advertised)
        if kind == "decision":
            from .inference.request_tuning import apply_sampling_defaults, sampling_defaults_enabled

            if sampling_defaults_enabled(env):
                apply_sampling_defaults(
                    tuning, str(args[0]), thinking=tuning.get("think", True), env=env, profile="decision",
                )
    if provider is not None:
        options["cloud"] = False  # the binding, never the model's spelling, owns transport
    with provider_bridge.bind_rung(provider, tier):
        raw = factory(*args, **options)
    return _TierGenerator(raw, provider, tier, options, consent, queue, local, tuning)
