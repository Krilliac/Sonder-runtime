import json

import pytest

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


# --- the widened keyword surface ---------------------------------------------
# Everything below was added when the verifier stopped returning "no errors" for
# data that plainly broke its schema. Each block below covers a keyword the
# verifier used to ignore outright: `validate` returned [] and
# `json_schema_verify` returned Verdict(passed=True, "valid") against data that
# violated it.


def _errors(datum, schema):
    return J.validate(datum, schema)


def _reasons(datum, schema):
    return " ".join(reason for _, reason in J.coverage_gaps(datum, schema))


# --- enum / const -------------------------------------------------------------

def test_enum_violation_is_reported():
    assert _errors("purple", {"type": "string", "enum": ["red", "green"]})


def test_enum_match_passes():
    assert _errors("red", {"type": "string", "enum": ["red", "green"]}) == []


def test_enum_does_not_confuse_true_with_one():
    # Python's True == 1; JSON Schema's enum equality does not.
    assert _errors(True, {"enum": [1]})
    assert _errors(1, {"enum": [True]})


def test_enum_compares_composite_values_structurally():
    assert _errors({"a": [1, 2]}, {"enum": [{"a": [1, 2]}]}) == []
    assert _errors({"a": [2, 1]}, {"enum": [{"a": [1, 2]}]})


def test_const_violation_is_reported():
    assert _errors("no", {"const": "yes"})
    assert _errors("yes", {"const": "yes"}) == []


def test_a_non_array_enum_is_a_schema_error_not_a_silent_pass():
    assert _errors("x", {"enum": "red"})


# --- numeric bounds -----------------------------------------------------------

@pytest.mark.parametrize("schema, bad, good", [
    ({"type": "integer", "minimum": 0}, -5, 0),
    ({"type": "integer", "maximum": 10}, 11, 10),
    ({"type": "integer", "exclusiveMinimum": 0}, 0, 1),
    ({"type": "integer", "exclusiveMaximum": 10}, 10, 9),
    ({"type": "integer", "multipleOf": 3}, 7, 9),
])
def test_numeric_bounds_are_enforced(schema, bad, good):
    assert _errors(bad, schema)
    assert _errors(good, schema) == []


def test_a_bound_names_the_offending_path():
    schema = {"type": "object", "properties": {"n": {"type": "integer", "minimum": 0}}}
    detail = "\n".join(_errors({"n": -1}, schema))
    assert "$.n" in detail
    assert "minimum" in detail


# --- string constraints -------------------------------------------------------

@pytest.mark.parametrize("schema, bad, good", [
    ({"type": "string", "minLength": 3}, "ab", "abc"),
    ({"type": "string", "maxLength": 3}, "abcd", "abc"),
    ({"type": "string", "pattern": "^a+$"}, "zzz", "aaa"),
])
def test_string_constraints_are_enforced(schema, bad, good):
    assert _errors(bad, schema)
    assert _errors(good, schema) == []


def test_pattern_is_unanchored_like_json_schema():
    assert _errors("xxaxx", {"type": "string", "pattern": "a"}) == []


def test_a_pattern_python_cannot_compile_is_a_gap_not_a_failed_match():
    # JSON Schema's dialect is ECMA-262, Python's `re` is not quite it. A
    # pattern that fails to compile here is this module's blind spot; rejecting
    # the data for it would remove capability the caller legitimately has,
    # since whatever produced the data was constrained by the whole schema.
    for pattern in ("(", "(?<year>\\d{4})"):
        schema = {"type": "string", "pattern": pattern}
        assert _errors("x", schema) == []
        assert "pattern" in _reasons("x", schema)
        assert J.json_schema_verify(json.dumps("x"), {"schema": schema}).passed is False


def test_a_tuple_form_items_is_a_gap_rather_than_a_rejection():
    schema = {"type": "array", "items": [{"type": "string"}, {"type": "integer"}]}
    assert _errors(["a", 1], schema) == []
    assert "items" in _reasons(["a", 1], schema)


