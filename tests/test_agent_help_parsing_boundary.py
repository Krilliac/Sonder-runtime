"""Boundary tests for sonder_runtime.domain.agent_help_parsing."""

import server
from sonder_runtime.domain.agent_help_parsing import help_advertised_tools


def test_root_helper_is_identity_preserving_alias():
    assert server._agent_help_advertised_tools is help_advertised_tools


def test_extracts_tool_names():
    text = "- file_read: {path}\n- file_write: {path, content}\n"
    assert help_advertised_tools(text) == ("file_read", "file_write")


def test_ignores_non_tool_lines():
    text = "Some header\n- file_read: {path}\nSome text\n  indented\n- status: {}\n"
    assert help_advertised_tools(text) == ("file_read", "status")


def test_ignores_lines_without_colon():
    text = "- no_colon_here\n- valid_tool: {args}\n"
    assert help_advertised_tools(text) == ("valid_tool",)


def test_ignores_non_identifier_names():
    text = "- 123bad: {}\n- good_name: {}\n- has-dash: {}\n"
    assert help_advertised_tools(text) == ("good_name",)


def test_empty_input():
    assert help_advertised_tools("") == ()
    assert help_advertised_tools(None) == ()


def test_returns_tuple():
    result = help_advertised_tools("- tool: {}")
    assert isinstance(result, tuple)
