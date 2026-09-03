"""Boundary tests for the agent_observation_quality packaged module."""

import server
from sonder_runtime.domain.agent_observation_quality import observation_ok


def test_root_helper_is_identity_preserving_alias():
    assert server._agent_observation_ok is observation_ok


def test_ok_observation():
    assert observation_ok("All tests passed") is True


def test_none_is_ok():
    assert observation_ok(None) is True


def test_empty_is_ok():
    assert observation_ok("") is True


def test_error_prefix():
    assert observation_ok("ERROR: something broke") is False


def test_ok_false_yaml():
    assert observation_ok("result:\n  ok: false") is False


def test_first_line_fail_suffix():
    assert observation_ok("compile: fail\ndetails here") is False


def test_validation_failed_prefix():
    assert observation_ok("validation_failed: schema mismatch") is False


def test_fail_tag():
    assert observation_ok("test output [fail] on line 5") is False
