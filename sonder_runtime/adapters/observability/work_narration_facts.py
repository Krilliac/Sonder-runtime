"""Safe, side-effect-free facts used by work narration.

This adapter deliberately inspects only configuration objects already held by
the runtime.  It never calls gateway settings resolvers, performs discovery,
reads a ready file, or contacts a provider.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit


_ENV_BY_PROVIDER = {
    "sonder_inference": "SONDER_INFERENCE_BASE_URL",
    "openai": "SONDER_OPENAI_BASE_URL",
    "openai_compat": "SONDER_OPENAI_BASE_URL",
    "openai_compatible": "SONDER_OPENAI_BASE_URL",
    "openrouter": "SONDER_OPENROUTER_BASE_URL",
    "anthropic": "SONDER_ANTHROPIC_BASE_URL",
}


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _url_host(value: Any) -> str:
    """Return only a URL's host and port, or ``""`` for invalid input."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw if "://" in raw else "http://" + raw)
        host = parts.hostname
        if not host or any(char.isspace() for char in host):
            return ""
        # Accessing port validates malformed and out-of-range ports.
        port = parts.port
        if ":" in host and not host.startswith("["):
            host = "[" + host + "]"
        return host + (":%d" % port if port is not None else "")
    except (ValueError, TypeError):
        return ""


def _base_url(config: Any) -> str:
    return _url_host(_get(config, "base_url", ""))


def _provider_map(gateway: Any) -> Mapping[str, Any]:
    providers = _get(gateway, "_providers", {})
    return providers if isinstance(providers, Mapping) else {}


def _configured_provider(gateway: Any, provider: str) -> tuple[str, str, Any]:
    """Return ``(label, host, gateway)`` without invoking gateway methods."""
    if not provider:
        return "", "", None
    selected = _provider_map(gateway).get(provider)
    if selected is None and provider:
        # Some graphs expose the provider gateway directly rather than through
        # ProviderDispatchGateway.
        selected = gateway if (_get(gateway, "PROVIDER_ID", "") == provider
                               or _get(gateway, "_settings_override") is not None
                               or _get(gateway, "_config") is not None
                               or _get(gateway, "_primary") is not None) else None
    if selected is None:
        return provider, "", None
    # PreSendFallbackGateway exposes the primary/fallback as attributes.
    wrapper = selected
    primary = _get(selected, "_primary")
    if primary is not None:
        selected = primary
    config = _get(selected, "_settings_override")
    if config is None:
        config = _get(selected, "_config")
    host = _base_url(config)
    if not host:
        env = _get(selected, "_env", {})
        if isinstance(env, Mapping):
            host = _url_host(env.get(_ENV_BY_PROVIDER.get(provider, ""), ""))
    return provider, host, wrapper


def _fallback_fact(selected: Any) -> tuple[str, str]:
    fallback = _get(selected, "_fallback")
    if fallback is None:
        return "", ""
    label = str(_get(selected, "_fallback_id", "ollama") or "ollama")
    config = _get(fallback, "_settings_override") or _get(fallback, "_config")
    host = _base_url(config)
    if not host:
        env = _get(fallback, "_env", {})
        if isinstance(env, Mapping):
            host = _url_host(env.get("OLLAMA_HOST", ""))
    return label, host


def configured_host(runtime: Any, tier: str = "") -> str:
    """Describe the selected configured provider endpoint without secrets.

    The result is suitable for a short acknowledgement, for example
    ``"sonder_inference 127.0.0.1:11437 (fallback ollama 127.0.0.1:11434)"``.
    An unavailable or malformed configuration returns ``""`` honestly.
    """
    runtime = runtime or {}
    selector = _get(runtime, "_bridge_provider_for_tier")
    provider = ""
    if callable(selector):
        try:
            provider = str(selector(tier) or "")
        except Exception:
            return ""
    graph = _get(runtime, "_APP_GRAPH") or _get(runtime, "app_graph") or runtime
    gateway = _get(graph, "model_gateway")
    if provider and provider.lower() not in {"ollama", "local_ollama"}:
        label, host, selected = _configured_provider(gateway, provider)
        if not host:
            env_name = _ENV_BY_PROVIDER.get(provider.lower())
            host = _url_host(os.environ.get(env_name, "")) if env_name else ""
        if not host:
            return ""
        fallback_label, fallback_host = _fallback_fact(selected)
        suffix = (" (fallback %s %s)" % (fallback_label, fallback_host)
                  if fallback_label and fallback_host else "")
        return "%s %s%s" % (label, host, suffix)
    pooled = _pool_hosts(runtime, tier)
    if pooled:
        return "ollama " + pooled
    base = _get(runtime, "BASE", "") or _get(runtime, "OLLAMA_HOST", "")
    host = _url_host(base) or _url_host(os.environ.get("OLLAMA_HOST", ""))
    return ("ollama " + host) if host else ""


def _pool_hosts(runtime: Any, tier: str) -> str:
    """Hosts of the pool workers that carry the tier's model, or ``""``.

    Only with remote workers in the pool: a lone local Ollama keeps naming its
    configured host. The pool's cached capability evidence is read, never
    probed. Account callers see a generic count instead of addresses.
    """
    pool = _get(runtime, "OLLAMA_POOL")
    try:
        if pool is None or not bool(getattr(pool, "has_remote_workers", False)):
            return ""
        policy = _get(runtime, "_RUNTIME_POLICY") or {}
        models = policy.get("local_models") if isinstance(policy, Mapping) else None
        model = str((models or {}).get(tier) or "").strip() if isinstance(models, Mapping) else ""
        origins = tuple(pool.origins_for_model(model)) if model else ()
    except Exception:
        return ""
    hosts = [host for host in (_url_host(origin) for origin in origins) if host]
    if not hosts:
        return ""
    from sonder_runtime.adapters.inference.ollama_reporting import caller_may_see_worker_hosts

    if caller_may_see_worker_hosts():
        return ", ".join(hosts)
    return "%d worker%s" % (len(hosts), "" if len(hosts) == 1 else "s")


__all__ = ["configured_host"]