@pytest.mark.parametrize("schema, datum", [
    ({"type": "object", "required": "name"}, {"name": "Ada"}),
    ({"type": "object", "properties": ["name"]}, {"name": "Ada"}),
    ({"type": "object", "patternProperties": ["^a"]}, {"name": "Ada"}),
    ({"type": "object", "additionalProperties": 7}, {"name": "Ada"}),
    ({"type": "object", "minProperties": "2"}, {"name": "Ada"}),
    ({"type": "array", "maxItems": 1.5}, [1, 2]),
    ({"type": "array", "uniqueItems": "yes"}, [1, 1]),
    ({"type": "string", "minLength": -1}, "Ada"),
    ({"type": "integer", "minimum": "0"}, 1),
    ({"type": "integer", "multipleOf": 0}, 1),
])
def test_a_keyword_with_an_unusable_value_is_an_error_and_a_gap(schema, datum):
    # Two failure shapes at once. "required": "name" used to iterate the string
    # and check for the keys 'n','a','m','e'. The bounds used an isinstance
    # guard that skipped a bad value in silence -- a guard that no-ops, which is
    # the shape of the bug this whole module is being fixed for.
    result = J.check(datum, schema)
    assert result.errors
    assert result.unchecked


# --- array constraints --------------------------------------------------------

@pytest.mark.parametrize("schema, bad, good", [
    ({"type": "array", "minItems": 2}, [1], [1, 2]),
    ({"type": "array", "maxItems": 2}, [1, 2, 3], [1, 2]),
    ({"type": "array", "uniqueItems": True}, [1, 1], [1, 2]),
])
def test_array_constraints_are_enforced(schema, bad, good):
    assert _errors(bad, schema)
    assert _errors(good, schema) == []


def test_unique_items_compares_unhashable_elements():
    assert _errors([{"a": 1}, {"a": 1}], {"type": "array", "uniqueItems": True})
    assert _errors([{"a": 1}, {"a": 2}], {"type": "array", "uniqueItems": True}) == []


# --- object constraints -------------------------------------------------------

def test_additional_properties_false_rejects_an_extra_key():
    schema = {"type": "object", "properties": {"a": {"type": "integer"}},
              "additionalProperties": False}
    detail = "\n".join(_errors({"a": 1, "b": 2}, schema))
    assert "b" in detail
    assert _errors({"a": 1}, schema) == []


def test_additional_properties_schema_is_applied_to_the_extras():
    schema = {"type": "object", "properties": {"a": {"type": "integer"}},
              "additionalProperties": {"type": "string"}}
    assert _errors({"a": 1, "b": 2}, schema)
    assert _errors({"a": 1, "b": "ok"}, schema) == []


def test_pattern_properties_are_applied_and_count_as_known_keys():
    schema = {"type": "object", "patternProperties": {"^s_": {"type": "string"}},
              "additionalProperties": False}
    assert _errors({"s_a": 1}, schema)
    assert _errors({"s_a": "ok"}, schema) == []
    assert _errors({"other": "x"}, schema)


def test_property_names_are_validated():
    schema = {"type": "object", "propertyNames": {"pattern": "^[a-z]+$"}}
    assert _errors({"OK": 1}, schema)
    assert _errors({"ok": 1}, schema) == []


@pytest.mark.parametrize("schema, bad, good", [
    ({"type": "object", "minProperties": 2}, {"a": 1}, {"a": 1, "b": 2}),
    ({"type": "object", "maxProperties": 1}, {"a": 1, "b": 2}, {"a": 1}),
])
def test_property_counts_are_enforced(schema, bad, good):
    assert _errors(bad, schema)
    assert _errors(good, schema) == []


# --- combinators --------------------------------------------------------------

def test_all_of_requires_every_branch():
    schema = {"allOf": [{"type": "integer"}, {"minimum": 5}]}
    assert _errors(1, schema)
    assert _errors(7, schema) == []


def test_any_of_requires_at_least_one_branch():
    schema = {"anyOf": [{"type": "integer"}, {"type": "string"}]}
    assert _errors([], schema)
    assert _errors("x", schema) == []


def test_one_of_requires_exactly_one_branch():
    schema = {"oneOf": [{"type": "integer"}, {"type": "integer", "minimum": 5}]}
    assert _errors(7, schema)          # matches both
    assert _errors("x", schema)        # matches neither
    assert _errors(1, schema) == []    # the unbounded branch only


def test_one_of_with_an_undecidable_branch_is_unchecked_not_a_violation():
    # {"minimum": 5} says nothing about a string, so this verifier cannot rule
    # that branch in or out -- and guessing "out" would reject valid data.
    schema = {"oneOf": [{"type": "integer"}, {"minimum": 5}]}
    assert _errors("x", schema) == []
    assert "did not apply" in _reasons("x", schema)


