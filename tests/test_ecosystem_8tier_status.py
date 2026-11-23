"""Tests for 8-tier ecosystem status, OpenAI-compatible provider_status, and additive metadata."""
from __future__ import annotations

from contextlib import contextmanager
import http.client
import json
import threading
from types import SimpleNamespace
import urllib.request

import pytest

from sonder_runtime.adapters import model_tool_policy as mtp
from sonder_runtime.adapters.inference.openai_compat_gateway import (
    OpenAICompatibleConfig,
    OpenAICompatibleGateway,
)
from sonder_runtime.adapters.model_request_admission import HostModelRequestAdmission
from sonder_runtime.adapters.observability.observatory_producer import ObservatoryProducer
from sonder_runtime.adapters.provider_bindings import (
    PROVIDER_TIERS,
    ProviderBindings,
    provider_bindings_from_env,
)
from sonder_runtime.adapters.provider_dispatch.gateway import ProviderDispatchGateway
from sonder_runtime.application.capabilities.observability import RedactingTelemetrySink
from sonder_runtime.application.observability.ecosystem_status import (
    CORE_PROVIDER_TIER_NAMES,
    ECOSYSTEM_SCHEMA,
    EXTENDED_PROVIDER_TIER_NAMES,
    PROVIDER_TIER_NAMES,
    build_ecosystem_status,
    image_generation_status_from_env,
    model_tool_policy_status_from_env,
)
from sonder_runtime.application.observability.runtime_telemetry import RuntimeTelemetry
from sonder_runtime.domain.tool_lane_models import ToolLaneFloor
from sonder_runtime.interfaces.http import serve as ts
from sonder_runtime.interfaces.http.facades import observatory_routes
from sonder_runtime.platform.logging import Redactor

ALL_8_TIERS = (
    "fast", "general", "code", "reasoning", "vision",
    "security", "creative", "draft",
)


def _build_doc(bindings, gateway, **extra):
    base = "http://127.0.0.1:18435"
    return build_ecosystem_status(
        generated_at="2026-11-23T09:00:00.000Z",
        runtime={"version": "0.10.0", "instance_id": "rt-0123456789ab", "node_id": "test-node"},
        bindings=bindings,
        gateway=gateway,
        export_enabled=True,
        runtime_stream={
            "discovery_url": base + "/.well-known/sonder-telemetry",
            "sse_url": base + "/v1/observability/events",
            "ndjson_url": base + "/v1/observability/events?format=ndjson",
        },
        stats={"subscribers": 0, "emitted_events": 1, "dropped_events": 0,
               "retained_events": 1, "buffer_capacity": 4096},
        observatory_origins=["http://127.0.0.1:4173"],
        dedicated_origins=["http://127.0.0.1:4173"],
        runtime_base_url=base,
        **extra,
    )


# ---------------------------------------------------------------------------
# 1. 8-tier PROVIDER_TIER_NAMES & backward compatibility
# ---------------------------------------------------------------------------

def test_provider_tier_names_include_all_eight_local_tiers_in_order():
    assert CORE_PROVIDER_TIER_NAMES == ("fast", "general", "code", "reasoning", "vision")
    assert EXTENDED_PROVIDER_TIER_NAMES == ("security", "creative", "draft")
    assert PROVIDER_TIER_NAMES == ALL_8_TIERS
    assert PROVIDER_TIER_NAMES == PROVIDER_TIERS


def test_full_8_tier_bindings_project_every_tier_and_bound_provider():
    bindings = provider_bindings_from_env({
        "SONDER_MODEL_BACKEND": "openai-compatible",
        "SONDER_FAST_PROVIDER": "openai-compatible",
        "SONDER_GENERAL_PROVIDER": "openai-compatible",
        "SONDER_CODE_PROVIDER": "openai-compatible",
        "SONDER_REASONING_PROVIDER": "ollama",
        "SONDER_VISION_PROVIDER": "ollama",
        "SONDER_SECURITY_PROVIDER": "sonder-inference",
        "SONDER_CREATIVE_PROVIDER": "ollama",
        "SONDER_DRAFT_PROVIDER": "openai-compatible",
        "SONDER_EMBEDDING_PROVIDER": "ollama",
    })
    doc = _build_doc(bindings, SimpleNamespace())
    assert tuple(doc["providers"]["tier_providers"].keys()) == ALL_8_TIERS
    assert doc["providers"]["tier_providers"] == {
        "fast": "openai_compatible",
        "general": "openai_compatible",
        "code": "openai_compatible",
        "reasoning": "ollama",
        "vision": "ollama",
        "security": "sonder_inference",
        "creative": "ollama",
        "draft": "openai_compatible",
    }
    assert set(doc["providers"]["status"]) == {"openai_compatible", "ollama", "sonder_inference"}


