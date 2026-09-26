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
        "fallbacks": {},
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


@pytest.mark.parametrize(
    "alias",
    ["sonder-inference", "sonder_inference", "sonder-infer", "inference", " Sonder-Inference "],
)
def test_sonder_inference_aliases_normalize_to_one_provider(alias):
    assert normalize_provider(alias) == "sonder_inference"


def test_sonder_is_a_tier_name_not_a_provider():
    with pytest.raises(ValueError, match="unknown model provider 'sonder'.*sonder-inference"):
        normalize_provider("sonder")
    with pytest.raises(ValueError, match="unknown model provider"):
        provider_bindings_from_env({"SONDER_MODEL_BACKEND": "sonder"})


def test_inference_fallback_is_parsed_and_required_but_not_bound():
    bindings = provider_bindings_from_env({
        "SONDER_MODEL_BACKEND": "sonder-inference",
        "SONDER_EMBEDDING_PROVIDER": "ollama",
        "SONDER_INFERENCE_FALLBACK": " OLLAMA ",
    })
    assert dict(bindings.fallbacks) == {"sonder_inference": "ollama"}
    assert bindings.required_providers == frozenset({"sonder_inference", "ollama"})
    assert bindings.constructed_providers == frozenset({"sonder_inference", "ollama"})
    assert bindings.status_projection()["fallbacks"] == {"sonder_inference": "ollama"}

    only_inference = provider_bindings_from_env({
        "SONDER_MODEL_BACKEND": "sonder-inference",
        "SONDER_INFERENCE_FALLBACK": "ollama",
    })
    # The fallback target is constructed, but it is not a routable binding:
    # required_providers keeps its routing meaning (bootstrap's strict
    # local-alias gate reads it), so it must not grow with the fallback.
    assert only_inference.bound_providers == frozenset({"sonder_inference"})
    assert only_inference.required_providers == frozenset({"sonder_inference"})
    assert only_inference.constructed_providers == frozenset({"sonder_inference", "ollama"})


@pytest.mark.parametrize("value", ["", "none", "NONE"])
def test_inference_fallback_defaults_to_none(value):
    bindings = provider_bindings_from_env({
        "SONDER_MODEL_BACKEND": "sonder-inference",
        "SONDER_INFERENCE_FALLBACK": value,
    })
    assert dict(bindings.fallbacks) == {}


@pytest.mark.parametrize("value", ["openai", "cloud", "1", "ollama,openai"])
def test_unknown_fallback_values_fail_closed(value):
    with pytest.raises(ValueError, match="SONDER_INFERENCE_FALLBACK"):
        provider_bindings_from_env({
            "SONDER_MODEL_BACKEND": "sonder-inference",
            "SONDER_INFERENCE_FALLBACK": value,
        })


def test_fallback_without_an_inference_binding_fails_closed():
    with pytest.raises(ValueError, match="no generation tier is bound to sonder_inference"):
        provider_bindings_from_env({"SONDER_INFERENCE_FALLBACK": "ollama"})


def test_only_sonder_inference_to_ollama_fallback_is_constructible():
    with pytest.raises(ValueError, match="unsupported provider fallback"):
        ProviderBindings(
            default_generation_provider="openai_compatible",
            tier_providers={tier: "openai_compatible" for tier in PROVIDER_TIERS},
            embedding_provider="ollama",
            fallbacks={"openai_compatible": "ollama"},
        )
    with pytest.raises(ValueError, match="serves no generation binding"):
        ProviderBindings(
            default_generation_provider="ollama",
            tier_providers={tier: "ollama" for tier in PROVIDER_TIERS},
            embedding_provider="sonder_inference",
            fallbacks={"sonder_inference": "ollama"},
        )


def test_dispatch_labels_map_to_binding_ids():
    from sonder_runtime.adapters.provider_bindings import provider_id_for_label

    assert provider_id_for_label("ollama") == "ollama"
    assert provider_id_for_label("openai-compatible") == "openai_compatible"
    assert provider_id_for_label("sonder-inference") == "sonder_inference"
    with pytest.raises(ValueError):
        provider_id_for_label("sonder_inference")