def test_not_inverts_its_subschema():
    schema = {"not": {"type": "string"}}
    assert _errors("x", schema)
    assert _errors(1, schema) == []


def test_a_combinator_branch_reports_a_nested_path():
    schema = {"type": "object",
              "properties": {"v": {"anyOf": [{"type": "integer"}, {"type": "string"}]}}}
    assert "$.v" in "\n".join(_errors({"v": []}, schema))


def test_a_branch_this_verifier_cannot_decide_is_unchecked_not_passed():
    # An undecidable branch must not be read as a satisfied one.
    schema = {"anyOf": [{"type": "string", "format": "date-time"}]}
    assert _errors("ada", schema) == []
    assert _reasons("ada", schema)


# --- $ref: the gap that let a wholly wrong shape verify -----------------------

REF_SCHEMA = {
    "$defs": {"person": {"type": "object", "required": ["name"],
                         "properties": {"name": {"type": "string"}}}},
    "$ref": "#/$defs/person",
}


def test_a_ref_is_followed_and_a_wrong_shape_fails():
    # The end-to-end defect: a $ref node carries no "type", so the verifier
    # defaulted to "any" and "totally the wrong shape" came back verified.
    assert _errors("totally the wrong shape", REF_SCHEMA)
    v = J.json_schema_verify(json.dumps("totally the wrong shape"),
                             {"schema": REF_SCHEMA})
    assert v.passed is False


def test_a_ref_that_resolves_and_matches_passes_cleanly():
    assert _errors({"name": "Ada"}, REF_SCHEMA) == []
    assert J.coverage_gaps({"name": "Ada"}, REF_SCHEMA) == []


def test_a_ref_violation_inside_the_target_is_reported():
    assert _errors({"name": 7}, REF_SCHEMA)


def test_a_ref_to_the_document_root_resolves():
    schema = {"type": "object", "properties": {"next": {"$ref": "#"}}}
    assert _errors({"next": {"next": {}}}, schema) == []
    assert _errors({"next": []}, schema)


def test_a_definitions_style_ref_resolves_too():
    schema = {"definitions": {"s": {"type": "string"}}, "$ref": "#/definitions/s"}
    assert _errors("x", schema) == []
    assert _errors(1, schema)


def test_an_unresolvable_ref_is_an_error_not_a_pass():
    assert _errors(1, {"$ref": "#/$defs/nope"})


def test_an_external_ref_is_reported_unchecked_rather_than_accepted():
    schema = {"$ref": "https://example.invalid/s.json"}
    assert "$ref" in _reasons(1, schema)
    assert J.json_schema_verify(json.dumps(1), {"schema": schema}).passed is False


def test_a_recursive_ref_terminates_and_is_reported_unchecked():
    schema = {"$defs": {"loop": {"$ref": "#/$defs/loop"}}, "$ref": "#/$defs/loop"}
    assert J.coverage_gaps(1, schema)
    assert J.validate(1, schema) == []


def test_a_recursive_ref_over_real_data_still_validates_each_level():
    schema = {"$defs": {"node": {"type": "object",
                                 "properties": {"child": {"$ref": "#/$defs/node"},
                                                "n": {"type": "integer"}}}},
              "$ref": "#/$defs/node"}
    assert _errors({"n": 1, "child": {"n": 2}}, schema) == []
    assert "$.child.n" in "\n".join(_errors({"n": 1, "child": {"n": "x"}}, schema))


def test_ref_siblings_are_applied_alongside_the_target():
    schema = {"$defs": {"s": {"type": "string"}},
              "$ref": "#/$defs/s", "minLength": 3}
    assert _errors("ab", schema)
    assert _errors("abc", schema) == []


# --- a node with no "type" ----------------------------------------------------

def test_properties_without_an_explicit_object_type_are_applied_to_an_object():
    schema = {"required": ["name"], "properties": {"name": {"type": "string"}}}
    assert _errors({}, schema)
    assert _errors({"name": 7}, schema)
    assert _errors({"name": "Ada"}, schema) == []


