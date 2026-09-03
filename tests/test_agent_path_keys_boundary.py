"""Boundary tests for the agent_path_keys packaged module."""

import server
from sonder_runtime.domain.agent_path_keys import created_path_key


def test_root_helper_is_identity_preserving_alias():
    assert server._agent_created_path_key is created_path_key


def test_normalizes_path():
    assert created_path_key("src/./foo/../bar.h") == created_path_key("src/bar.h")


def test_none_returns_dot():
    result = created_path_key(None)
    assert result == created_path_key("")


def test_backslash_and_forward_slash_equivalent():
    assert created_path_key("src/a.h") == created_path_key("src/a.h")
