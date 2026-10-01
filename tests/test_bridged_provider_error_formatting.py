"""Provider identity survives the legacy bridge without changing retry policy."""
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters import legacy_chat_bridge
from sonder_runtime.adapters.inference.openai_compat_gateway import (
    OpenAICompatibleConfig,
    OpenAICompatibleGateway,
)
from sonder_runtime.adapters.inference.openrouter_gateway import OpenRouterSettings
from sonder_runtime.adapters.inference.sonder_inference_gateway import (
    SonderInferenceConfig,
    SonderInferenceUnreachable,
)
from sonder_runtime.adapters.model_error_formatting import format_runtime_model_call_error
from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.adapters.provider_dispatch.fallback import PreSendFallbackGateway
from sonder_runtime.adapters.provider_dispatch.gateway import ProviderDispatchGateway
from sonder_runtime.application.chat.provider_bridge import (
    UnsupportedProviderFeature,
    bind_rung,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelResponse
from sonder_runtime.domain.common.errors import (
    Cancelled,
    CapacityExceeded,
    DeadlineExceeded,
    DependencyUnavailable,
    Forbidden,
    InvalidInput,
    SonderError,
)


def _bridge_error(gateway, provider="sonder_inference"):
    with bind_rung(provider, "code") as rung:
        with pytest.raises(ModelCallError) as caught:
            legacy_chat_bridge.chat_request(
                gateway, {"messages": [{"role": "user", "content": "probe"}]}, rung,
                context=local_owner_context(correlation_id="provider-error-test"),
            )
    return caught.value


def _render(error):
    return format_runtime_model_call_error(
        error, endpoint_loopback=False, display="https://unrelated-ollama.invalid",
    )


def _failing_gateway(settings, failure=None):
    def generate(request, context):
        raise failure if failure is not None else DependencyUnavailable("connection refused")

    return SimpleNamespace(settings=lambda: settings, generate=generate)


@pytest.mark.parametrize("base_url", ["http://127.0.0.1:11437", "http://127.0.0.1:21437"])
@pytest.mark.parametrize("shape", ["direct", "dispatch", "fallback", "dispatch-fallback"])
def test_error_uses_bound_gateway_through_wrappers(base_url, shape, monkeypatch):
    monkeypatch.setenv("SONDER_INFERENCE_BASE_URL", "http://127.0.0.1:31437")
    gateway = _failing_gateway(SonderInferenceConfig(base_url=base_url))
    unrelated = _failing_gateway(SonderInferenceConfig(base_url="http://127.0.0.1:41437"))
    if "fallback" in shape:
        gateway = PreSendFallbackGateway(gateway, fallback=unrelated)
    if "dispatch" in shape:
        gateway = ProviderDispatchGateway(
            providers={"sonder_inference": gateway, "ollama": unrelated},
            tier_providers={"code": "sonder_inference"},
            default_generation_provider="ollama", embedding_provider="ollama",
        )
    error = _bridge_error(gateway)
    assert error.provider == "sonder_inference"
    assert error.provider_display_url == base_url
    assert _render(error) == (
        f"ERROR contacting sonder_inference at {base_url} after 1 attempt(s): "
        "provider sonder_inference is unavailable: connection refused"
    )


@pytest.mark.parametrize("failure,kind,status,transient", [
    (DependencyUnavailable("down"), "provider_unavailable", 503, False),
    (UnsupportedProviderFeature("unsupported"), "unsupported_feature", 400, False),
    (InvalidInput("invalid"), "configuration", 400, False),
    (Forbidden("forbidden"), "configuration", 403, False),
    (DeadlineExceeded("late"), "timeout", None, True),
    (Cancelled("cancelled"), "cancelled", None, False),
    (CapacityExceeded("full"), "request", 429, True),
    (SonderError("failed"), "request", 502, False),
])
def test_bridge_preserves_error_classification_and_cause(failure, kind, status, transient):
    error = _bridge_error(_failing_gateway(SonderInferenceConfig(), failure))
    assert (error.kind, error.status, error.transient, error.attempts, error.cloud) == (
        kind, status, transient, 1, False,
    )
    assert error.__cause__ is failure
    assert error.retry_after_seconds is None
    assert _render(error).startswith("ERROR contacting sonder_inference at ")


def test_remote_inference_uses_safe_display_url_without_private_path():
    settings = SonderInferenceConfig(base_url="https://inference.example/private-path")
    error = _bridge_error(_failing_gateway(settings))
    assert error.provider_display_url == "https://inference.example"
    assert "private-path" not in _render(error)


def test_failed_fallback_keeps_bound_primary_identity_and_both_causes():
    settings = SonderInferenceConfig(base_url="http://127.0.0.1:21437")
    gateway = PreSendFallbackGateway(
        _failing_gateway(settings, SonderInferenceUnreachable("primary refused")),
        fallback=_failing_gateway(None, DependencyUnavailable("fallback refused")),
    )
    error = _bridge_error(gateway)
    assert gateway.fallback_count == 1
    assert error.provider_display_url == settings.display_base_url
    assert (error.kind, error.status, error.transient, error.attempts, error.cloud) == (
        "provider_unavailable", 503, False, 1, False,
    )
    assert _render(error) == (
        "ERROR contacting sonder_inference at http://127.0.0.1:21437 after 1 attempt(s): "
        "provider sonder_inference is unavailable: fallback refused "
        "(fallback to ollama after: primary refused)"
    )


def test_openrouter_uses_its_bound_settings():
    settings = OpenRouterSettings(base_url="https://router.example/v1")
    error = _bridge_error(_failing_gateway(settings), "openrouter")
    assert error.provider_display_url == settings.display_base_url
    assert _render(error).startswith("ERROR contacting openrouter at https://router.example/v1")
    assert error.cloud is False  # The bridge's existing classification stays intact.


@pytest.mark.parametrize("base_url,display", [
    ("http://127.0.0.1:32123/v1", "http://127.0.0.1:32123"),
    ("https://user:fake-password@api.example:9443/private?key=fake#secret", "https://api.example:9443"),
    ("http://[::1]:32123/v1", "http://[::1]:32123"),
])
def test_openai_compatible_uses_bound_config_and_scrubs_url(base_url, display):
    class FailingGateway(OpenAICompatibleGateway):
        def generate(self, request, context):
            raise DependencyUnavailable("down")

    gateway = FailingGateway(OpenAICompatibleConfig(base_url=base_url, model="test"))
    error = _bridge_error(gateway, "openai_compatible")
    assert error.provider_display_url == display
    assert _render(error).startswith(f"ERROR contacting openai_compatible at {display} ")


@pytest.mark.parametrize("mode", ["missing", "raises", "cycle", "malformed"])
def test_unavailable_endpoint_metadata_does_not_mask_original_error(mode):
    gateway = _failing_gateway(SimpleNamespace(base_url="http://[bad"))
    if mode == "missing":
        del gateway.settings
    elif mode == "raises":
        def settings():
            raise InvalidInput("fake private configuration detail")
        gateway.settings = settings
    elif mode == "cycle":
        gateway = PreSendFallbackGateway(gateway, fallback=object())
        gateway._primary = gateway
        gateway.generate = lambda request, context: (_ for _ in ()).throw(
            DependencyUnavailable("connection refused")
        )
    error = _bridge_error(gateway)
    assert (error.kind, error.status, error.attempts, error.cloud) == (
        "provider_unavailable", 503, 1, False,
    )
    assert error.provider_display_url == "(endpoint unavailable)"
    assert _render(error).startswith("ERROR contacting sonder_inference at (endpoint unavailable)")
    assert "private" not in _render(error)


def test_success_does_not_resolve_error_display_metadata():
    def settings():
        pytest.fail("a successful call must not resolve error metadata")

    gateway = SimpleNamespace(
        settings=settings,
        generate=lambda request, context: ModelResponse(text="ok", model="test", tier="code"),
    )
    with bind_rung("sonder_inference", "code") as rung:
        out, text = legacy_chat_bridge.chat_request(
            gateway, {"messages": [{"role": "user", "content": "probe"}]}, rung,
            context=local_owner_context(correlation_id="success-test"),
        )
    assert text == "ok"
    assert out == {"model": "test", "message": {"role": "assistant", "content": "ok"}, "done": True}


@pytest.mark.parametrize("cloud,loopback,target", [
    (False, True, "local Ollama"),
    (False, False, "remote Ollama"),
    (True, False, "hosted Ollama"),
])
@pytest.mark.parametrize("kind", [
    "budget", "rate", "http", "configuration", "protocol", "empty_response",
    "request", "cancelled", "timeout", "connection", "unknown",
])
def test_ollama_error_output_remains_byte_identical(cloud, loopback, target, kind):
    error = ModelCallError(kind, "failed", cloud=cloud, status=503, attempts=2, retry_after_seconds=2.4)
    expected = {
        "budget": "ERROR: hosted agent output budget exhausted: failed",
        "rate": "ERROR: model request rate admission refused: failed. Retry after about 2s.",
        "http": f"ERROR: {target} rejected the model request (HTTP 503) after 2 attempt(s): failed",
        "configuration": "ERROR: failed",
        "cancelled": "ERROR: failed",
    }
    for response_kind in ("protocol", "empty_response", "request"):
        expected[response_kind] = f"ERROR: invalid response from {target} after 2 attempt(s): failed"
    if cloud:
        expected["http"] += (
            " Cloud calls are not retried automatically to avoid duplicate metered work."
            " Provider suggests retrying after about 2s."
        )
    rendered = format_runtime_model_call_error(error, endpoint_loopback=loopback, display="endpoint")
    assert rendered == expected.get(
        kind, f"ERROR contacting {target} at endpoint after 2 attempt(s): failed",
    )