def test_legacy_5_tier_duck_typed_bindings_remain_backward_compatible():
    legacy_tiers = {
        "fast": "ollama",
        "general": "openai_compatible",
        "code": "openai_compatible",
        "reasoning": "ollama",
        "vision": "ollama",
    }
    # Both projection-method and plain-attribute legacy bindings objects must work.
    projected_legacy = SimpleNamespace(
        default_generation_provider="openai_compatible",
        tier_providers=legacy_tiers,
        embedding_provider="ollama",
        fallbacks={},
        status_projection=lambda: {
            "default_generation_provider": "openai_compatible",
            "tier_providers": dict(legacy_tiers),
            "embedding_provider": "ollama",
            "fallbacks": {},
        },
    )
    plain_legacy = SimpleNamespace(
        default_generation_provider="sonder_inference",
        tier_providers=legacy_tiers,
        embedding_provider="ollama",
        fallbacks={},
    )
    doc_a = _build_doc(projected_legacy, SimpleNamespace())
    assert doc_a["providers"]["tier_providers"] == {
        **legacy_tiers,
        "security": "openai_compatible",
        "creative": "openai_compatible",
        "draft": "openai_compatible",
    }

    doc_b = _build_doc(plain_legacy, SimpleNamespace())
    assert doc_b["providers"]["tier_providers"] == {
        **legacy_tiers,
        "security": "sonder_inference",
        "creative": "sonder_inference",
        "draft": "sonder_inference",
    }


# ---------------------------------------------------------------------------
# 2. OpenAICompatibleGateway.provider_status() & served_tier_models()
# ---------------------------------------------------------------------------

