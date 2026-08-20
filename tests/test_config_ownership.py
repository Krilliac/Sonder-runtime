from __future__ import annotations

import ast
from pathlib import Path

import sonder_config
from sonder_runtime.platform import config as packaged_config


def test_root_configuration_surface_reexports_canonical_objects():
    for name in (
        "SonderConfig",
        "ServerConfig",
        "StateConfig",
        "OllamaConfig",
        "FeaturesConfig",
        "CapacityConfig",
        "ObservabilityConfig",
        "BackupConfig",
        "Secrets",
        "ConfigError",
        "load_config",
        "parse_env_file",
    ):
        assert getattr(sonder_config, name) is getattr(packaged_config, name)


def test_packaged_configuration_owns_implementation_and_uses_packaged_paths():
    root = Path(__file__).parents[1]
    source = (root / "sonder_runtime/platform/config.py").read_text(encoding="utf-8")
    imports = ast.parse(source)
    imported_modules = {
        alias.name.split(".")[0]
        for node in ast.walk(imports)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_modules.update(
        node.module.split(".")[0]
        for node in ast.walk(imports)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert "sonder_config" not in imported_modules
    assert "sonder_paths" not in imported_modules
    assert "sonder_runtime" in imported_modules


def test_configuration_error_identity_is_preserved():
    assert sonder_config.ConfigError is packaged_config.ConfigError
    assert sonder_config.sonder_paths is packaged_config.sonder_paths
