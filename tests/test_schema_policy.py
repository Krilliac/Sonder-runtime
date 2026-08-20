from sonder_runtime.domain import schema_policy


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
