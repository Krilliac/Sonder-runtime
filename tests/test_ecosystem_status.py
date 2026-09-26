"""GET /v1/sonder/ecosystem document (sonder.runtime.ecosystem/1), key for key."""
from types import SimpleNamespace

from sonder_runtime.adapters.provider_bindings import ProviderBindings
from sonder_runtime.application.observability.ecosystem_status import (
    ECOSYSTEM_SCHEMA,
    build_ecosystem_status,
)

STATUS_KEYS = {
    "provider", "state", "healthy", "checked_at", "detail", "capabilities", "base_url",
    "version", "api_version", "models", "synthetic", "identity", "telemetry",
    "fallback", "fallback_count",
}


def _bindings(default="openai_compatible", embedding="ollama"):
    tiers = {"fast": "ollama", "general": default, "code": default,
             "reasoning": default, "vision": "ollama"}
    if {default, embedding} <= {"ollama", "openai_compatible"}:
        return ProviderBindings(
            default_generation_provider=default,
            tier_providers=tiers,
            embedding_provider=embedding,
        )
    # Provider ids this branch does not know yet (sonder_inference arrives
    # with the provider lane) are read through the same duck-typed surface.
    return SimpleNamespace(
        default_generation_provider=default,
        tier_providers=tiers,
        embedding_provider=embedding,
        fallbacks={},
        status_projection=lambda: {
            "default_generation_provider": default,
            "tier_providers": dict(tiers),
            "embedding_provider": embedding,
            "fallbacks": {},
        },
    )


def _inference_status():
    return {
        "provider": "sonder_inference", "state": "ready", "healthy": True,
        "checked_at": "2026-09-26T10:00:00.000Z", "detail": "", "capabilities": ["chat"],
        "base_url": "http://127.0.0.1:18437", "version": "0.1.0", "api_version": 1,
        "models": ["mock:tiny"], "synthetic": True, "identity": None,
        "telemetry": {
            "discovery_url": "http://127.0.0.1:18437/.well-known/sonder-telemetry",
            "sse_url": "http://127.0.0.1:18437/v1/telemetry/sse",
            "ndjson_url": "http://127.0.0.1:18437/v1/telemetry/ndjson",
        },
        "fallback": None, "fallback_count": 0,
    }


def _build(bindings, gateway, *, export=True, origins=("http://127.0.0.1:4173",)):
    base = "http://127.0.0.1:18435"
    return build_ecosystem_status(
        generated_at="2026-09-26T10:00:00.000Z",
        runtime={"version": "0.9.0", "instance_id": "rt-0123456789ab", "node_id": "host"},
        bindings=bindings,
        gateway=gateway,
        export_enabled=export,
        runtime_stream={
            "discovery_url": base + "/.well-known/sonder-telemetry",
            "sse_url": base + "/v1/observability/events",
            "ndjson_url": base + "/v1/observability/events?format=ndjson",
        },
        stats={"subscribers": 1, "emitted_events": 10, "dropped_events": 0,
               "retained_events": 10, "buffer_capacity": 4096,
               "subscriber_dropped_events": 3},
        observatory_origins=list(origins),
        runtime_base_url=base,
    )


def test_document_matches_contract_section_9_key_for_key():
    bindings = _bindings("sonder_inference")
    gateway = SimpleNamespace(provider_status=lambda: {
        "sonder_inference": _inference_status(),
        "ollama": {"provider": "ollama", "state": "unavailable", "healthy": False},
    })
    document = _build(bindings, gateway)
    assert set(document) == {"schema", "generated_at", "runtime", "providers", "observatory"}
    assert document["schema"] == ECOSYSTEM_SCHEMA
    assert set(document["runtime"]) == {"version", "instance_id", "node_id", "base_url"}
    providers = document["providers"]
    assert set(providers) == {"default_generation_provider", "tier_providers",
                              "embedding_provider", "fallbacks", "status"}
    assert set(providers["tier_providers"]) == {"fast", "general", "code", "reasoning", "vision"}
    assert providers["fallbacks"] == {}
    assert set(providers["status"]) == {"sonder_inference", "ollama"}
    assert set(providers["status"]["sonder_inference"]) == STATUS_KEYS
    observatory = document["observatory"]
    assert set(observatory) == {"export_enabled", "runtime_stream", "stats", "cors_origins",
                                "connect_urls", "warnings"}
    assert set(observatory["runtime_stream"]) == {"discovery_url", "sse_url", "ndjson_url"}
    assert set(observatory["stats"]) == {"subscribers", "emitted_events", "dropped_events",
                                         "retained_events", "buffer_capacity"}
    assert observatory["connect_urls"] == ["http://127.0.0.1:18435", "http://127.0.0.1:18437"]
    assert any("synthetic" in warning for warning in observatory["warnings"])


def test_gateway_without_provider_status_reports_unknown():
    document = _build(_bindings(), SimpleNamespace())
    assert document["providers"]["status"] == {
        "openai_compatible": {"provider": "openai_compatible", "state": "unknown"},
        "ollama": {"provider": "ollama", "state": "unknown"},
    }
    assert document["observatory"]["connect_urls"] == ["http://127.0.0.1:18435"]


def test_providers_missing_from_the_report_are_unknown_and_bad_states_are_normalised():
    gateway = SimpleNamespace(provider_status=lambda: {
        "openai_compatible": {"state": "on fire", "healthy": False},
    })
    status = _build(_bindings(), gateway)["providers"]["status"]
    assert status["openai_compatible"]["state"] == "unknown"
    assert status["openai_compatible"]["provider"] == "openai_compatible"
    assert status["ollama"] == {"provider": "ollama", "state": "unknown"}


def test_a_failing_status_surface_is_reported_not_raised():
    def broken():
        raise RuntimeError("boom")

    status = _build(_bindings(), SimpleNamespace(provider_status=broken))["providers"]["status"]
    assert status["ollama"]["state"] == "unknown"
    assert "RuntimeError" in status["ollama"]["detail"]


def test_warnings_cover_embedding_origins_and_legacy_surfaces():
    document = _build(_bindings("sonder_inference", embedding="sonder_inference"),
                      SimpleNamespace(), origins=())
    warnings = " | ".join(document["observatory"]["warnings"])
    assert "SONDER_EMBEDDING_PROVIDER=ollama" in warnings
    assert "SONDER_OBSERVATORY_ORIGINS" in warnings
    assert "REPL, MCP" in warnings


def test_disabled_export_without_a_status_surface_is_not_found():
    assert _build(_bindings(), SimpleNamespace(), export=False) is None


def test_disabled_export_with_a_status_surface_still_reports_providers():
    gateway = SimpleNamespace(provider_status=lambda: {})
    document = _build(_bindings(), gateway, export=False)
    assert document["observatory"]["export_enabled"] is False
    assert document["observatory"]["runtime_stream"] is None
    assert document["observatory"]["connect_urls"] == []
