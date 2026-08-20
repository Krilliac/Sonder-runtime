import pytest

from sonder_runtime.domain.runtime_model_configuration import (
    RuntimeModelConfiguration,
)


def test_default_projection_preserves_server_model_and_tier_defaults():
    config = RuntimeModelConfiguration.from_environment({})

    assert config.stable_alias == "sonder:latest"
    assert config.local_code_model == "sonder:latest"
    assert config.default_cloud_code_model == "kimi-k2.7-code:cloud"
    assert config.default_cloud_general_model == "glm-5.2:cloud"
    assert config.cloud_extra_usage_fallback_model == "kimi-k2.7-code:cloud"
    assert config.retired_cloud_models == frozenset({"qwen3-coder:480b-cloud"})
    assert config.tier_bindings == (
        ("fast", "sonder:latest"),
        ("code", "sonder:latest"),
        ("general", "sonder:latest"),
        ("reasoning", ""),
        ("vision", ""),
        ("cloud-code", "kimi-k2.7-code:cloud"),
        ("cloud-general", "glm-5.2:cloud"),
    )
    assert config.cloud_tiers == ("cloud-code", "cloud-general")


def test_projection_applies_environment_overrides_and_retired_fallbacks():
    config = RuntimeModelConfiguration.from_environment(
        {
            "SONDER_CODE_LOCAL": "local-code:latest",
            "SONDER_FAST": "fast-model:latest",
            "SONDER_CODE": "code-model:latest",
            "SONDER_GENERAL": "general-model:latest",
            "SONDER_REASONING": "reasoner:latest",
            "SONDER_VISION": "vision-model:latest",
            "SONDER_CLOUD_CODE": "qwen3-coder:480b-cloud",
            "SONDER_CLOUD_GENERAL": "custom:cloud",
        }
    )

    assert config.local_code_model == "local-code:latest"
    assert dict(config.tier_bindings) == {
        "fast": "fast-model:latest",
        "code": "code-model:latest",
        "general": "general-model:latest",
        "reasoning": "reasoner:latest",
        "vision": "vision-model:latest",
        "cloud-code": "kimi-k2.7-code:cloud",
        "cloud-general": "custom:cloud",
    }


def test_projection_is_immutable_but_exposes_an_independent_compatibility_seed():
    config = RuntimeModelConfiguration.from_environment({})

    with pytest.raises((AttributeError, TypeError)):
        config.stable_alias = "changed"  # type: ignore[misc]

    seed = config.tier_map()
    seed["code"] = "temporary-test-binding"
    assert dict(config.tier_bindings)["code"] == "sonder:latest"
