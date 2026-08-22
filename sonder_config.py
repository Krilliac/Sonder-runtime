"""Compatibility surface for the canonical packaged configuration contract.

The implementation lives in :mod:`sonder_runtime.platform.config`.  This
module remains importable for external configuration tooling and legacy
callers; every public object is re-exported from the canonical module so
class identity and exception matching remain exact.
"""
from sonder_runtime.platform.config import (
    ACCOUNT_BEARING_AUTH_MODES,
    BUILTIN_DEV_AUTH_SECRET,
    MIN_API_KEY_LENGTH,
    PROFILES,
    SECRET_ENV_KEYS,
    BackupConfig,
    CapacityConfig,
    ConfigError,
    FeaturesConfig,
    OllamaConfig,
    ObservabilityConfig,
    Secrets,
    ServerConfig,
    SonderConfig,
    StateConfig,
    _SECTION_TYPES,
    load_config,
    parse_env_file,
    sonder_paths,
)

__all__ = [
    "ACCOUNT_BEARING_AUTH_MODES",
    "BUILTIN_DEV_AUTH_SECRET",
    "MIN_API_KEY_LENGTH",
    "PROFILES",
    "SECRET_ENV_KEYS",
    "BackupConfig",
    "CapacityConfig",
    "ConfigError",
    "FeaturesConfig",
    "OllamaConfig",
    "ObservabilityConfig",
    "Secrets",
    "ServerConfig",
    "SonderConfig",
    "StateConfig",
    "load_config",
    "parse_env_file",
    "sonder_paths",
]
