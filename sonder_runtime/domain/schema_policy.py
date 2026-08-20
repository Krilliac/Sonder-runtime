"""Pure policy for schema-constrained model responses."""

from __future__ import annotations

import json


MAX_REPORTED_SCHEMA_GAPS = 8


def leading_json_object(text):
    """Decode the first JSON value and require it to be an object.

    Schema-constrained model responses may carry protocol footers after the
    JSON body.  ``raw_decode`` intentionally stops at the end of that first
    value, while the domain-level ``ValueError`` keeps this policy independent
    from the transport layer's ``ModelCallError`` type.
    """
    body = (text or "").lstrip()
    try:
        data, _end = json.JSONDecoder().raw_decode(body)
    except ValueError as exc:
        raise ValueError(
            "response did not begin with the JSON object the schema required: %s"
            % exc
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(
            "response was a JSON %s, not the object the schema required"
            % type(data).__name__
        )
    return data


def format_schema_gaps(gaps) -> str:
    """Render bounded schema-verification gaps for human and machine callers."""
    gaps = list(gaps)
    shown = [
        "%s (%s)" % (path, reason)
        for path, reason in gaps[:MAX_REPORTED_SCHEMA_GAPS]
    ]
    remaining = len(gaps) - len(shown)
    if remaining > 0:
        shown.append("and %d more" % remaining)
    return "; ".join(shown)
