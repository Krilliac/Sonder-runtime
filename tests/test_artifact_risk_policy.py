"""Ownership and behavior tests for the execution-risk domain policy."""

import sonder_runtime.adapters.artifact_risk as artifact_risk
from sonder_runtime.domain.artifact_risk_policy import policy_denies


def test_policy_denies_is_owned_by_domain_boundary():
    assert artifact_risk.policy_denies is policy_denies


def test_deny_high_only_blocks_high_risk():
    assert policy_denies("deny-high", "high") is True
    assert policy_denies("deny-high", "medium") is False
    assert policy_denies("deny-high", "unknown") is False


def test_deny_medium_includes_high_and_medium():
    assert policy_denies("deny-medium", "high") is True
    assert policy_denies("deny-medium", "medium") is True
    assert policy_denies("deny-medium", "low") is False


def test_deny_unknown_includes_unknown_and_known_risks():
    assert policy_denies("deny-unknown", "unknown") is True
    assert policy_denies("deny-unknown", "medium") is True
    assert policy_denies("deny-unknown", "high") is True
    assert policy_denies("deny-unknown", "low") is False


def test_non_denial_policies_are_permissive():
    for policy in ("off", "report", "", None):
        assert policy_denies(policy, "high") is False
