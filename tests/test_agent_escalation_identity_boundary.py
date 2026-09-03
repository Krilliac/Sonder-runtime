"""Boundary tests for the agent_escalation_identity packaged module."""

import server
from sonder_runtime.domain.agent_escalation_identity import escalation_key


def test_root_helper_is_identity_preserving_alias():
    assert server._agent_escalation_key is escalation_key


def test_deterministic():
    a = escalation_key("fast", "hello world")
    b = escalation_key("fast", "hello world")
    assert a == b


def test_format_tier_colon_digest():
    result = escalation_key("Fast", "prompt")
    assert result.startswith("fast:")
    assert len(result.split(":")[1]) == 16


def test_none_tier_and_prompt():
    result = escalation_key(None, None)
    assert result.startswith(":")
    assert len(result.split(":")[1]) == 16


def test_different_prompts_different_keys():
    a = escalation_key("t", "prompt one")
    b = escalation_key("t", "prompt two")
    assert a != b
