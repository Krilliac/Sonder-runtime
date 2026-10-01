"""Normalize model-generated agent tool arguments.

The model protocol deliberately stays permissive at its boundary.  This
module turns the common alternate spellings produced by small models into the
canonical argument object used by dispatch, without doing I/O or consulting
the host.  It is intentionally deterministic and idempotent.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any


_ALIASES = {
    "file": "path",
    "filename": "path",
    "filepath": "path",
    "file_path": "path",
    "pattern": "query",
    "needle": "query",
    "old_string": "old",
    "new_string": "new",
    "body": "content",
}
_FENCED_KEYS = frozenset(("content", "code", "script"))
_QUERY_TOOLS = frozenset(("file_find", "text_search", "repository_symbol_index"))
_CONTENT_TOOLS = frozenset(("file_edit", "file_write", "text_patch", "apply_patch"))
_BOOLS = {"true": True, "false": False}
_AUTHORITY_KEYS = frozenset(("token", "approval", "extra_roots"))
_OUTER_FENCE = re.compile(r"^```[^\r\n]*\r?\n(?P<body>.*?)(?:\r?\n)?```$", re.DOTALL)


def _schema_properties(schema: Any) -> Mapping[str, Any]:
    if not isinstance(schema, Mapping):
        return {}
    properties = schema.get("properties", schema)
    return properties if isinstance(properties, Mapping) else {}


def _decode_mapping(value: Any) -> Any:
    """Decode JSON strings, while leaving ordinary scalar strings alone."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return value


def _strip_one_fence(value: str) -> str:
    match = _OUTER_FENCE.match(value.strip())
    if not match:
        return value
    body = match.group("body")
    # A nested fenced payload is usually intentional source content.  Keeping
    # the outer spelling makes repeated normalization idempotent and prevents
    # a second pass from consuming another meaningful fence.
    if body.lstrip().startswith("```"):
        return value
    return body


def _coerce_bool(value: Any) -> Any:
    if isinstance(value, str):
        return _BOOLS.get(value.strip().lower(), value)
    return value


def _clamp(value: Any, spec: Mapping[str, Any], name: str, notes: list[str]) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    result = value
    minimum, maximum = spec.get("minimum"), spec.get("maximum")
    if minimum is not None and result < minimum:
        result = minimum
    if maximum is not None and result > maximum:
        result = maximum
    if result != value:
        notes.append("clamped %s to %s" % (name, result))
    return result


def _command_parts(value: Any) -> tuple[Any, Any]:
    if isinstance(value, (list, tuple)):
        parts = list(value)
    elif isinstance(value, str):
        # A tiny shell-free tokenizer: quotes group argv words, while
        # backslashes remain ordinary Windows path characters.  This is only
        # argument shaping; it never invokes a shell or evaluates input.
        parts = []
        buf = []
        quote = None
        word_started = False
        for char in value.strip():
            if quote is not None:
                if char == quote:
                    quote = None
                else:
                    buf.append(char)
            elif char in ("'", '"'):
                quote = char
                word_started = True
            elif char.isspace():
                if buf or word_started:
                    parts.append("".join(buf))
                    buf = []
                    word_started = False
            else:
                buf.append(char)
                word_started = True
        if quote is not None:
            return value, None
        if buf or word_started:
            parts.append("".join(buf))
    else:
        return value, None
    if not parts:
        return "", []
    return parts[0], parts[1:]


def normalize_tool_args(tool: str | None, args: Any, schema: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], list[str]]:
    """Return canonical arguments and host-readable normalization notes.

    ``schema`` may be a JSON-schema object or its ``properties`` mapping.  If
    present, unknown keys are dropped.  With no schema the normalizer preserves
    keys, which lets the dispatcher apply its existing tool-specific policy.
    """
    notes: list[str] = []
    value = _decode_mapping(args)
    if not isinstance(value, Mapping):
        notes.append("tool arguments must be a JSON object")
        return value, notes
    raw = dict(value)

    # Some providers emit a complete decision as the argument value.  Unwrap
    # both protocol spellings; ``name`` is a tool alias only at this envelope.
    is_envelope = "tool" in raw or ("name" in raw and "arguments" in raw) or set(raw) <= {"args", "arguments"}
    envelope = raw.get("arguments") if is_envelope else None
    if envelope is not None:
        decoded = _decode_mapping(envelope)
        if isinstance(decoded, Mapping):
            raw = dict(decoded)
            notes.append("unwrapped arguments envelope")
    elif is_envelope and isinstance(raw.get("args"), (Mapping, str)):
        decoded = _decode_mapping(raw["args"])
        if isinstance(decoded, Mapping):
            raw = dict(decoded)
            notes.append("unwrapped args envelope")

    properties = _schema_properties(schema)
    allowed = set(properties) if schema is not None else None
    result: dict[str, Any] = {}

    # Canonical keys win over aliases regardless of insertion order.
    for key, item in raw.items():
        target = key
        if key in _ALIASES and not (allowed is not None and key in allowed):
            target = _ALIASES[key]
        elif key == "text" and not (allowed is not None and key in allowed):
            if tool in _QUERY_TOOLS or "query" in raw or (allowed is not None and "query" in allowed):
                target = "query"
            elif tool in _CONTENT_TOOLS or "content" in raw or (allowed is not None and "content" in allowed):
                target = "content"
        if target != key and target in raw:
            continue
        # Host policy must see model-supplied authority-looking fields so it
        # can refuse them.  Dropping them here would turn an explicit hostile
        # proposal into an apparently safe call before policy evaluation.
        if allowed is not None and target not in allowed and target not in _AUTHORITY_KEYS:
            notes.append("dropped unknown argument %s" % key)
            continue
        result[target] = item

    command = next((raw[k] for k in ("cmd", "command") if k in raw), None)
    if command is not None and "program" not in result and (allowed is None or "program" in allowed):
        program, command_args = _command_parts(command)
        if allowed is None or "program" in allowed:
            result["program"] = program
        if command_args is not None and (allowed is None or "args" in allowed):
            result["args"] = command_args
        notes.append("normalized command to program and args")
        for key in ("cmd", "command"):
            if allowed is None or key not in allowed:
                result.pop(key, None)

    for name, item in list(result.items()):
        spec = properties.get(name, {})
        if isinstance(spec, Mapping):
            if spec.get("type") == "boolean":
                item = _coerce_bool(item)
            item = _clamp(item, spec, name, notes)
        if name in _FENCED_KEYS and isinstance(item, str):
            stripped = _strip_one_fence(item)
            if stripped != item:
                notes.append("stripped outer fence from %s" % name)
            item = stripped
        result[name] = item
    return result, notes


def normalize_decision_aliases(decision: Any) -> Any:
    """Canonicalize the optional ``name``/``arguments`` decision envelope.

    Name is meaningful only alongside ``arguments``; ordinary final decisions
    and legacy decisions are returned unchanged (as a shallow copy).
    """
    if not isinstance(decision, Mapping):
        return decision
    result = dict(decision)
    if "arguments" not in result:
        return result
    if "args" not in result:
        result["args"] = result.pop("arguments")
    else:
        result.pop("arguments", None)
    if "name" in result and not result.get("tool"):
        result["tool"] = result.pop("name")
    return result


__all__ = ["normalize_decision_aliases", "normalize_tool_args"]
