import json

import json_schema_verifier as J


PERSON_SCHEMA = {
    "type": "object",
    "required": ["name", "age"],
    "properties": {
        "name": {"type": "string"},
        "age": {"type": "integer"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "address": {
            "type": "object",
            "required": ["city"],
            "properties": {"city": {"type": "string"}},
        },
    },
}


def test_valid_document_passes():
    doc = json.dumps({"name": "Ada", "age": 36, "tags": ["math", "logic"]})
    v = J.json_schema_verify(doc, {"schema": PERSON_SCHEMA})
    assert v.passed is True
    assert v.reason == "valid"


def test_valid_document_with_nested_object_passes():
    doc = json.dumps({"name": "Ada", "age": 36, "address": {"city": "London"}})
    v = J.json_schema_verify(doc, {"schema": PERSON_SCHEMA})
    assert v.passed is True


def test_missing_required_key_fails():
    doc = json.dumps({"name": "Ada"})
    v = J.json_schema_verify(doc, {"schema": PERSON_SCHEMA})
    assert v.passed is False
    assert "age" in v.reason
    assert "missing required key" in v.detail


def test_wrong_top_level_type_fails():
    doc = json.dumps(["not", "an", "object"])
    v = J.json_schema_verify(doc, {"schema": PERSON_SCHEMA})
    assert v.passed is False
    assert "expected type object" in v.detail


def test_wrong_field_type_fails():
    doc = json.dumps({"name": "Ada", "age": "thirty-six"})
    v = J.json_schema_verify(doc, {"schema": PERSON_SCHEMA})
    assert v.passed is False
    assert "$.age" in v.detail
    assert "expected type integer" in v.detail


def test_bool_is_not_accepted_as_integer():
    # bool is a subclass of int in Python -- must not sneak past an "integer" check.
    doc = json.dumps({"name": "Ada", "age": True})
    v = J.json_schema_verify(doc, {"schema": PERSON_SCHEMA})
    assert v.passed is False
    assert "$.age" in v.detail


def test_array_items_are_validated_and_report_index():
    doc = json.dumps({"name": "Ada", "age": 36, "tags": ["ok", 5]})
    v = J.json_schema_verify(doc, {"schema": PERSON_SCHEMA})
    assert v.passed is False
    assert "$.tags[1]" in v.detail


def test_multiple_violations_are_all_reported():
    doc = json.dumps({"age": "not-a-number"})
    v = J.json_schema_verify(doc, {"schema": PERSON_SCHEMA})
    assert v.passed is False
    assert "2 schema violations" in v.reason
    assert "missing required key 'name'" in v.detail
    assert "$.age" in v.detail


def test_invalid_json_fails_with_reason():
    v = J.json_schema_verify("{not valid json", {"schema": PERSON_SCHEMA})
    assert v.passed is False
    assert "invalid json" in v.reason


def test_missing_schema_fails_without_raising():
    v = J.json_schema_verify(json.dumps({"a": 1}), {})
    assert v.passed is False
    assert "no schema" in v.reason


def test_unknown_schema_type_is_reported_not_raised():
    v = J.json_schema_verify(json.dumps(1), {"schema": {"type": "widget"}})
    assert v.passed is False
    assert "unknown schema type" in v.detail


def test_any_type_accepts_anything():
    for payload in (1, "s", [1, 2], {"k": "v"}, None, True):
        v = J.json_schema_verify(json.dumps(payload), {"schema": {"type": "any"}})
        assert v.passed is True


def test_validate_helper_returns_plain_error_list():
    errors = J.validate({"age": 1}, PERSON_SCHEMA)
    assert isinstance(errors, list)
    assert any("name" in e for e in errors)
