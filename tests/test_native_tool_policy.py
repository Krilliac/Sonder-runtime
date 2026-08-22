from __future__ import annotations

import json

from sonder_runtime.domain.native_tool_policy import native_tool_call_decision


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
