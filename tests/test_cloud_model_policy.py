"""Ownership and regression tests for the cloud-model policy boundary."""
from __future__ import annotations
from sonder_runtime.adapters import model_lists as _test_model_lists

import server
from sonder_runtime.domain import cloud_model_policy


def test_cloud_model_policy_is_owned_by_domain_and_reexported_by_server():
    from sonder_runtime.adapters.model_list_consumers import live_cloud_model
    assert server._live_cloud_model is live_cloud_model


def test_cloud_model_policy_rewrites_retired_and_empty_values():
    default = "live-code:cloud"
    assert (
        cloud_model_policy.live_cloud_model(
            "qwen3-coder:480b-cloud",
            default,
            retired_models=_test_model_lists.load().values["retired_cloud_models"],
        )
        == default
    )
    assert (
        cloud_model_policy.live_cloud_model(
            "QWEN3-CODER:480B-CLOUD",
            default,
            retired_models=_test_model_lists.load().values["retired_cloud_models"],
        )
        == default
    )
    assert (
        cloud_model_policy.live_cloud_model(
            None,
            default,
            retired_models=_test_model_lists.load().values["retired_cloud_models"],
        )
        == default
    )
    assert (
        cloud_model_policy.live_cloud_model(
            "   ",
            default,
            retired_models=_test_model_lists.load().values["retired_cloud_models"],
        )
        == default
    )


def test_cloud_model_policy_preserves_live_override_spelling():
    assert (
        cloud_model_policy.live_cloud_model(
            "Some-Other:Cloud",
            "live-code:cloud",
            retired_models=_test_model_lists.load().values["retired_cloud_models"],
        )
        == "Some-Other:Cloud"
    )


def test_cloud_model_policy_accepts_provider_specific_retired_catalog():
    assert cloud_model_policy.live_cloud_model(
        "provider:retired", "provider:default", {"provider:retired"}
    ) == "provider:default"
