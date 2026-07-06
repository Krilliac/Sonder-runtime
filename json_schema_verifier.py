"""json_schema_verifier -- a stdlib-only oracle for "does this JSON match a
minimal schema (types + required keys)".

Fits the repo's verifier contract (see verifiers.py): fn(artifact, spec) ->
Verdict(passed, reason, detail). `artifact` is a JSON string; `spec={"schema":
{...}}` gives the minimal schema to check it against. There is no external
tool or model involved, so this verifier never raises VerifierUnavailable --
malformed JSON or a bad/missing schema is just a failed Verdict explaining why.

Schema shape (a small JSON-Schema subset, stdlib types only):
    {"type": "object"|"array"|"string"|"integer"|"number"|"boolean"|"null"|"any",
     "required": [<key>, ...],                  # object nodes only
     "properties": {<key>: <subschema>, ...},   # object nodes only
     "items": <subschema>}                      # array nodes only, applied
                                                 # to every element

A missing "type" means "accept any value" at that node, so a schema can start
as shallow as {"type": "object", "required": [...]} and grow "properties"/
"items" only where nested validation is actually wanted.
"""
import collections
import json

Verdict = collections.namedtuple("Verdict", ["passed", "reason", "detail"])

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    # bool is a subclass of int in Python -- exclude it from integer/number
    # so {"type": "integer"} doesn't silently accept `true`/`false`.
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    "any": lambda v: True,
}


def _validate(value, schema, path, errors):
    """Recursively check value against schema, appending 'path: message' to errors."""
    if not isinstance(schema, dict):
        errors.append("%s: schema node must be an object, got %r" % (path, schema))
        return

    expected = schema.get("type", "any")
    check = _TYPE_CHECKS.get(expected)
    if check is None:
        errors.append("%s: unknown schema type %r" % (path, expected))
        return
    if not check(value):
        errors.append("%s: expected type %s, got %s" % (path, expected, type(value).__name__))
        return  # nested checks below would be meaningless on a type mismatch

    if expected == "object":
        for key in schema.get("required", []):
            if key not in value:
                errors.append("%s: missing required key %r" % (path, key))
        for key, subschema in schema.get("properties", {}).items():
            if key in value:
                _validate(value[key], subschema, "%s.%s" % (path, key), errors)

    elif expected == "array":
        items_schema = schema.get("items")
        if items_schema is not None:
            for i, item in enumerate(value):
                _validate(item, items_schema, "%s[%d]" % (path, i), errors)


def validate(data, schema):
    """Validate an already-parsed JSON value against schema. Returns a list of
    error strings (empty list == valid). Pure function, no I/O -- usable on
    its own outside the verifier seam, e.g. to check config dicts in-process."""
    errors = []
    _validate(data, schema, "$", errors)
    return errors


def json_schema_verify(artifact, spec=None):
    """Verifier-registry entrypoint: fn(artifact: str, spec: dict) -> Verdict.
    spec={"schema": {...}}. `artifact` is the raw JSON text to validate.
    Never raises -- invalid JSON or a missing schema is a failed Verdict, not
    VerifierUnavailable (nothing external could be "unavailable" here)."""
    spec = spec or {}
    schema = spec.get("schema")
    if schema is None:
        return Verdict(False, "no schema provided", "spec['schema'] was missing or None")

    try:
        data = json.loads(artifact)
    except json.JSONDecodeError as e:
        return Verdict(False, "invalid json: %s" % e, str(e))

    errors = validate(data, schema)
    if not errors:
        return Verdict(True, "valid", "matches schema")
    reason = errors[0] if len(errors) == 1 else "%d schema violations" % len(errors)
    return Verdict(False, reason, "\n".join(errors))
