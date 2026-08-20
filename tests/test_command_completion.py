"""Tests for the pure command-completion adapter."""

from sonder_runtime.adapters.command_completion import completion_limit


def test_completion_limit_defaults_for_missing_and_invalid_values():
    assert completion_limit(None) == 12
    assert completion_limit("") == 12
    assert completion_limit("not-a-number") == 12


def test_completion_limit_clamps_supported_range():
    assert completion_limit("1") == 1
    assert completion_limit("25") == 25
    assert completion_limit("0") == 1
    assert completion_limit("999") == 50
