"""Check that a model reply is JSON matching a JSON Schema.

Pure and stdlib-only.  It enforces the common keyword subset below and fails
closed on everything else: a schema that uses a keyword (or a malformed value
for one) this check cannot evaluate is rejected as ``unsupported_schema``
before the reply is looked at, never passed as satisfied.  A decoder-side
constraint is a claim about the backend, not evidence.  Nothing is repaired,
coerced or defaulted.

Enforced: type, enum, const, required, properties, additionalProperties
(bool or schema), items (single schema), minItems, maxItems, minLength,
maxLength, minimum, maximum, exclusiveMinimum, exclusiveMaximum, pattern,
anyOf.  Annotation keywords assert nothing and are not gaps.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re

_ANNOTATIONS = frozenset({
    "$schema", "$id", "$comment", "title", "description", "default",
    "examples", "deprecated", "readOnly", "writeOnly",
})
_ENFORCED = frozenset({
    "type", "enum", "const", "required", "properties", "additionalProperties",
    "items", "minItems", "maxItems", "minLength", "maxLength", "minimum",
    "maximum", "exclusiveMinimum", "exclusiveMaximum", "pattern", "anyOf",
})
_TYPE_NAMES = frozenset({
    "object", "array", "string", "integer", "number", "boolean", "null",
})
UNSUPPORTED_SCHEMA = "unsupported_schema"
_BOUND_KEYWORDS = (
    "minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum",
    "exclusiveMinimum", "exclusiveMaximum",
)


@dataclass(frozen=True)
class ReplyCheck:
    """Outcome of checking one reply; ``kind`` is empty when ``ok``."""

    value: object
    kind: str
    errors: tuple[str, ...]
    unchecked: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.kind


def _reject_constant(name: str) -> object:
    raise ValueError("%s is not a JSON value" % name)


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```") and len(stripped) >= 6:
        body = stripped[3:-3]
        newline = body.find("\n")
        if newline != -1 and body[:newline].strip().isalpha():
            body = body[newline + 1:]
        return body.strip()
    return stripped


def _matches_type(value: object, name: str) -> bool:
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    if isinstance(value, bool):
        return False
    if name == "integer":
        return isinstance(value, int) or (isinstance(value, float) and value.is_integer())
    return isinstance(value, (int, float))


def _same(a: object, b: object) -> bool:
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    return a == b


def _walk(value: object, schema: object, path: str, errors: list[str],
          unchecked: set[str]) -> None:
    if schema is True or schema == {}:
        return
    if schema is False:
        errors.append("%s: no value is allowed here" % path)
        return
    if not isinstance(schema, dict):
        unchecked.add("<non-object subschema at %s>" % path)
        return
    for keyword in schema:
        if keyword not in _ENFORCED and keyword not in _ANNOTATIONS:
            unchecked.add(keyword)
    declared = schema.get("type")
    if declared is not None:
        names = [declared] if isinstance(declared, str) else declared
        if not isinstance(names, list) or not names or not all(
            isinstance(n, str) and n in _TYPE_NAMES for n in names
        ):
            unchecked.add("type")
        elif not any(_matches_type(value, n) for n in names):
            errors.append("%s: expected %s, got %s" % (
                path, " or ".join(names), type(value).__name__))
            return
    if "enum" in schema:
        options = schema["enum"]
        if isinstance(options, list):
            if not any(_same(value, option) for option in options):
                errors.append("%s: value is not one of the allowed values" % path)
        else:
            unchecked.add("enum")
    if "const" in schema and not _same(value, schema["const"]):
        errors.append("%s: value does not equal the required constant" % path)
    if "anyOf" in schema:
        branches = schema["anyOf"]
        if isinstance(branches, list) and branches:
            for branch in branches:
                branch_errors: list[str] = []
                _walk(value, branch, path, branch_errors, unchecked)
                if not branch_errors:
                    break
            else:
                errors.append("%s: value matches none of the anyOf alternatives" % path)
        else:
            unchecked.add("anyOf")
    if isinstance(value, dict):
        _walk_object(value, schema, path, errors, unchecked)
    elif isinstance(value, list):
        _walk_array(value, schema, path, errors, unchecked)
    elif isinstance(value, str):
        _walk_string(value, schema, path, errors, unchecked)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        _walk_number(value, schema, path, errors, unchecked)


def _walk_object(value: dict, schema: dict, path: str, errors: list[str],
                 unchecked: set[str]) -> None:
    required = schema.get("required", [])
    if isinstance(required, list):
        for name in required:
            if isinstance(name, str) and name not in value:
                errors.append("%s: missing required property %r" % (path, name))
    else:
        unchecked.add("required")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        unchecked.add("properties")
        properties = {}
    for name, sub in properties.items():
        if name in value:
            _walk(value[name], sub, "%s.%s" % (path, name), errors, unchecked)
    extra = schema.get("additionalProperties", True)
    for name in value:
        if name in properties:
            continue
        if extra is False:
            errors.append("%s: unexpected property %r" % (path, name))
        elif isinstance(extra, dict):
            _walk(value[name], extra, "%s.%s" % (path, name), errors, unchecked)


def _bound(schema: dict, keyword: str, unchecked: set[str]) -> int | float | None:
    if keyword not in schema:
        return None
    bound = schema[keyword]
    if isinstance(bound, bool) or not isinstance(bound, (int, float)):
        unchecked.add(keyword)
        return None
    return bound


def _walk_array(value: list, schema: dict, path: str, errors: list[str],
                unchecked: set[str]) -> None:
    low = _bound(schema, "minItems", unchecked)
    high = _bound(schema, "maxItems", unchecked)
    if low is not None and len(value) < low:
        errors.append("%s: has %d items, fewer than %s" % (path, len(value), low))
    if high is not None and len(value) > high:
        errors.append("%s: has %d items, more than %s" % (path, len(value), high))
    if "items" in schema:
        sub = schema["items"]
        if isinstance(sub, (dict, bool)):
            for index, item in enumerate(value):
                _walk(item, sub, "%s[%d]" % (path, index), errors, unchecked)
        else:
            unchecked.add("items")


def _walk_string(value: str, schema: dict, path: str, errors: list[str],
                 unchecked: set[str]) -> None:
    low = _bound(schema, "minLength", unchecked)
    high = _bound(schema, "maxLength", unchecked)
    if low is not None and len(value) < low:
        errors.append("%s: string is shorter than %s" % (path, low))
    if high is not None and len(value) > high:
        errors.append("%s: string is longer than %s" % (path, high))
    pattern = schema.get("pattern")
    if pattern is None:
        return
    if not isinstance(pattern, str):
        unchecked.add("pattern")
        return
    try:
        matched = re.search(pattern, value) is not None
    except re.error:
        unchecked.add("pattern")
        return
    if not matched:
        errors.append("%s: string does not match the pattern" % path)


def _walk_number(value: int | float, schema: dict, path: str, errors: list[str],
                 unchecked: set[str]) -> None:
    checks = (
        ("minimum", lambda v, b: v < b, "is below the minimum"),
        ("maximum", lambda v, b: v > b, "is above the maximum"),
        ("exclusiveMinimum", lambda v, b: v <= b, "is not above the exclusive minimum"),
        ("exclusiveMaximum", lambda v, b: v >= b, "is not below the exclusive maximum"),
    )
    for keyword, broken, message in checks:
        bound = _bound(schema, keyword, unchecked)
        if bound is not None and broken(value, bound):
            errors.append("%s: %s %s %s" % (path, value, message, bound))


def _scan(schema: object, path: str, found: set[str]) -> None:
    if schema is True or schema is False:
        return
    if not isinstance(schema, dict):
        found.add("<non-object subschema at %s>" % path)
        return
    for keyword in schema:
        if keyword not in _ENFORCED and keyword not in _ANNOTATIONS:
            found.add(keyword)
    declared = schema.get("type")
    if declared is not None:
        names = [declared] if isinstance(declared, str) else declared
        if not isinstance(names, list) or not names or not all(
            isinstance(n, str) and n in _TYPE_NAMES for n in names
        ):
            found.add("type")
    if "enum" in schema and not isinstance(schema["enum"], list):
        found.add("enum")
    if "required" in schema and not (
        isinstance(schema["required"], list)
        and all(isinstance(name, str) for name in schema["required"])
    ):
        found.add("required")
    for keyword in _BOUND_KEYWORDS:
        if keyword in schema:
            bound = schema[keyword]
            if isinstance(bound, bool) or not isinstance(bound, (int, float)):
                found.add(keyword)
    if "pattern" in schema:
        pattern = schema["pattern"]
        try:
            if not isinstance(pattern, str):
                raise re.error("not a string")
            re.compile(pattern)
        except re.error:
            found.add("pattern")
    if "properties" in schema:
        properties = schema["properties"]
        if isinstance(properties, dict):
            for name, sub in properties.items():
                _scan(sub, "%s.%s" % (path, name), found)
        else:
            found.add("properties")
    if "additionalProperties" in schema:
        extra = schema["additionalProperties"]
        if isinstance(extra, (bool, dict)):
            _scan(extra, path + ".*", found)
        else:
            found.add("additionalProperties")
    if "items" in schema:
        sub = schema["items"]
        if isinstance(sub, (dict, bool)):
            _scan(sub, path + "[]", found)
        else:
            found.add("items")
    if "anyOf" in schema:
        branches = schema["anyOf"]
        if isinstance(branches, list) and branches:
            for index, branch in enumerate(branches):
                _scan(branch, "%s|%d" % (path, index), found)
        else:
            found.add("anyOf")


def unsupported_keywords(schema: object) -> tuple[str, ...]:
    """Every keyword (or malformed value) in ``schema`` this check cannot
    evaluate, sorted; empty when the whole schema is checkable."""
    found: set[str] = set()
    _scan(schema, "$", found)
    return tuple(sorted(found))


def check_reply(text: str, schema: dict) -> ReplyCheck:
    """Parse ``text`` as JSON and check it against ``schema``.

    A single surrounding Markdown code fence is tolerated; anything else around
    the JSON is an ``invalid_json`` failure.  A schema using anything outside
    the enforced subset is an ``unsupported_schema`` failure.
    """
    gaps = unsupported_keywords(schema)
    if gaps:
        return ReplyCheck(None, UNSUPPORTED_SCHEMA, (
            "schema uses keywords this check cannot evaluate: %s" % ", ".join(gaps),
        ), gaps)
    try:
        value = json.loads(_strip_fence(text), parse_constant=_reject_constant)
    except RecursionError:
        return ReplyCheck(None, "invalid_json", ("reply is nested too deeply to be valid JSON",), ())
    except (TypeError, ValueError) as exc:
        return ReplyCheck(None, "invalid_json", ("reply is not valid JSON: %s" % exc,), ())
    errors: list[str] = []
    unchecked: set[str] = set()
    _walk(value, schema, "$", errors, unchecked)
    gaps = tuple(sorted(unchecked))
    if errors:
        return ReplyCheck(None, "schema_violation", tuple(errors), gaps)
    return ReplyCheck(value, "", (), gaps)
