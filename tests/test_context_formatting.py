import server

from sonder_runtime.domain.context_formatting import (
    rough_token_count,
    rough_token_count_from_chars,
)


def test_rough_token_count_is_the_server_compatibility_alias():
    assert server._rough_token_count is rough_token_count


def test_rough_token_count_handles_empty_and_short_values():
    assert rough_token_count(None) == 0
    assert rough_token_count("") == 0
    assert rough_token_count("a") == 1
    assert rough_token_count("abcd") == 1
    assert rough_token_count("abcde") == 2


def test_rough_token_count_stringifies_non_text_values():
    assert rough_token_count(12345) == 2


def test_character_count_estimator_is_the_server_compatibility_alias():
    assert server._rough_token_count_from_chars is rough_token_count_from_chars


def test_character_count_estimator_handles_empty_and_fractional_quarters():
    assert rough_token_count_from_chars(None) == 0
    assert rough_token_count_from_chars(0) == 0
    assert rough_token_count_from_chars(1) == 1
    assert rough_token_count_from_chars(4) == 1
    assert rough_token_count_from_chars(5) == 2


def test_character_count_estimator_clamps_negative_values():
    assert rough_token_count_from_chars(-10) == 0
