"""Boundary tests for the runtime_update_parsing packaged module."""

import pytest

import server
from sonder_runtime.domain.runtime_update_parsing import parse_update_object


def test_root_helper_is_identity_preserving_alias():
    assert server._runtime_update_object is parse_update_object


def test_none_returns_empty_dict():
    assert parse_update_object(None, "test") == {}


def test_empty_string_returns_empty_dict():
    assert parse_update_object("", "test") == {}


def test_dict_passthrough():
    d = {"key": "value"}
    assert parse_update_object(d, "test") is d


def test_json_string_decoded():
    assert parse_update_object('{"a": 1}', "test") == {"a": 1}


def test_invalid_json_raises():
    with pytest.raises(ValueError, match="test must be a JSON object"):
        parse_update_object("not-json", "test")


def test_non_object_json_raises():
    with pytest.raises(ValueError, match="test must be a JSON object"):
        parse_update_object("[1, 2]", "test")
