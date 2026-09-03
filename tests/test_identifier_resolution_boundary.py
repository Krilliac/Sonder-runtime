"""Boundary tests for sonder_runtime.domain.identifier_resolution."""

import server
from sonder_runtime.domain.identifier_resolution import resolve_identifier


def test_server_session_delegate_uses_domain_function():
    result = server._resolve_session("")
    assert result == server.DEFAULT_SESSION


def test_server_project_delegate_uses_domain_function():
    result = server._resolve_project("")
    assert result == server.DEFAULT_PROJECT


def test_empty_returns_default():
    assert resolve_identifier("", "my_default") == "my_default"


def test_none_returns_default():
    assert resolve_identifier(None, "my_default") == "my_default"


def test_whitespace_only_returns_default():
    assert resolve_identifier("   ", "my_default") == "my_default"


def test_none_string_returns_none():
    assert resolve_identifier("none", "my_default") is None
    assert resolve_identifier("None", "my_default") is None
    assert resolve_identifier("NONE", "my_default") is None


def test_value_passes_through():
    assert resolve_identifier("custom_id", "my_default") == "custom_id"


def test_value_stripped():
    assert resolve_identifier("  custom_id  ", "my_default") == "custom_id"
