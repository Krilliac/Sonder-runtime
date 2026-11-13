"""``model_usage.response_metadata``: the bounded metadata a generator keeps per response.

The expected values were produced by the block this function replaced in
``server._make_generate`` on the same inputs (values and key order), so the
move is pinned as behaviour-preserving.
"""
from __future__ import annotations

import pytest

from sonder_runtime.domain.model_usage import response_metadata

CASES = [
    ({}, {"done_reason": ""}),
    ({"message": {"content": "x"}}, {"done_reason": ""}),
    (
        {"message": {"content": "x", "thinking": "abc"}, "done_reason": " Stop "},
        {"done_reason": "stop", "thinking_chars": 3},
    ),
    (
        {"message": "notadict", "done_reason": None, "eval_count": 3},
        {"done_reason": "", "eval_count": 3},
    ),
    (
        {"message": {"thinking": 5}, "total_duration": 1, "load_duration": 2,
         "prompt_eval_count": 3, "prompt_eval_cached_count": 4, "prompt_eval_duration": 5,
         "eval_count": 6, "eval_duration": 7, "load_state": "warm", "cold_start": False,
         "finish_reason": "length", "extra": "dropped", "model": "m"},
        {"done_reason": "", "total_duration": 1, "load_duration": 2, "prompt_eval_count": 3,
         "prompt_eval_cached_count": 4, "prompt_eval_duration": 5, "eval_count": 6,
         "eval_duration": 7, "load_state": "warm", "cold_start": False, "finish_reason": "length"},
    ),
    (
        {"message": {"thinking": "abcdef"}, "reasoning_segments": 2, "thinking_chars": 40},
        {"done_reason": "", "reasoning_segments": 2, "thinking_chars": 40},
    ),
    (
        {"message": {"thinking": "abcdef"}, "reasoning_segments": 2, "thinking_chars": -1},
        {"done_reason": "", "reasoning_segments": 2, "thinking_chars": 6},
    ),
    (
        {"message": {"thinking": "abcdef"}, "reasoning_segments": 2},
        {"done_reason": "", "reasoning_segments": 2, "thinking_chars": 6},
    ),
    (
        {"message": {"thinking": ""}, "reasoning_segments": 1, "thinking_chars": 0},
        {"done_reason": "", "reasoning_segments": 1},
    ),
    ({"done_reason": "LENGTH", "thinking_chars": 99}, {"done_reason": "length"}),
]


@pytest.mark.parametrize("out, expected", CASES)
def test_response_metadata_matches_the_generator_contract(out, expected):
    result = response_metadata(out)
    assert result == expected
    assert list(result) == list(expected)  # key order is part of the contract


def test_response_metadata_never_copies_content_or_reasoning_text():
    out = {"message": {"content": "secret answer", "thinking": "private"}, "model": "m",
           "response": "x", "context": [1, 2]}
    result = response_metadata(out)
    assert result == {"done_reason": "", "thinking_chars": 7}
    assert "secret answer" not in repr(result) and "private" not in repr(result)
