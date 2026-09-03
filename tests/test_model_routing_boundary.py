"""Boundary tests for sonder_runtime.domain.model_routing."""

import server
from sonder_runtime.domain.model_routing import is_cloud_model_name, is_cloud_tier


def test_root_helpers_are_identity_preserving_aliases():
    assert server._is_cloud_model_name is is_cloud_model_name


def test_is_cloud_tier_by_set_membership():
    assert is_cloud_tier("cloud", cloud_tiers={"cloud"}, tier_map={})
    assert not is_cloud_tier("fast", cloud_tiers={"cloud"}, tier_map={})


def test_is_cloud_tier_falls_back_to_model_name():
    assert is_cloud_tier("code", cloud_tiers=set(), tier_map={"code": "qwen-cloud"})
    assert not is_cloud_tier("code", cloud_tiers=set(), tier_map={"code": "qwen:latest"})


def test_is_cloud_tier_explicit_model_overrides_map():
    assert is_cloud_tier("fast", model="deepseek-cloud", cloud_tiers=set(), tier_map={})
    assert not is_cloud_tier("fast", model="deepseek:latest", cloud_tiers=set(), tier_map={})


def test_is_cloud_tier_none_model_and_missing_tier():
    assert not is_cloud_tier("unknown", cloud_tiers=set(), tier_map={})


def test_server_delegate_binds_module_globals():
    result = server._is_cloud_tier("__nonexistent_test_tier__")
    assert isinstance(result, bool)