def test_object_keywords_that_could_not_apply_are_reported_unchecked():
    # The author plainly meant an object; 42 is not one, and silence here is
    # what let a missing "type" accept any value at all.
    schema = {"required": ["name"], "properties": {"name": {"type": "string"}}}
    reported = _reasons(42, schema)
    assert "required" in reported and "properties" in reported
    assert J.json_schema_verify(json.dumps(42), {"schema": schema}).passed is False


def test_an_empty_schema_node_still_accepts_anything():
    for payload in (1, "s", [1], {"k": "v"}, None, True):
        assert _errors(payload, {}) == []
        assert J.coverage_gaps(payload, {}) == []


# --- type unions and boolean schemas ------------------------------------------

def test_a_type_union_is_evaluated_rather_than_crashing():
    schema = {"type": ["string", "null"]}
    assert _errors("x", schema) == []
    assert _errors(None, schema) == []
    assert _errors(1, schema)


def test_boolean_schemas_are_honoured():
    assert _errors(1, True) == []
    assert _errors(1, False)


# --- fail closed on what is still not checked ---------------------------------

@pytest.mark.parametrize("keyword, schema, datum", [
    ("if", {"type": "integer", "if": {"minimum": 0}}, 1),
    ("contains", {"type": "array", "contains": {"type": "integer"}}, [1]),
    ("dependentRequired", {"type": "object", "dependentRequired": {"a": ["b"]}}, {"a": 1}),
    ("unevaluatedProperties", {"type": "object", "unevaluatedProperties": False}, {"a": 1}),
    ("format", {"type": "string", "format": "date-time"}, "x"),
    ("x-invented-by-nobody", {"type": "string", "x-invented-by-nobody": True}, "x"),
])
def test_a_keyword_this_verifier_cannot_check_is_never_treated_as_satisfied(
        keyword, schema, datum):
    assert keyword in _reasons(datum, schema)
    v = J.json_schema_verify(json.dumps(datum), {"schema": schema})
    assert v.passed is False
    assert "not checked" in v.detail


def test_annotations_are_not_reported_as_unchecked():
    schema = {"type": "string", "title": "Name", "description": "d",
              "default": "x", "examples": ["y"], "$comment": "c"}
    assert J.coverage_gaps("ada", schema) == []
    assert J.json_schema_verify(json.dumps("ada"), {"schema": schema}).passed is True


def test_an_unchecked_gap_is_reported_at_its_nested_path():
    schema = {"type": "object",
              "properties": {"rows": {"type": "array",
                                      "items": {"type": "integer", "format": "int64"}}}}
    gaps = J.coverage_gaps({"rows": [1]}, schema)
    assert any(path == "$.rows[0]" for path, _ in gaps)


def test_coverage_is_data_driven_so_an_unwalked_branch_is_not_claimed():
    # The verifier only enters properties[key] when the key is present, so a
    # gap under an absent key was never traversed and must not be reported as
    # covered *or* as a gap the data actually hit.
    schema = {"type": "object",
              "properties": {"absent": {"type": "string", "format": "email"}}}
    assert J.coverage_gaps({}, schema) == []
    assert J.coverage_gaps({"absent": "x"}, schema)


def test_validate_returns_violations_only_not_coverage_gaps():
    # `validate` stays the violations channel, so a caller that only wants "did
    # this break the schema" is not handed "and here is what I could not see".
    schema = {"type": "string", "format": "date-time"}
    assert J.validate("ada", schema) == []
    assert J.coverage_gaps("ada", schema)


def test_check_returns_both_channels_separately():
    result = J.check(1, {"type": "string", "format": "date-time"})
    assert result.errors
    assert result.unchecked


# --- the mirror: what is claimed checked really is ----------------------------

