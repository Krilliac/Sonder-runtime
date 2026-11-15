"""Pure validation and serialization policy for model-native tool calls."""
from __future__ import annotations

import json
import re


MAX_ARGUMENT_CHARS = 65536
MAX_ARGUMENT_DEPTH = 64
TOOL_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}\Z")


def _json_text_depth_within(text: str, limit: int) -> bool:
    """Return False once bracket nesting outside string literals exceeds limit.

    A single linear pass (string/escape state machine, no regex, no
    backtracking), independent of the interpreter recursion limit, so the
    verdict and the cost are the same on every Python version. An unterminated
    string swallows the rest of the text; json.loads rejects such input anyway.
    """
    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{" or char == "[":
            depth += 1
            if depth > limit:
                return False
        elif char == "}" or char == "]":
            depth -= 1
    return True


def _value_depth_within(value, limit: int) -> bool:
    """Iteratively check container nesting of a parsed value against limit."""
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            children = item.values()
        elif isinstance(item, (list, tuple)):
            children = item
        else:
            continue
        if depth > limit:
            return False
        stack.extend((child, depth + 1) for child in children)
    return True


def native_tool_call_decision(message) -> str | None:
    """Translate exactly one bounded provider tool call to host JSON."""
    if not isinstance(message, dict):
        return None
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, (list, tuple)) or len(tool_calls) != 1:
        return None
    tool_call = tool_calls[0]
    function = tool_call.get("function") if isinstance(tool_call, dict) else None
    if not isinstance(function, dict):
        return None
    name = function.get("name")
    if not isinstance(name, str):
        return None
    name = name.strip()
    if not TOOL_NAME_RE.fullmatch(name):
        return None
    if "arguments" not in function:
        return None
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        if len(arguments) > MAX_ARGUMENT_CHARS:
            return None
        if not _json_text_depth_within(arguments, MAX_ARGUMENT_DEPTH):
            return None
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError, RecursionError):
            return None
    if not isinstance(arguments, dict):
        return None
    if not _value_depth_within(arguments, MAX_ARGUMENT_DEPTH):
        return None
    try:
        encoded_arguments = json.dumps(
            arguments, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None
    if len(encoded_arguments) > MAX_ARGUMENT_CHARS:
        return None
    try:
        return json.dumps(
            {"tool": name, "args": arguments, "reason": "model native tool call"},
            ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None


__all__ = ["MAX_ARGUMENT_CHARS", "MAX_ARGUMENT_DEPTH", "TOOL_NAME_RE", "native_tool_call_decision"]
