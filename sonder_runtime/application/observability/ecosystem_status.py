"""Build the ``sonder.runtime.ecosystem/1`` document (GET /v1/sonder/ecosystem).

The document tells an operator (and the Flutter app) which provider serves
each tier, what each provider reports about itself, and where Observatory can
connect.  It is assembled from three read-only inputs: the provider bindings,
the model gateway's duck-typed ``provider_status()``, and the live producer's
counters.  Nothing here probes a provider; a gateway without
``provider_status`` yields ``state: unknown`` rather than a guess.
"""
from __future__ import annotations

from datetime import datetime
import ipaddress
import re
from typing import Mapping, Sequence

from ..ports.telemetry_feed import rfc3339_millis

ECOSYSTEM_SCHEMA = "sonder.runtime.ecosystem/1"
CORE_PROVIDER_TIER_NAMES = ("fast", "general", "code", "reasoning", "vision")
EXTENDED_PROVIDER_TIER_NAMES = ("security", "creative", "draft")
PROVIDER_TIER_NAMES = CORE_PROVIDER_TIER_NAMES + EXTENDED_PROVIDER_TIER_NAMES
_STATES = frozenset({"ready", "degraded", "unavailable", "unknown"})
_DISCOVERY_SUFFIX = "/.well-known/sonder-telemetry"
_STAT_KEYS = (
    "subscribers", "emitted_events", "dropped_events", "retained_events",
    "buffer_capacity",
)
_IMAGE_BACKENDS = frozenset({"openai_api", "ollama"})
_ALLOW_SECURITY_MODEL_TOOLS_ENV = "SONDER_ALLOW_SECURITY_MODEL_TOOLS"
_ALLOW_SECURITY_MODEL_TOOLS_ACK = "I_ACCEPT_SECURITY_MODEL_TOOL_RISK"
_MAX_URL_LENGTH = 2048
_UNSAFE_URL_CHARS = re.compile(r"[\x00-\x20\x7f]")
_SCHEME_RE = re.compile(r"^(https?)://", re.I)
_IPV6_AUTHORITY_RE = re.compile(r"^\[([0-9A-Fa-f:]+)\](?::([0-9]{1,5}))?$")
_HOST_AUTHORITY_RE = re.compile(
    r"^([A-Za-z0-9](?:[A-Za-z0-9._\-]{0,252})?)(?::([0-9]{1,5}))?$"
)
_SAFE_URL_PATH_RE = re.compile(r"^(?:/[A-Za-z0-9._~%:+,=\-]*)*$")


def _host_is_loopback(host: str) -> bool:
    text = host.strip().lower().rstrip(".")
    if not text:
        return False
    if text == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def _safe_image_api_url(raw: object) -> tuple[str | None, bool | None]:
    """Validate and normalize an image API URL for read-only display.

    Accepts only ``http://`` loopback URLs and credential-free ``https://``
    URLs.  Strips any query string, fragment, and path ``;params``; returns
    ``(None, None)`` for empty, malformed, credential-bearing, or non-loopback
    plaintext HTTP URLs so nothing unsafe or secret is ever echoed.
    """
    if not isinstance(raw, str):
        return None, None
    text = raw.strip()
    if not text or len(text) > _MAX_URL_LENGTH or _UNSAFE_URL_CHARS.search(text):
        return None, None
    scheme_match = _SCHEME_RE.match(text)
    if scheme_match is None:
        return None, None
    scheme = scheme_match.group(1).lower()
    rest = text[scheme_match.end():]
    before_query = rest.split("?", 1)[0].split("#", 1)[0]
    if "@" in before_query:
        return None, None
    slash = before_query.find("/")
    authority = before_query if slash < 0 else before_query[:slash]
    raw_path = "" if slash < 0 else before_query[slash:]
    if not authority or "%" in authority:
        return None, None

    ipv6_match = _IPV6_AUTHORITY_RE.fullmatch(authority)
    if ipv6_match is not None:
        try:
            addr = ipaddress.IPv6Address(ipv6_match.group(1))
        except ValueError:
            return None, None
        host_for_check = addr.compressed
        rendered_host = "[%s]" % addr.compressed
        port_text = ipv6_match.group(2)
    else:
        host_match = _HOST_AUTHORITY_RE.fullmatch(authority)
        if host_match is None:
            return None, None
        host_raw = host_match.group(1).lower()
        if ".." in host_raw:
            return None, None
        host_for_check = host_raw
        rendered_host = host_raw
        port_text = host_match.group(2)

    port_suffix = ""
    if port_text is not None:
        port = int(port_text)
        if not 1 <= port <= 65535:
            return None, None
        port_suffix = ":%d" % port

    loopback = _host_is_loopback(host_for_check)
    if scheme == "http" and not loopback:
        return None, None

    clean_path = "/".join(segment.partition(";")[0] for segment in raw_path.split("/"))
    if clean_path and not _SAFE_URL_PATH_RE.fullmatch(clean_path):
        return None, None
    clean_path = clean_path.rstrip("/")
    return "%s://%s%s%s" % (scheme, rendered_host, port_suffix, clean_path), loopback


