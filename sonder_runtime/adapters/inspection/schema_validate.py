"""Guarded, bounded, stdlib-only JSON Schema validator for JSON, JSONL, and TOML files.

Validates a guarded ``.json``, ``.jsonl``/``.ndjson``, or ``.toml`` file against either
a guarded ``.json`` schema file (``schema_path``) or an inline JSON schema string
(``schema_json``).

Supports Draft-7 / Draft 2020-12 core keywords:
``type``, ``const``, ``enum``, ``required``, ``properties``, ``additionalProperties``,
``minProperties``, ``maxProperties``, ``items``, ``minItems``, ``maxItems``,
``uniqueItems``, ``minimum``, ``maximum``, ``exclusiveMinimum``, ``exclusiveMaximum``,
``multipleOf``, ``minLength``, ``maxLength``, ``pattern``, ``allOf``, ``anyOf``,
``oneOf``, ``not``, and local JSON-pointer ``$ref`` (``#/$defs/...`` /
``#/definitions/...``) with cycle detection and depth ceilings.

Security invariants:
- Both data and schema files pass ``file_ops.require_read_access``, reparse-point
  refusal, sensitive-directory refusal, and byte ceilings.
- External/HTTP ``$ref`` URIs are always refused (local fragment refs only).
- Regex ``pattern`` length is bounded and evaluated without spawning processes.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import tomllib
from typing import Any

import sonder_runtime.adapters.filesystem.file_ops as file_ops
from sonder_runtime.adapters.inspection.regex_lab import detect_redos_hazards


MAX_DATA_BYTES = 16 * 1024 * 1024
MAX_SCHEMA_BYTES = 2 * 1024 * 1024
MAX_DEPTH = 48
MAX_ERRORS = 100
MAX_PATTERN_CHARS = 512
MAX_PATTERN_INPUT_CHARS = 65_536
MAX_MEDIUM_PATTERN_INPUT_CHARS = 4_096
MAX_JSONL_RECORDS = 10_000


class SchemaValidateError(ValueError):
    """Stable rejection from bounded schema validation."""


def _resolve_file(
    path: str,
    label: str,
    max_bytes: int,
    *,
    extra_roots: str = "",
    bypass: bool = False,
    developer_authorized: bool = False,
) -> Path:
    try:
        requested = file_ops._requested_path(str(path or ""))
        file_ops._require_no_reparse_components(requested)
    except PermissionError as exc:
        if "symlink or junction" in str(exc):
            raise SchemaValidateError("%s must not be a symlink or junction" % label) from exc
        raise SchemaValidateError("%s rejected: %s" % (label, exc)) from exc
    except (OSError, TypeError, ValueError) as exc:
        raise SchemaValidateError("%s rejected: %s" % (label, exc)) from exc
    try:
        target = file_ops.require_read_access(
            str(path or ""),
            extra_roots=extra_roots,
            bypass=bypass,
            developer_authorized=developer_authorized,
        )
    except (OSError, PermissionError, TypeError, ValueError) as exc:
        raise SchemaValidateError("%s rejected: %s" % (label, exc)) from exc
    if not target.exists() or not target.is_file():
        raise SchemaValidateError("%s must be an existing regular file" % label)
    for root in file_ops.allowed_roots(extra_roots if bypass else ""):
        resolved_root = file_ops._resolve_best_effort(root)
        if target == resolved_root or file_ops._is_inside(target, resolved_root):
            relative = target.relative_to(resolved_root)
            if any(
                part.lower() in file_ops.SENSITIVE_READ_DIRECTORIES
                for part in relative.parts
            ):
                raise SchemaValidateError("%s is secret or repository control state" % label)
            break
    size = target.stat().st_size
    if size > max_bytes:
        raise SchemaValidateError(
            "%s exceeds size ceiling (%d > %d bytes)" % (label, size, max_bytes)
        )
    return target


def _toml_to_jsonable(value: Any) -> Any:
    import datetime as _dt
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _toml_to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_toml_to_jsonable(v) for v in value]
    return value


def _load_data(target: Path) -> tuple[str, Any]:
    suffix = target.suffix.lower()
    raw = target.read_text(encoding="utf-8")
    if suffix == ".json":
        try:
            return "json", json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SchemaValidateError("invalid JSON data: %s" % exc) from exc
    if suffix in (".jsonl", ".ndjson"):
        records: list[Any] = []
        for idx, line in enumerate(raw.splitlines(), 1):
            if not line.strip():
                continue
            if len(records) >= MAX_JSONL_RECORDS:
                raise SchemaValidateError("JSONL exceeds record limit (%d)" % MAX_JSONL_RECORDS)
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SchemaValidateError("invalid JSONL at line %d: %s" % (idx, exc)) from exc
        return "jsonl", records
    if suffix == ".toml":
        try:
            return "toml", _toml_to_jsonable(tomllib.loads(raw))
        except Exception as exc:
            raise SchemaValidateError("invalid TOML data: %s" % exc) from exc
    raise SchemaValidateError(
        "unsupported data extension %r (supported: .json, .jsonl, .ndjson, .toml)" % target.suffix
    )


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return False


def _resolve_local_ref(root_schema: dict[str, Any], ref: str) -> dict[str, Any]:
    if not isinstance(ref, str) or not ref.startswith("#"):
        raise SchemaValidateError("only local fragment $ref values starting with '#' are supported")
    if ref == "#":
        return root_schema
    if not ref.startswith("#/"):
        raise SchemaValidateError("unsupported $ref pointer: %s" % ref)
    current: Any = root_schema
    for part in ref[2:].split("/"):
        token = part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            current = current[token]
        else:
            raise SchemaValidateError("unresolved local $ref: %s" % ref)
    if not isinstance(current, dict):
        raise SchemaValidateError("$ref target must be a schema object: %s" % ref)
    return current


def _validate_node(
    value: Any,
    schema: Any,
    root_schema: dict[str, Any],
    pointer: str,
    errors: list[dict[str, Any]],
    depth: int,
    active_refs: frozenset[str],
) -> None:
    if len(errors) >= MAX_ERRORS:
        return
    if depth > MAX_DEPTH:
        errors.append({"path": pointer, "message": "schema nesting depth ceiling exceeded"})
        return
    if isinstance(schema, bool):
        if not schema:
            errors.append({"path": pointer, "message": "value disallowed by boolean false schema"})
        return
    if not isinstance(schema, dict):
        raise SchemaValidateError("schema node at %s must be an object or boolean" % pointer)

    if "$ref" in schema:
        ref = schema["$ref"]
        if ref in active_refs:
            raise SchemaValidateError("cyclic $ref detected: %s" % ref)
        target = _resolve_local_ref(root_schema, ref)
        _validate_node(value, target, root_schema, pointer, errors, depth + 1, active_refs | {ref})

    if "type" in schema:
        expected = schema["type"]
        types = [expected] if isinstance(expected, str) else list(expected) if isinstance(expected, list) else []
        if types and not any(_matches_type(value, t) for t in types):
            errors.append({
                "path": pointer,
                "message": "expected type %s, got %s" % ("|".join(types), type(value).__name__),
            })
            return

    if "const" in schema and (value != schema["const"] or type(value) is not type(schema["const"])):
        errors.append({"path": pointer, "message": "value does not match const %r" % (schema["const"],)})

    if "enum" in schema:
        allowed = schema["enum"]
        if isinstance(allowed, list) and not any(
            value == item and (type(value) is type(item) or (
                isinstance(value, (int, float)) and not isinstance(value, bool)
                and isinstance(item, (int, float)) and not isinstance(item, bool)
            ))
            for item in allowed
        ):
            errors.append({"path": pointer, "message": "value %r not in enum" % (value,)})

    if isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            errors.append({"path": pointer, "message": "string length %d < minLength %d" % (len(value), int(schema["minLength"]))})
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            errors.append({"path": pointer, "message": "string length %d > maxLength %d" % (len(value), int(schema["maxLength"]))})
        if "pattern" in schema:
            pat = str(schema["pattern"])
            if len(pat) > MAX_PATTERN_CHARS:
                raise SchemaValidateError("schema pattern exceeds length ceiling")
            try:
                compiled = re.compile(pat)
            except re.error as exc:
                raise SchemaValidateError("invalid schema regex pattern: %s" % exc) from exc
            hazards = detect_redos_hazards(pat)
            high_hazards = [h["kind"] for h in hazards if h.get("severity") == "high"]
            if high_hazards:
                raise SchemaValidateError(
                    "schema regex pattern rejected due to ReDoS hazard (%s)" % ", ".join(high_hazards)
                )
            input_cap = MAX_MEDIUM_PATTERN_INPUT_CHARS if hazards else MAX_PATTERN_INPUT_CHARS
            if len(value) > input_cap:
                raise SchemaValidateError(
                    "string exceeds regex validation length ceiling (%d > %d chars)" % (len(value), input_cap)
                )
            if compiled.search(value) is None:
                errors.append({"path": pointer, "message": "string does not match pattern %r" % pat})

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append({"path": pointer, "message": "%r < minimum %r" % (value, schema["minimum"])})
        if "maximum" in schema and value > schema["maximum"]:
            errors.append({"path": pointer, "message": "%r > maximum %r" % (value, schema["maximum"])})
        if "exclusiveMinimum" in schema:
            ex_min = schema["exclusiveMinimum"]
            if isinstance(ex_min, (int, float)) and not isinstance(ex_min, bool) and value <= ex_min:
                errors.append({"path": pointer, "message": "%r <= exclusiveMinimum %r" % (value, ex_min)})
        if "exclusiveMaximum" in schema:
            ex_max = schema["exclusiveMaximum"]
            if isinstance(ex_max, (int, float)) and not isinstance(ex_max, bool) and value >= ex_max:
                errors.append({"path": pointer, "message": "%r >= exclusiveMaximum %r" % (value, ex_max)})
        if "multipleOf" in schema:
            mult = schema["multipleOf"]
            if isinstance(mult, (int, float)) and mult > 0:
                rem = abs(value / mult - round(value / mult))
                if rem > 1e-9:
                    errors.append({"path": pointer, "message": "%r is not a multiple of %r" % (value, mult)})

    if isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            errors.append({"path": pointer, "message": "array length %d < minItems %d" % (len(value), int(schema["minItems"]))})
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            errors.append({"path": pointer, "message": "array length %d > maxItems %d" % (len(value), int(schema["maxItems"]))})
        if schema.get("uniqueItems") is True:
            seen_encoded: set[str] = set()
            for idx, item in enumerate(value):
                key = json.dumps(item, sort_keys=True, separators=(",", ":"))
                if key in seen_encoded:
                    errors.append({"path": f"{pointer}[{idx}]", "message": "duplicate item violates uniqueItems"})
                    break
                seen_encoded.add(key)
        if "items" in schema:
            item_schema = schema["items"]
            for idx, item in enumerate(value):
                if len(errors) >= MAX_ERRORS:
                    break
                _validate_node(item, item_schema, root_schema, f"{pointer}[{idx}]", errors, depth + 1, active_refs)

    if isinstance(value, dict):
        if "minProperties" in schema and len(value) < int(schema["minProperties"]):
            errors.append({"path": pointer, "message": "object has %d properties < minProperties %d" % (len(value), int(schema["minProperties"]))})
        if "maxProperties" in schema and len(value) > int(schema["maxProperties"]):
            errors.append({"path": pointer, "message": "object has %d properties > maxProperties %d" % (len(value), int(schema["maxProperties"]))})
        for req_key in schema.get("required") or ():
            if req_key not in value:
                errors.append({"path": f"{pointer}.{req_key}", "message": "missing required property %r" % req_key})
        props = schema.get("properties") or {}
        additional = schema.get("additionalProperties", True)
        for k, v in value.items():
            if len(errors) >= MAX_ERRORS:
                break
            child_ptr = f"{pointer}.{k}"
            if k in props:
                _validate_node(v, props[k], root_schema, child_ptr, errors, depth + 1, active_refs)
            elif additional is False:
                errors.append({"path": child_ptr, "message": "unexpected additional property %r" % k})
            elif isinstance(additional, dict):
                _validate_node(v, additional, root_schema, child_ptr, errors, depth + 1, active_refs)

    if "allOf" in schema and isinstance(schema["allOf"], list):
        for sub in schema["allOf"]:
            _validate_node(value, sub, root_schema, pointer, errors, depth + 1, active_refs)

    if "anyOf" in schema and isinstance(schema["anyOf"], list):
        matched = False
        for sub in schema["anyOf"]:
            probe: list[dict[str, Any]] = []
            _validate_node(value, sub, root_schema, pointer, probe, depth + 1, active_refs)
            if not probe:
                matched = True
                break
        if not matched:
            errors.append({"path": pointer, "message": "value did not match any branch of anyOf"})

    if "oneOf" in schema and isinstance(schema["oneOf"], list):
        matches = 0
        for sub in schema["oneOf"]:
            probe = []
            _validate_node(value, sub, root_schema, pointer, probe, depth + 1, active_refs)
            if not probe:
                matches += 1
        if matches != 1:
            errors.append({"path": pointer, "message": "value matched %d branches of oneOf (expected 1)" % matches})

    if "not" in schema:
        probe = []
        _validate_node(value, schema["not"], root_schema, pointer, probe, depth + 1, active_refs)
        if not probe:
            errors.append({"path": pointer, "message": "value matched disallowed 'not' schema"})


def validate_schema(
    path: str,
    *,
    schema_path: str = "",
    schema_json: str = "",
    extra_roots: str = "",
    bypass: bool = False,
    developer_authorized: bool = False,
) -> dict[str, Any]:
    """Validate a guarded JSON, JSONL, or TOML file against a JSON Schema."""
    if bool(schema_path) == bool(schema_json):
        raise SchemaValidateError("provide exactly one of schema_path or schema_json")
    data_target = _resolve_file(
        path, "data path", MAX_DATA_BYTES,
        extra_roots=extra_roots, bypass=bypass, developer_authorized=developer_authorized,
    )
    if schema_path:
        s_target = _resolve_file(
            schema_path, "schema path", MAX_SCHEMA_BYTES,
            extra_roots=extra_roots, bypass=bypass, developer_authorized=developer_authorized,
        )
        try:
            schema = json.loads(s_target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SchemaValidateError("invalid JSON schema file: %s" % exc) from exc
        schema_source = str(s_target)
    else:
        if len(str(schema_json).encode("utf-8")) > MAX_SCHEMA_BYTES:
            raise SchemaValidateError("inline schema_json exceeds size ceiling")
        try:
            schema = json.loads(schema_json)
        except json.JSONDecodeError as exc:
            raise SchemaValidateError("invalid inline schema_json: %s" % exc) from exc
        schema_source = "inline"

    if not isinstance(schema, dict):
        raise SchemaValidateError("root JSON schema must be an object")

    data_format, payload = _load_data(data_target)
    errors: list[dict[str, Any]] = []
    if data_format == "jsonl":
        for idx, rec in enumerate(payload):
            if len(errors) >= MAX_ERRORS:
                break
            _validate_node(rec, schema, schema, f"$[{idx}]", errors, 0, frozenset())
        records_checked = len(payload)
    else:
        _validate_node(payload, schema, schema, "$", errors, 0, frozenset())
        records_checked = 1

    return {
        "ok": True,
        "valid": len(errors) == 0,
        "path": str(data_target),
        "data_format": data_format,
        "schema_source": schema_source,
        "records_checked": records_checked,
        "error_count": len(errors),
        "errors_truncated": len(errors) >= MAX_ERRORS,
        "errors": errors,
    }


def format_result(data: dict[str, Any]) -> str:
    verdict = "VALID" if data.get("valid") else "INVALID"
    lines = [
        f"schema validation ({verdict}): {data.get('path', '')}",
        f"  format: {data.get('data_format', '')}  schema: {data.get('schema_source', '')}  records: {data.get('records_checked', 0)}",
        f"  errors: {data.get('error_count', 0)}",
    ]
    for err in (data.get("errors") or [])[:25]:
        lines.append(f"  - {err.get('path', '$')}: {err.get('message', '')}")
    if data.get("errors_truncated"):
        lines.append("  (error list truncated at ceiling)")
    return "\n".join(lines)


__all__ = [
    "SchemaValidateError",
    "format_result",
    "validate_schema",
]
