from __future__ import annotations

import ast
from pathlib import Path

import pytest
import sonder_config
from sonder_runtime.platform import config as packaged_config
from sonder_runtime.platform import config_environment


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


def test_environment_file_parser_is_owned_by_packaged_policy_boundary(tmp_path):
    path = tmp_path / "secrets.env"
    path.write_text("# comment\nTOKEN = \"value\"\nEMPTY=\n", encoding="utf-8")

    assert config_environment.parse_env_file(path) == {
        "TOKEN": "value",
        "EMPTY": "",
    }
    assert sonder_config.parse_env_file(path) == config_environment.parse_env_file(path)


def test_environment_file_parser_preserves_root_config_error_contract(tmp_path):
    path = tmp_path / "secrets.env"
    path.write_text("not-an-assignment\n", encoding="utf-8")

    with pytest.raises(sonder_config.ConfigError) as excinfo:
        sonder_config.parse_env_file(path)

    assert excinfo.value.errors == (
        f"{path}:1: expected KEY=VALUE, got 'not-an-assignment'",
    )
