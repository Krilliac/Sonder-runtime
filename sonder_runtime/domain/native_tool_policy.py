"""Pure validation and serialization policy for model-native tool calls."""
from __future__ import annotations

import json
import re


MAX_ARGUMENT_CHARS = 65536
TOOL_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}\Z")


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
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError, RecursionError):
            return None
    if not isinstance(arguments, dict):
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


__all__ = ["MAX_ARGUMENT_CHARS", "TOOL_NAME_RE", "native_tool_call_decision"]
