from sonder_runtime.domain import schema_policy

import pytest


def test_format_schema_gaps_preserves_order_and_wording():
    gaps = [("$.name", "required property is missing"), ("$.items", "items unchecked")]

    assert schema_policy.format_schema_gaps(gaps) == (
        "$.name (required property is missing); $.items (items unchecked)"
    )


def test_format_schema_gaps_bounds_detail_and_reports_remainder():
    gaps = [("$[%d]" % index, "unchecked") for index in range(10)]

    assert schema_policy.format_schema_gaps(gaps) == (
        "$[0] (unchecked); $[1] (unchecked); $[2] (unchecked); "
        "$[3] (unchecked); $[4] (unchecked); $[5] (unchecked); "
        "$[6] (unchecked); $[7] (unchecked); and 2 more"
    )


def test_format_schema_gaps_accepts_iterators_and_empty_input():
    assert schema_policy.format_schema_gaps(iter([])) == ""
    assert schema_policy.format_schema_gaps(iter([("$", "incomplete")])) == (
        "$ (incomplete)"
    )


def test_server_keeps_identity_compatible_alias():
    import server

    assert server._format_schema_gaps is schema_policy.format_schema_gaps


def test_leading_json_object_decodes_only_the_first_value():
    assert schema_policy.leading_json_object(
        '  {"answer": 42}\n[interaction_id: abc]'
    ) == {"answer": 42}


@pytest.mark.parametrize(
    "text, expected",
    [
        ("not json", "response did not begin with the JSON object the schema required"),
        ("[1, 2]", "response was a JSON list, not the object the schema required"),
        ("null", "response was a JSON NoneType, not the object the schema required"),
    ],
)
def test_leading_json_object_rejects_non_object_responses(text, expected):
    with pytest.raises(ValueError, match=expected):
        schema_policy.leading_json_object(text)


def test_server_leading_json_object_preserves_model_call_error_contract():
    import server

    with pytest.raises(server.ModelCallError, match="not the object the schema required") as raised:
        server._leading_json_object("[]")
    assert raised.value.kind == "protocol"