_VIOLATIONS = {
    "type": ({"type": "string"}, 1),
    "required": ({"type": "object", "required": ["a"]}, {}),
    "properties": ({"type": "object", "properties": {"a": {"type": "string"}}}, {"a": 1}),
    "items": ({"type": "array", "items": {"type": "integer"}}, ["x"]),
    "enum": ({"enum": ["red"]}, "purple"),
    "const": ({"const": "red"}, "purple"),
    "minimum": ({"minimum": 0}, -1),
    "maximum": ({"maximum": 0}, 1),
    "exclusiveMinimum": ({"exclusiveMinimum": 0}, 0),
    "exclusiveMaximum": ({"exclusiveMaximum": 0}, 0),
    "multipleOf": ({"multipleOf": 2}, 3),
    "minLength": ({"minLength": 2}, "a"),
    "maxLength": ({"maxLength": 1}, "ab"),
    "pattern": ({"pattern": "^a$"}, "zzz"),
    "minItems": ({"minItems": 2}, [1]),
    "maxItems": ({"maxItems": 1}, [1, 2]),
    "uniqueItems": ({"uniqueItems": True}, [1, 1]),
    "additionalProperties": ({"additionalProperties": False,
                              "properties": {"a": {}}}, {"b": 1}),
    "patternProperties": ({"patternProperties": {"^a": {"type": "string"}}}, {"ab": 1}),
    "propertyNames": ({"propertyNames": {"pattern": "^a$"}}, {"b": 1}),
    "minProperties": ({"minProperties": 2}, {"a": 1}),
    "maxProperties": ({"maxProperties": 1}, {"a": 1, "b": 2}),
    "allOf": ({"allOf": [{"type": "integer"}]}, "x"),
    "anyOf": ({"anyOf": [{"type": "integer"}]}, "x"),
    "oneOf": ({"oneOf": [{"type": "integer"}]}, "x"),
    "not": ({"not": {"type": "string"}}, "x"),
    "$ref": ({"$defs": {"s": {"type": "string"}}, "$ref": "#/$defs/s"}, 1),
}


def test_every_keyword_claimed_checked_really_rejects_a_violation():
    # The mirror a downstream coverage report derives from: if a keyword is in
    # CHECKED_KEYWORDS, some violating datum must actually fail.
    assert set(_VIOLATIONS) == set(J.CHECKED_KEYWORDS), (
        "CHECKED_KEYWORDS and the violation table disagree: %s"
        % sorted(set(_VIOLATIONS) ^ set(J.CHECKED_KEYWORDS)))
    for keyword, (schema, datum) in sorted(_VIOLATIONS.items()):
        assert J.validate(datum, schema), keyword


def test_a_checked_keyword_is_never_reported_as_a_coverage_gap():
    for keyword, (schema, datum) in sorted(_VIOLATIONS.items()):
        reported = _reasons(datum, schema)
        assert "%s not checked" % keyword not in reported, keyword


def test_checked_and_annotation_keywords_do_not_overlap():
    assert not (J.CHECKED_KEYWORDS & J.ANNOTATION_KEYWORDS)


def test_a_fully_checked_schema_reports_no_gaps():
    doc = {"name": "Ada", "age": 36, "tags": ["math"], "address": {"city": "London"}}
    assert J.coverage_gaps(doc, PERSON_SCHEMA) == []


# --- hard failure, never a silent repair --------------------------------------

def test_a_violation_never_mutates_the_data():
    datum = {"name": "Ada", "age": "36", "tags": ["a", "a"]}
    before = json.dumps(datum, sort_keys=True)
    schema = {"type": "object",
              "properties": {"name": {"type": "string", "minLength": 99},
                             "age": {"type": "integer"},
                             "tags": {"type": "array", "uniqueItems": True}}}
    assert J.validate(datum, schema)
    assert json.dumps(datum, sort_keys=True) == before


def test_a_violating_document_is_a_failed_verdict_with_every_error():
    schema = {"type": "object",
              "properties": {"color": {"type": "string", "enum": ["red"]},
                             "n": {"type": "integer", "minimum": 0}}}
    v = J.json_schema_verify(json.dumps({"color": "purple", "n": -1}), {"schema": schema})
    assert v.passed is False
    assert "$.color" in v.detail and "$.n" in v.detail


@pytest.mark.parametrize("schema", [
    "not a schema node",
    {"type": "widget"},
    {"$ref": "#/$defs/nope"},
])
def test_a_schema_node_this_verifier_cannot_evaluate_fills_both_channels(schema):
    # An un-evaluable *schema* is an error AND a coverage gap: a caller told
    # only "this is invalid" would still not know the subtree went unlooked-at.
    result = J.check(1, schema)
    assert result.errors
    assert result.unchecked


def test_a_plain_type_mismatch_is_a_violation_and_not_a_coverage_gap():
    # The schema was understood perfectly; the data broke it. Reporting that as
    # unchecked would drown the real gaps in noise.
    assert J.check(1, {"type": "string"}).errors
    assert J.check(1, {"type": "string"}).unchecked == []