def image_generation_status_from_env(env: Mapping[str, str] | None) -> dict[str, object]:
    """Read-only inspection of configured image-generation settings (no network probes)."""
    source = env if isinstance(env, Mapping) else {}
    raw_url = str(source.get("SONDER_IMAGE_API_URL", "") or "").strip()
    raw_model = str(source.get("SONDER_IMAGE_MODEL", "") or "").strip()
    safe_url, loopback = _safe_image_api_url(raw_url) if raw_url else (None, None)
    model = raw_model or None
    if safe_url is not None:
        return {
            "configured": True,
            "backend": "openai_api",
            "api_url": safe_url,
            "model": model,
            "loopback": loopback,
        }
    if model is not None:
        return {
            "configured": True,
            "backend": "ollama",
            "api_url": None,
            "model": model,
            "loopback": None,
        }
    return {
        "configured": False,
        "backend": None,
        "api_url": None,
        "model": None,
        "loopback": None,
    }


def _normalize_image_generation(raw: Mapping[str, object] | None) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        return {
            "configured": False,
            "backend": None,
            "api_url": None,
            "model": None,
            "loopback": None,
        }
    raw_url = raw.get("api_url")
    safe_url, loopback = (
        _safe_image_api_url(raw_url)
        if isinstance(raw_url, str) and raw_url.strip()
        else (None, None)
    )
    raw_model = raw.get("model")
    model = raw_model.strip() if isinstance(raw_model, str) and raw_model.strip() else None
    raw_backend = raw.get("backend")
    backend = (
        raw_backend.strip()
        if isinstance(raw_backend, str) and raw_backend.strip() in _IMAGE_BACKENDS
        else None
    )
    if backend == "openai_api" and safe_url is None:
        backend = "ollama" if model is not None else None
    if backend is None:
        if safe_url is not None:
            backend = "openai_api"
        elif model is not None:
            backend = "ollama"
    configured = raw.get("configured")
    if not isinstance(configured, bool) or (configured and backend is None):
        configured = backend is not None
    return {
        "configured": configured,
        "backend": backend,
        "api_url": safe_url,
        "model": model,
        "loopback": loopback if safe_url is not None else None,
    }


def model_tool_policy_status_from_env(
    env: Mapping[str, str] | None,
    *,
    floor_available: bool = True,
) -> dict[str, object]:
    """Read-only status of the host-only security-advisers floor relaxation."""
    source = env if isinstance(env, Mapping) else {}
    ack = str(source.get(_ALLOW_SECURITY_MODEL_TOOLS_ENV, "") or "").strip()
    return {
        "security_floor_relaxed": bool(
            floor_available and ack == _ALLOW_SECURITY_MODEL_TOOLS_ACK
        ),
    }


def _normalize_model_tool_policy(raw: Mapping[str, object] | None) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        return {"security_floor_relaxed": False}
    return {
        "security_floor_relaxed": bool(raw.get("security_floor_relaxed", False)),
    }


