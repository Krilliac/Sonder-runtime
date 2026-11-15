from __future__ import annotations

import json
import time

from sonder_runtime.domain.native_tool_policy import (
    MAX_ARGUMENT_CHARS,
    MAX_ARGUMENT_DEPTH,
    native_tool_call_decision,
)


def test_native_tool_policy_serializes_one_valid_call():
    result = native_tool_call_decision({
        "tool_calls": [{"function": {"name": "read_file", "arguments": "{\"path\":\"x\"}"}}]
    })
    assert json.loads(result) == {
        "args": {"path": "x"}, "reason": "model native tool call", "tool": "read_file",
    }


def test_native_tool_policy_rejects_ambiguous_or_oversized_calls():
    assert native_tool_call_decision({"tool_calls": []}) is None
    assert native_tool_call_decision({
        "tool_calls": [{"function": {"name": "bad name", "arguments": {}}}]
    }) is None
    assert native_tool_call_decision({
        "tool_calls": [{"function": {"name": "x", "arguments": "x" * 65537}}]
    }) is None


def _call(arguments):
    return native_tool_call_decision(
        {"tool_calls": [{"function": {"name": "x", "arguments": arguments}}]}
    )


def _nested(depth):
    # The outer object is depth 1.
    return '{"a":' * (depth - 1) + "{}" + "}" * (depth - 1)


def test_native_tool_policy_nesting_limit_is_exact():
    assert _call(_nested(MAX_ARGUMENT_DEPTH)) is not None
    assert _call(_nested(MAX_ARGUMENT_DEPTH + 1)) is None


def test_native_tool_policy_ignores_brackets_inside_strings():
    noisy = "[" * 500 + '\\"' + "{" * 500
    arguments = json.dumps({"k": noisy})
    assert '\\"' in arguments
    assert _call(arguments) is not None
    # An escaped quote must not end the string early and expose its brackets.
    assert _call('{"k":"\\"' + "[" * 500 + '"}') is not None


def test_native_tool_policy_refuses_huge_nesting_quickly():
    # Stay under MAX_ARGUMENT_CHARS so the depth guard, not the size guard,
    # is what refuses these.
    assert _call("[" * 60000) is None
    assert _call('{"a":' * 10000 + "{}" + "}" * 10000) is None
    # 100k-deep already-parsed value (no text, no recursion in the checker).
    value = {}
    for _ in range(100000):
        value = {"a": value}
    assert _call(value) is None


def test_native_tool_policy_adversarial_quote_shapes_stay_linear():
    # Regression: a backtracking string-literal regex took ~8-12 s on the
    # first two shapes at the size cap. The linear scan takes a few ms; the
    # 1 s bound is ~100x headroom for loaded CI and still far below the
    # quadratic cost.
    n = MAX_ARGUMENT_CHARS
    shapes = {
        "escaped_quote_pairs": ('\\"' * n)[:n],
        "quote_backslash_pairs": ('"\\' * n)[:n],
        "only_quotes": '"' * n,
        "only_backslashes": "\\" * n,
        "alternating_bracket_escaped_quote": ('[\\"' * n)[:n],
    }
    for name, text in shapes.items():
        assert len(text) == n, name
        started = time.monotonic()
        _call(text)
        assert time.monotonic() - started < 1.0, name


def test_native_tool_policy_refuses_deep_dict_arguments():
    value = {}
    for _ in range(MAX_ARGUMENT_DEPTH + 5):
        value = {"a": value}
    assert _call(value) is None
