"""Packaged ownership tests for speculative execution safety policy."""

import sonder_speculation
from sonder_runtime.domain import speculation_policy


def test_root_allowlist_preserves_packaged_identity():
    assert sonder_speculation.SPECULATABLE_TOOLS is speculation_policy.SPECULATABLE_TOOLS


def test_policy_is_closed_and_excludes_side_effecting_tools():
    assert speculation_policy.is_speculatable("file_read")
    assert speculation_policy.is_speculatable("permission_policy")
    for tool_name in ("file_write", "run_code", "web_fetch", "model_call"):
        assert not speculation_policy.is_speculatable(tool_name)


def test_predictor_uses_packaged_policy():
    predictor = sonder_speculation.BranchPredictor()
    assert predictor.speculatable("file_read") is True
    assert predictor.speculatable("file_write") is False