def _bindings_projection(bindings: object) -> dict[str, object]:
    projection = getattr(bindings, "status_projection", None)
    data = dict(projection()) if callable(projection) else {}
    tiers = data.get("tier_providers")
    if not isinstance(tiers, Mapping):
        tiers = getattr(bindings, "tier_providers", {}) or {}
    fallbacks = data.get("fallbacks")
    if not isinstance(fallbacks, Mapping):
        fallbacks = getattr(bindings, "fallbacks", {}) or {}
    default_provider = str(
        data.get("default_generation_provider")
        or getattr(bindings, "default_generation_provider", "") or ""
    )
    tier_providers: dict[str, str] = {}
    for tier in PROVIDER_TIER_NAMES:
        if tier in tiers:
            raw_tier = tiers.get(tier)
            tier_providers[tier] = "" if raw_tier is None else str(raw_tier)
        elif tier in EXTENDED_PROVIDER_TIER_NAMES:
            # Backward compatibility for legacy 5-tier bindings objects.
            tier_providers[tier] = default_provider
        else:
            tier_providers[tier] = ""
    return {
        "default_generation_provider": default_provider,
        "tier_providers": tier_providers,
        "embedding_provider": str(
            data.get("embedding_provider")
            or getattr(bindings, "embedding_provider", "") or ""
        ),
        "fallbacks": {str(k): str(v) for k, v in dict(fallbacks).items()},
    }


def bound_providers(projection: Mapping[str, object]) -> tuple[str, ...]:
    """Every provider a binding (or fallback) can route to, in stable order."""
    names: list[str] = [str(projection["default_generation_provider"])]
    names.extend(str(v) for v in dict(projection["tier_providers"]).values())
    names.append(str(projection["embedding_provider"]))
    names.extend(str(v) for v in dict(projection["fallbacks"]).values())
    seen: list[str] = []
    for name in names:
        if name and name not in seen:
            seen.append(name)
    return tuple(seen)


def _unknown(provider: str) -> dict[str, object]:
    return {"provider": provider, "state": "unknown"}


def _status_row(provider: str, row: object) -> dict[str, object]:
    if not isinstance(row, Mapping):
        return _unknown(provider)
    value = {str(k): v for k, v in row.items()}
    value["provider"] = provider
    if value.get("state") not in _STATES:
        value["state"] = "unknown"
    return value


def provider_statuses(
    providers: Sequence[str], gateway: object,
) -> tuple[dict[str, dict[str, object]], bool]:
    """Return ({provider: status}, surface_available) from ``provider_status``."""
    reader = getattr(gateway, "provider_status", None)
    if not callable(reader):
        return {name: _unknown(name) for name in providers}, False
    try:
        reported = reader()
    except Exception as error:
        detail = "provider_status failed: %s" % type(error).__name__
        return {
            name: {**_unknown(name), "detail": detail} for name in providers
        }, True
    reported = reported if isinstance(reported, Mapping) else {}
    return {
        name: _status_row(name, reported.get(name)) for name in providers
    }, True


def _base_url_of(status: Mapping[str, object]) -> str | None:
    telemetry = status.get("telemetry")
    if not isinstance(telemetry, Mapping):
        return None
    discovery = telemetry.get("discovery_url")
    if isinstance(discovery, str) and discovery.endswith(_DISCOVERY_SUFFIX):
        return discovery[: -len(_DISCOVERY_SUFFIX)] or None
    base = status.get("base_url")
    return base if isinstance(base, str) and base else None


