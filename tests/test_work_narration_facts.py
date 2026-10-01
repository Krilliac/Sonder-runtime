from types import SimpleNamespace

from sonder_runtime.adapters.observability.work_narration_facts import configured_host


def _runtime(provider, gateway):
    return SimpleNamespace(
        _bridge_provider_for_tier=lambda _tier: provider,
        _APP_GRAPH=SimpleNamespace(model_gateway=gateway),
    )


def test_ollama_runtime_endpoint_is_host_only():
    runtime = SimpleNamespace(BASE="http://user:secret@127.0.0.1:11434/api?x=1")
    assert configured_host(runtime, "code") == "ollama 127.0.0.1:11434"


def test_dispatch_provider_uses_loaded_sonder_settings_and_fallback():
    primary = SimpleNamespace(_settings_override=SimpleNamespace(
        base_url="https://user:secret@infer.example:443/v1?key=x"))
    fallback = SimpleNamespace(_settings_override=SimpleNamespace(
        base_url="http://127.0.0.1:11434/api"))
    selected = SimpleNamespace(
        _primary=primary, _fallback=fallback, _fallback_id="ollama")
    gateway = SimpleNamespace(_providers={"sonder_inference": selected})
    assert configured_host(_runtime("sonder_inference", gateway), "code") == (
        "sonder_inference infer.example:443 (fallback ollama 127.0.0.1:11434)"
    )


def test_openai_config_and_environment_fallback_are_supported(monkeypatch):
    gateway = SimpleNamespace(_providers={
        "openai": SimpleNamespace(_config=SimpleNamespace(
            base_url="https://api.openai.example/v1")),
    })
    assert configured_host(_runtime("openai", gateway), "code") == "openai api.openai.example"
    empty = SimpleNamespace(_providers={"openrouter": SimpleNamespace(_config=None)})
    monkeypatch.setenv("SONDER_OPENROUTER_BASE_URL", "https://key@router.example/v1")
    assert configured_host(_runtime("openrouter", empty), "code") == "openrouter router.example"


def test_invalid_or_missing_configuration_is_honestly_unknown(monkeypatch):
    gateway = SimpleNamespace(_providers={
        "sonder_inference": SimpleNamespace(_settings_override=SimpleNamespace(
            base_url="https://[bad")),
    })
    assert configured_host(_runtime("sonder_inference", gateway), "code") == ""
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    assert configured_host(SimpleNamespace(BASE="not a url"), "code") == ""
