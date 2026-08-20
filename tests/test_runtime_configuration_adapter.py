from sonder_runtime.adapters.runtime_configuration import (
    RuntimeConfig,
    build_config_from_env,
)
from sonder_runtime.bootstrap.container import RuntimeConfig as BootstrapConfig
from sonder_runtime.bootstrap.main import build_config_from_env as bootstrap_builder


def test_runtime_config_owns_bootstrap_configuration_identity():
    assert BootstrapConfig is RuntimeConfig
    assert bootstrap_builder is build_config_from_env


def test_build_config_from_env_normalizes_backend_and_preserves_home():
    config = build_config_from_env(
        "test-profile",
        {"SONDER_MODEL_BACKEND": "  OPENAI-Compatible ", "SONDER_HOME": " C:/state "},
    )

    assert config == RuntimeConfig(
        profile="test-profile",
        model_backend="openai-compatible",
        sonder_home=" C:/state ",
    )


def test_build_config_from_env_uses_defaults_without_environment_keys():
    assert build_config_from_env("local", {}) == RuntimeConfig(profile="local")
