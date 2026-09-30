"""Playbook policy remains explicit, typed and bounded before startup."""
from dataclasses import replace

import pytest

from sonder_runtime.platform.config import ConfigError, SonderConfig, load_config
from sonder_runtime.platform.playbooks_config import PlaybooksConfig, playbooks_errors


def test_defaults_require_owner_approval():
    config = SonderConfig()
    assert config.playbooks.approval == "required"
    assert config.as_redacted_dict()["playbooks"]["max_index_bytes"] == 4096
    assert playbooks_errors(config) == []


@pytest.mark.parametrize("approval", ["required", "owner_corrections_auto", "auto"])
def test_toml_modes_and_category_extension(tmp_path, approval):
    path = tmp_path / "playbooks.toml"
    path.write_text(
        f'[playbooks]\napproval = "{approval}"\n'
        'categories = ["pitfall", "procedure", "deployment"]\n'
        'max_index_bytes = 2048\n', encoding="utf-8",
    )
    config = load_config(path, env={})
    assert config.playbooks.approval == approval
    assert config.playbooks.categories[-1] == "deployment"
    assert config.playbooks.max_index_bytes == 2048


@pytest.mark.parametrize("field,value", [
    ("approval", "yes"), ("max_index_bytes", 4097), ("max_topics", 0),
    ("max_context_bytes", True), ("max_entry_bytes", 1000000),
    ("categories", ("pitfall", "pitfall")), ("categories", ("../unsafe",)),
    ("environment_stale_days", -1), ("measurement_stale_days", 0),
])
def test_invalid_policy_rejected(field, value):
    config = replace(SonderConfig(), playbooks=replace(PlaybooksConfig(), **{field: value}))
    assert playbooks_errors(config)


def test_invalid_toml_fails_before_runtime(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text('[playbooks]\napproval = "trust_model"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="playbooks"):
        load_config(path, env={})
