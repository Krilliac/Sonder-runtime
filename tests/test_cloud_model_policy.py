"""Ownership and regression tests for the cloud-model policy boundary."""
from __future__ import annotations

import server
from sonder_runtime.domain import cloud_model_policy


def test_cloud_model_policy_is_owned_by_domain_and_reexported_by_server():
    assert server._live_cloud_model is cloud_model_policy.live_cloud_model


def test_cloud_model_policy_rewrites_retired_and_empty_values():
    default = "live-code:cloud"
    assert cloud_model_policy.live_cloud_model("qwen3-coder:480b-cloud", default) == default
    assert cloud_model_policy.live_cloud_model("QWEN3-CODER:480B-CLOUD", default) == default
    assert cloud_model_policy.live_cloud_model(None, default) == default
    assert cloud_model_policy.live_cloud_model("   ", default) == default


def test_cloud_model_policy_preserves_live_override_spelling():
    assert cloud_model_policy.live_cloud_model(
        "Some-Other:Cloud", "live-code:cloud"
    ) == "Some-Other:Cloud"


def test_cloud_model_policy_accepts_provider_specific_retired_catalog():
    assert cloud_model_policy.live_cloud_model(
        "provider:retired", "provider:default", {"provider:retired"}
    ) == "provider:default"