def ecosystem_warnings(
    projection: Mapping[str, object],
    statuses: Mapping[str, Mapping[str, object]],
    *,
    export_enabled: bool,
    observatory_origins: Sequence[str],
    dedicated_origins: Sequence[str] | None = None,
) -> list[str]:
    """Operator warnings for the ecosystem document.

    ``observatory_origins`` are every origin that may read the telemetry
    routes (the route-scoped list plus the global ``SONDER_CORS_ORIGINS``);
    ``dedicated_origins`` are the route-scoped ``SONDER_OBSERVATORY_ORIGINS``
    alone.  A global origin (for example the Flutter web app) does not mean
    Observatory was configured, so the missing-origin warning looks at the
    dedicated list.
    """
    warnings: list[str] = []
    dedicated = list(observatory_origins if dedicated_origins is None else dedicated_origins)
    if projection.get("embedding_provider") == "sonder_inference":
        warnings.append(
            "embedding_provider is sonder_inference, which does not serve "
            "embeddings; set SONDER_EMBEDDING_PROVIDER=ollama"
        )
    if export_enabled and not dedicated:
        if observatory_origins:
            warnings.append(
                "no SONDER_OBSERVATORY_ORIGINS entry: only the global "
                "SONDER_CORS_ORIGINS origins (%s) may read Runtime telemetry "
                "in a browser; add the Observatory origin to "
                "SONDER_OBSERVATORY_ORIGINS (for example http://127.0.0.1:4173)"
                % ", ".join(observatory_origins)
            )
        else:
            warnings.append(
                "no browser origin may read Runtime telemetry; add the Observatory "
                "origin to SONDER_OBSERVATORY_ORIGINS (for example "
                "http://127.0.0.1:4173)"
            )
    if not export_enabled:
        warnings.append(
            "live telemetry export is disabled (SONDER_OBSERVATORY_EXPORT=0)"
        )
    for name, status in statuses.items():
        if status.get("synthetic") is True:
            warnings.append(
                "provider %s is synthetic (mock backend): output is not a "
                "quality or performance signal" % name
            )
    generation = [projection.get("default_generation_provider"),
                  *dict(projection.get("tier_providers") or {}).values()]
    if any(name and name != "ollama" for name in generation):
        warnings.append(
            "provider bindings apply to tier-based chat, agents, autopilot, fleet "
            "and helpers; exact model pins, the strict sonder alias and durable "
            "fanout remain Ollama routes. Native image/schema requests and sealed "
            "single-send canaries require Ollama-bound tiers"
        )
    return warnings


def build_ecosystem_status(
    *,
    generated_at: datetime | str,
    runtime: Mapping[str, object],
    bindings: object,
    gateway: object,
    export_enabled: bool,
    runtime_stream: Mapping[str, str] | None,
    stats: Mapping[str, object] | None,
    observatory_origins: Sequence[str],
    runtime_base_url: str,
    dedicated_origins: Sequence[str] | None = None,
    image_generation: Mapping[str, object] | None = None,
    model_tool_policy: Mapping[str, object] | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, object] | None:
    """Return the ecosystem document, or None when there is nothing to report.

    None means both the live export and the provider status surface are
    unavailable; the HTTP route answers 404 in that case.
    """
    projection = _bindings_projection(bindings)
    providers = bound_providers(projection)
    statuses, surface = provider_statuses(providers, gateway)
    if not export_enabled and not surface:
        return None
    connect_urls: list[str] = []
    if export_enabled and runtime_base_url:
        connect_urls.append(runtime_base_url)
    for status in statuses.values():
        base = _base_url_of(status)
        if base and base not in connect_urls:
            connect_urls.append(base)
    stats = stats or {}
    image_status = (
        _normalize_image_generation(image_generation)
        if image_generation is not None
        else image_generation_status_from_env(env)
    )
    policy_status = (
        _normalize_model_tool_policy(model_tool_policy)
        if model_tool_policy is not None
        else model_tool_policy_status_from_env(env)
    )
    return {
        "schema": ECOSYSTEM_SCHEMA,
        "generated_at": (
            rfc3339_millis(generated_at) if isinstance(generated_at, datetime)
            else str(generated_at)
        ),
        "runtime": {
            "version": str(runtime.get("version") or ""),
            "instance_id": runtime.get("instance_id"),
            "node_id": str(runtime.get("node_id") or ""),
            "base_url": runtime_base_url,
        },
        "providers": {
            **projection,
            "status": statuses,
        },
        "observatory": {
            "export_enabled": bool(export_enabled),
            "runtime_stream": dict(runtime_stream) if export_enabled and runtime_stream else None,
            "stats": {key: int(stats.get(key, 0) or 0) for key in _STAT_KEYS},
            "cors_origins": list(observatory_origins),
            "connect_urls": connect_urls,
            "warnings": ecosystem_warnings(
                projection, statuses,
                export_enabled=export_enabled,
                observatory_origins=observatory_origins,
                dedicated_origins=dedicated_origins,
            ),
        },
        "image_generation": image_status,
        "model_tool_policy": policy_status,
    }


__all__ = [
    "CORE_PROVIDER_TIER_NAMES",
    "ECOSYSTEM_SCHEMA",
    "EXTENDED_PROVIDER_TIER_NAMES",
    "PROVIDER_TIER_NAMES",
    "bound_providers",
    "build_ecosystem_status",
    "ecosystem_warnings",
    "image_generation_status_from_env",
    "model_tool_policy_status_from_env",
    "provider_statuses",
]