def test_openai_compat_provider_status_reports_tier_models_base_url_and_model_without_io(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("provider_status must never make network calls")

    monkeypatch.setenv("SONDER_OPENAI_BASE_URL", "http://127.0.0.1:18080")
    monkeypatch.setenv("SONDER_OPENAI_MODEL", "bonsai-27b-pq2")
    monkeypatch.setenv("SONDER_OPENAI_EMBED_MODEL", "nomic-embed-text")
    monkeypatch.setenv(
        "SONDER_OPENAI_TIER_MODELS",
        "fast=bonsai-9b-mtp,general=bonsai-27b-pq2,code=bonsai-27b-pq2,draft=bonsai-4b-mtp",
    )
    monkeypatch.delenv("SONDER_OPENAI_API_KEY", raising=False)

    gateway = OpenAICompatibleGateway(
        transport=explode,
        get_transport=explode,
        request_admission=HostModelRequestAdmission(None),
    )
    status_map = gateway.provider_status()
    assert set(status_map) == {"openai_compatible"}
    entry = status_map["openai_compatible"]
    assert entry["provider"] == "openai_compatible"
    assert entry["state"] == "unknown"
    assert entry["healthy"] is None
    assert entry["configured"] is True
    assert entry["base_url"] == "http://127.0.0.1:18080"
    assert entry["loopback"] is True
    assert entry["model"] == "bonsai-27b-pq2"
    assert entry["embed_model"] == "nomic-embed-text"
    assert entry["api_key_configured"] is False
    assert entry["tier_models"] == {
        "fast": "bonsai-9b-mtp",
        "general": "bonsai-27b-pq2",
        "code": "bonsai-27b-pq2",
        "reasoning": "bonsai-27b-pq2",
        "vision": "bonsai-27b-pq2",
        "security": "bonsai-27b-pq2",
        "creative": "bonsai-27b-pq2",
        "draft": "bonsai-4b-mtp",
    }
    assert entry["models"] == ["bonsai-27b-pq2", "bonsai-9b-mtp", "bonsai-4b-mtp"]
    assert "not probed" in entry["detail"]
    assert gateway.served_tier_models() == {"openai_compatible": entry["tier_models"]}


def test_openai_compat_provider_status_tier_models_only_without_default_model():
    cfg = OpenAICompatibleConfig(
        base_url="http://127.0.0.1:18080",
        model="",
        tier_models={"fast": "bonsai-9b-mtp", "draft": "bonsai-4b-mtp"},
    )
    gw = OpenAICompatibleGateway(cfg, request_admission=HostModelRequestAdmission(None))
    entry = gw.provider_status()["openai_compatible"]
    assert entry["configured"] is True
    assert entry["model"] is None
    assert entry["tier_models"] == {"fast": "bonsai-9b-mtp", "draft": "bonsai-4b-mtp"}
    assert entry["models"] == ["bonsai-9b-mtp", "bonsai-4b-mtp"]
    assert gw.served_tier_models() == {
        "openai_compatible": {"fast": "bonsai-9b-mtp", "draft": "bonsai-4b-mtp"},
    }


def test_openai_compat_provider_status_unconfigured_and_remote_transport_safety(monkeypatch):
    monkeypatch.delenv("SONDER_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("SONDER_OPENAI_MODEL", raising=False)
    monkeypatch.delenv("SONDER_OPENAI_TIER_MODELS", raising=False)
    monkeypatch.delenv("SONDER_OPENAI_API_KEY", raising=False)

    unconfigured = OpenAICompatibleGateway(request_admission=HostModelRequestAdmission(None))
    row = unconfigured.provider_status()["openai_compatible"]
    assert row["state"] == "unavailable"
    assert row["healthy"] is False
    assert row["configured"] is False
    assert row["base_url"] is None
    assert "SONDER_OPENAI_BASE_URL" in row["detail"]
    assert unconfigured.served_tier_models() == {}

    # Remote plaintext HTTP is rejected cleanly without raising.
    plaintext_remote = OpenAICompatibleGateway(
        OpenAICompatibleConfig(base_url="http://remote.example.com/v1", model="m1"),
        request_admission=HostModelRequestAdmission(None),
        spend_ledger=None,
    )
    bad = plaintext_remote.provider_status()["openai_compatible"]
    assert bad["state"] == "unavailable"
    assert bad["healthy"] is False
    assert bad["configured"] is False
    assert bad["loopback"] is False
    assert "https://" in bad["detail"]

    # Remote HTTPS with API key never leaks the key.
    secret = "sk-test-super-secret-token-987654"
    https_remote = OpenAICompatibleGateway(
        OpenAICompatibleConfig(
            base_url="https://api.example.com/v1",
            api_key=secret,
            model="remote-model",
            tier_models={"code": "remote-code"},
        ),
        request_admission=HostModelRequestAdmission(None),
        spend_ledger=None,
    )
    ok = https_remote.provider_status()["openai_compatible"]
    assert ok["state"] == "unknown"
    assert ok["healthy"] is None
    assert ok["configured"] is True
    assert ok["loopback"] is False
    assert ok["base_url"] == "https://api.example.com/v1"
    assert ok["api_key_configured"] is True
    assert secret not in json.dumps(ok)


def test_openai_compat_provider_status_redacts_url_credentials_and_handles_bad_env(monkeypatch):
    cred_gw = OpenAICompatibleGateway(
        OpenAICompatibleConfig(
            base_url="https://alice:hunter2secret@api.example.com/v1?api_key=qsecret",
            model="m1",
        ),
        request_admission=HostModelRequestAdmission(None),
        spend_ledger=None,
    )
    row = cred_gw.provider_status()["openai_compatible"]
    dumped = json.dumps(row)
    assert row["state"] == "unavailable"
    assert row["configured"] is False
    assert row["base_url"] is None
    assert "hunter2secret" not in dumped
    assert "qsecret" not in dumped

    # Invalid env value never raises out of provider_status().
    monkeypatch.setenv("SONDER_OPENAI_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("SONDER_OPENAI_DAILY_USD", "not-a-number")
    bad_env_gw = OpenAICompatibleGateway(request_admission=HostModelRequestAdmission(None))
    err_row = bad_env_gw.provider_status()["openai_compatible"]
    assert err_row["state"] == "unavailable"
    assert err_row["configured"] is False
    assert "SONDER_OPENAI_DAILY_USD" in err_row["detail"]


def test_provider_dispatch_surfaces_openai_compat_tier_models_in_ecosystem_status():
    openai_gw = OpenAICompatibleGateway(
        OpenAICompatibleConfig(
            base_url="http://127.0.0.1:18080",
            model="bonsai-27b-pq2",
            tier_models={
                "fast": "bonsai-9b-mtp",
                "general": "bonsai-27b-pq2",
                "code": "bonsai-27b-pq2",
                "draft": "bonsai-4b-mtp",
            },
        ),
        request_admission=HostModelRequestAdmission(None),
    )
    bindings = provider_bindings_from_env({
        "SONDER_MODEL_BACKEND": "openai-compatible",
        "SONDER_EMBEDDING_PROVIDER": "ollama",
        "SONDER_REASONING_PROVIDER": "ollama",
        "SONDER_VISION_PROVIDER": "ollama",
    })
    dispatch = ProviderDispatchGateway(
        providers={"openai_compatible": openai_gw, "ollama": SimpleNamespace()},
        tier_providers=bindings.tier_providers,
        default_generation_provider=bindings.default_generation_provider,
        embedding_provider=bindings.embedding_provider,
    )
    doc = _build_doc(bindings, dispatch)
    oai = doc["providers"]["status"]["openai_compatible"]
    assert oai["base_url"] == "http://127.0.0.1:18080"
    assert oai["model"] == "bonsai-27b-pq2"
    assert oai["tier_models"]["fast"] == "bonsai-9b-mtp"
    assert oai["tier_models"]["draft"] == "bonsai-4b-mtp"
    assert dispatch.served_tier_models()["openai_compatible"]["fast"] == "bonsai-9b-mtp"


# ---------------------------------------------------------------------------
# 3. Additive image_generation and model_tool_policy metadata
# ---------------------------------------------------------------------------

def test_image_generation_metadata_env_variants_and_safety():
    # Unconfigured
    assert image_generation_status_from_env({}) == {
        "configured": False,
        "backend": None,
        "api_url": None,
        "model": None,
        "loopback": None,
    }

    # Loopback HTTP dedicated image server + model
    assert image_generation_status_from_env({
        "SONDER_IMAGE_API_URL": "http://127.0.0.1:11440/",
        "SONDER_IMAGE_MODEL": "sdxl-turbo",
        "SONDER_IMAGE_API_KEY": "img-secret-do-not-expose",
    }) == {
        "configured": True,
        "backend": "openai_api",
        "api_url": "http://127.0.0.1:11440",
        "model": "sdxl-turbo",
        "loopback": True,
    }

    # IPv6 loopback + path + stripped query/fragment
    ipv6 = image_generation_status_from_env({
        "SONDER_IMAGE_API_URL": "http://[::1]:11440/v1?token=secret#frag",
    })
    assert ipv6 == {
        "configured": True,
        "backend": "openai_api",
        "api_url": "http://[::1]:11440/v1",
        "model": None,
        "loopback": True,
    }
    assert "secret" not in json.dumps(ipv6)

    # Safe remote HTTPS
    assert image_generation_status_from_env({
        "SONDER_IMAGE_API_URL": "https://images.example.com/v1",
        "SONDER_IMAGE_MODEL": "flux-schnell",
    }) == {
        "configured": True,
        "backend": "openai_api",
        "api_url": "https://images.example.com/v1",
        "model": "flux-schnell",
        "loopback": False,
    }

    # Ollama image model without API URL
    assert image_generation_status_from_env({
        "SONDER_IMAGE_MODEL": "x/z-image-turbo",
    }) == {
        "configured": True,
        "backend": "ollama",
        "api_url": None,
        "model": "x/z-image-turbo",
        "loopback": None,
    }


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "http://192.168.1.44:11440",
        "http://images.example.com:11440",
        "http://0.0.0.0:11440",
        "http://user:pass@127.0.0.1:11440",
        "https://user:pass@images.example.com/v1",
        "ftp://127.0.0.1:11440",
        "http://127.0.0.1:99999",
        "http://127.0.0.1:11440\r\nX-Test: 1",
    ],
)
def test_image_generation_metadata_rejects_unsafe_or_credentialed_urls(unsafe_url):
    result = image_generation_status_from_env({"SONDER_IMAGE_API_URL": unsafe_url})
    assert result == {
        "configured": False,
        "backend": None,
        "api_url": None,
        "model": None,
        "loopback": None,
    }
    # If an explicit SONDER_IMAGE_MODEL is also set, fall back to ollama cleanly.
    with_model = image_generation_status_from_env({
        "SONDER_IMAGE_API_URL": unsafe_url,
        "SONDER_IMAGE_MODEL": "x/z-image-turbo",
    })
    assert with_model == {
        "configured": True,
        "backend": "ollama",
        "api_url": None,
        "model": "x/z-image-turbo",
        "loopback": None,
    }


@pytest.mark.parametrize("bad_ack", ["", "1", "true", "TRUE", "yes", "i_accept_security_model_tool_risk"])
def test_model_tool_policy_metadata_fails_closed_on_missing_or_shorthand_ack(bad_ack):
    status = model_tool_policy_status_from_env(
        {mtp.ALLOW_SECURITY_MODEL_TOOLS_ENV: bad_ack},
    )
    assert status == {"security_floor_relaxed": False}


def test_model_tool_policy_metadata_matches_adapter_load_and_fails_closed_when_floor_unavailable(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv(mtp.OVERLAY_ENV, str(tmp_path / "overlay" / "model_tool_policy.json"))
    (tmp_path / "overlay").mkdir()
    mtp.clear_cache()
    try:
        monkeypatch.delenv(mtp.ALLOW_SECURITY_MODEL_TOOLS_ENV, raising=False)
        assert model_tool_policy_status_from_env({}) == {
            "security_floor_relaxed": bool(mtp.load().security_floor_relaxed),
        }

        monkeypatch.setenv(mtp.ALLOW_SECURITY_MODEL_TOOLS_ENV, mtp.ALLOW_SECURITY_MODEL_TOOLS_ACK)
        mtp.clear_cache()
        assert mtp.load().security_floor_relaxed is True
        assert model_tool_policy_status_from_env(
            {mtp.ALLOW_SECURITY_MODEL_TOOLS_ENV: mtp.ALLOW_SECURITY_MODEL_TOOLS_ACK},
            floor_available=True,
        ) == {"security_floor_relaxed": True}
        assert model_tool_policy_status_from_env(
            {mtp.ALLOW_SECURITY_MODEL_TOOLS_ENV: mtp.ALLOW_SECURITY_MODEL_TOOLS_ACK},
            floor_available=False,
        ) == {"security_floor_relaxed": False}
    finally:
        mtp.clear_cache()


def test_ecosystem_document_fails_closed_when_application_floor_unavailable(monkeypatch):
    monkeypatch.setenv(mtp.ALLOW_SECURITY_MODEL_TOOLS_ENV, mtp.ALLOW_SECURITY_MODEL_TOOLS_ACK)
    producer = ObservatoryProducer(version="0.10.0", node_id="n", instance_hex="0123456789ab")
    app = SimpleNamespace(
        provider_bindings=ProviderBindings.uniform("ollama"),
        model_gateway=SimpleNamespace(),
        runtime_policy=SimpleNamespace(
            _floor_provider=lambda: ToolLaneFloor.unavailable("tampered"),
        ),
    )
    doc = observatory_routes.ecosystem_document(
        app,
        producer,
        base="http://127.0.0.1:18435",
        version="0.10.0",
        generated_at="2026-11-23T09:00:00.000Z",
        node="n",
        observatory_origins=["http://127.0.0.1:4173"],
        dedicated_origins=["http://127.0.0.1:4173"],
    )
    assert doc["model_tool_policy"] == {"security_floor_relaxed": False}


# ---------------------------------------------------------------------------
# 4. End-to-end HTTP GET /v1/sonder/ecosystem with zero network probes
# ---------------------------------------------------------------------------

@contextmanager
def _serve(monkeypatch, application):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(ts, "CORS_ORIGINS", frozenset())
    monkeypatch.setattr(ts, "OBSERVATORY_ORIGINS", frozenset({"http://127.0.0.1:4173"}))
    monkeypatch.setattr(ts, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(ts, "_live_telemetry_application", lambda *, build=False: application)
    httpd = ts.ThreadingHTTPServer(("127.0.0.1", 0), ts.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(ts, "BOUND_PORT", httpd.server_address[1])
    try:
        yield httpd.server_address[1]
    finally:
        application.producer.close_subscribers()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_http_ecosystem_route_exposes_8_tiers_openai_models_image_and_policy_without_probing(
    monkeypatch,
):
    def no_network(*args, **kwargs):
        raise AssertionError("GET /v1/sonder/ecosystem must never initiate outbound network probes")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    monkeypatch.setattr(urllib.request, "build_opener", no_network)
    monkeypatch.setenv("SONDER_OPENAI_BASE_URL", "http://127.0.0.1:18080")
    monkeypatch.setenv("SONDER_OPENAI_MODEL", "bonsai-27b-pq2")
    monkeypatch.setenv(
        "SONDER_OPENAI_TIER_MODELS",
        "fast=bonsai-9b-mtp,general=bonsai-27b-pq2,code=bonsai-27b-pq2,draft=bonsai-4b-mtp",
    )
    monkeypatch.setenv("SONDER_IMAGE_API_URL", "http://127.0.0.1:11440")
    monkeypatch.setenv("SONDER_IMAGE_MODEL", "sdxl-turbo")
    monkeypatch.setenv("SONDER_IMAGE_API_KEY", "secret-image-key")
    monkeypatch.setenv(mtp.ALLOW_SECURITY_MODEL_TOOLS_ENV, mtp.ALLOW_SECURITY_MODEL_TOOLS_ACK)

    bindings = provider_bindings_from_env({
        "SONDER_MODEL_BACKEND": "openai-compatible",
        "SONDER_REASONING_PROVIDER": "ollama",
        "SONDER_VISION_PROVIDER": "ollama",
        "SONDER_SECURITY_PROVIDER": "ollama",
        "SONDER_CREATIVE_PROVIDER": "ollama",
        "SONDER_EMBEDDING_PROVIDER": "ollama",
    })
    openai_gw = OpenAICompatibleGateway(request_admission=HostModelRequestAdmission(None))
    dispatch = ProviderDispatchGateway(
        providers={"openai_compatible": openai_gw, "ollama": SimpleNamespace()},
        tier_providers=bindings.tier_providers,
        default_generation_provider=bindings.default_generation_provider,
        embedding_provider=bindings.embedding_provider,
    )
    producer = ObservatoryProducer(
        version="0.10.0", node_id="test-host", instance_hex="0123456789ab",
    )
    telemetry = RuntimeTelemetry(
        RedactingTelemetrySink(producer, Redactor()),
        session_id=producer.session_id,
        version="0.10.0",
    )
    app = SimpleNamespace(
        telemetry=telemetry,
        telemetry_feed=producer,
        provider_bindings=bindings,
        model_gateway=dispatch,
        runtime_policy=SimpleNamespace(_floor_provider=lambda: ToolLaneFloor(frozenset(), (), frozenset())),
        producer=producer,
    )

    with _serve(monkeypatch, app) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        conn.request("GET", "/v1/sonder/ecosystem")
        resp = conn.getresponse()
        body = resp.read()
        conn.close()

    assert resp.status == 200
    assert b"secret-image-key" not in body
    doc = json.loads(body)
    assert doc["schema"] == ECOSYSTEM_SCHEMA
    assert tuple(doc["providers"]["tier_providers"].keys()) == ALL_8_TIERS
    assert doc["providers"]["tier_providers"]["draft"] == "openai_compatible"
    assert doc["providers"]["tier_providers"]["security"] == "ollama"
    oai = doc["providers"]["status"]["openai_compatible"]
    assert oai["base_url"] == "http://127.0.0.1:18080"
    assert oai["model"] == "bonsai-27b-pq2"
    assert oai["tier_models"]["fast"] == "bonsai-9b-mtp"
    assert oai["tier_models"]["draft"] == "bonsai-4b-mtp"
    assert doc["image_generation"] == {
        "configured": True,
        "backend": "openai_api",
        "api_url": "http://127.0.0.1:11440",
        "model": "sdxl-turbo",
        "loopback": True,
    }
    assert doc["model_tool_policy"] == {"security_floor_relaxed": True}
