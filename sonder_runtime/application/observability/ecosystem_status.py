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
from typing import Mapping, Sequence

from ..ports.telemetry_feed import rfc3339_millis

ECOSYSTEM_SCHEMA = "sonder.runtime.ecosystem/1"
PROVIDER_TIER_NAMES = ("fast", "general", "code", "reasoning", "vision")
_STATES = frozenset({"ready", "degraded", "unavailable", "unknown"})
_DISCOVERY_SUFFIX = "/.well-known/sonder-telemetry"
_STAT_KEYS = (
    "subscribers", "emitted_events", "dropped_events", "retained_events",
    "buffer_capacity",
)


def _bindings_projection(bindings: object) -> dict[str, object]:
    projection = getattr(bindings, "status_projection", None)
    data = dict(projection()) if callable(projection) else {}
    tiers = data.get("tier_providers")
    if not isinstance(tiers, Mapping):
        tiers = getattr(bindings, "tier_providers", {}) or {}
    fallbacks = data.get("fallbacks")
    if not isinstance(fallbacks, Mapping):
        fallbacks = getattr(bindings, "fallbacks", {}) or {}
    return {
        "default_generation_provider": str(
            data.get("default_generation_provider")
            or getattr(bindings, "default_generation_provider", "") or ""
        ),
        "tier_providers": {tier: str(tiers.get(tier, "")) for tier in PROVIDER_TIER_NAMES},
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
            "provider bindings apply to HTTP chat and A2A; REPL, MCP, autopilot "
            "and fleet generation still use Ollama, and so do these HTTP chat "
            "dispatchers: natural-language work intents, ensemble and fanout "
            "(developer surfaces), exact model pins and the strict sonder "
            "alias; web research on a tier bound elsewhere is refused with 503"
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
    }


__all__ = [
    "ECOSYSTEM_SCHEMA",
    "bound_providers",
    "build_ecosystem_status",
    "ecosystem_warnings",
    "provider_statuses",
]
