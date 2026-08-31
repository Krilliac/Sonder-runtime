from __future__ import annotations

import pytest

from sonder_runtime.bootstrap.provider_bindings import (
    PROVIDER_TIERS,
    ProviderBindings,
    normalize_provider,
    provider_bindings_from_env,
)


def test_aliases_and_unset_bindings_inherit_global_backend():
    bindings = provider_bindings_from_env({"SONDER_MODEL_BACKEND": " llamacpp "})

    assert bindings.default_generation_provider == "openai_compatible"
    assert dict(bindings.tier_providers) == {
        tier: "openai_compatible" for tier in PROVIDER_TIERS
    }
    assert bindings.embedding_provider == "openai_compatible"
    assert bindings.required_providers == frozenset({"openai_compatible"})


def test_mixed_profile_has_explicit_ollama_embeddings_and_content_free_status():
    bindings = provider_bindings_from_env(
        {
            "SONDER_MODEL_BACKEND": "ollama",
            "SONDER_FAST_PROVIDER": "openai-compatible",
            "SONDER_GENERAL_PROVIDER": "vllm",
            "SONDER_EMBEDDING_PROVIDER": "ollama",
            "SONDER_OPENAI_BASE_URL": "http://127.0.0.1:18080",
            "SONDER_OPENAI_API_KEY": "must-not-appear",
        }
    )

    assert bindings.tier_providers["fast"] == "openai_compatible"
    assert bindings.tier_providers["general"] == "openai_compatible"
    assert bindings.tier_providers["code"] == "ollama"
    assert bindings.embedding_provider == "ollama"
    assert bindings.required_providers == frozenset({"ollama", "openai_compatible"})
    assert bindings.status_projection() == {
        "default_generation_provider": "ollama",
        "tier_providers": {
            "fast": "openai_compatible",
            "general": "openai_compatible",
            "code": "ollama",
            "reasoning": "ollama",
            "vision": "ollama",
        },
        "embedding_provider": "ollama",
    }


@pytest.mark.parametrize("value", ["cloud", "unknown", "openai_compatible_typo"])
def test_unknown_nonblank_provider_fails_closed(value):
    with pytest.raises(ValueError, match="unknown model provider"):
        normalize_provider(value)


def test_uniform_constructor_normalizes_alias():
    bindings = ProviderBindings.uniform("openai")
    assert bindings.required_providers == frozenset({"openai_compatible"})


def test_required_providers_includes_an_otherwise_unreferenced_default():
    bindings = ProviderBindings(
        default_generation_provider="ollama",
        tier_providers={tier: "openai_compatible" for tier in PROVIDER_TIERS},
        embedding_provider="openai_compatible",
    )

    assert bindings.required_providers == frozenset({"ollama", "openai_compatible"})
