"""Domain check that a model reply is JSON matching a schema."""
from __future__ import annotations

import pytest

from sonder_runtime.domain.json_reply import check_reply

pytestmark = pytest.mark.unit

SCHEMA = {
    "type": "object",
    "required": ["name", "count"],
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "count": {"type": "integer", "minimum": 0},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
        "kind": {"enum": ["a", "b"]},
    },
    "additionalProperties": False,
}


def test_valid_reply_returns_the_parsed_value():
    result = check_reply('{"name": "x", "count": 3}', SCHEMA)
    assert result.ok
    assert result.value == {"name": "x", "count": 3}
    assert result.errors == ()


def test_code_fence_around_json_is_accepted():
    result = check_reply('```json\n{"name": "x", "count": 1}\n```', SCHEMA)
    assert result.ok


def test_non_json_text_is_invalid_json():
    result = check_reply("sure, here you go", SCHEMA)
    assert not result.ok
    assert result.kind == "invalid_json"
    assert result.errors


@pytest.mark.parametrize("reply, fragment", [
    ('{"name": "x"}', "count"),
    ('{"name": "x", "count": "3"}', "$.count"),
    ('{"name": "x", "count": true}', "$.count"),
    ('{"name": "x", "count": -1}', "$.count"),
    ('{"name": "", "count": 1}', "$.name"),
    ('{"name": "x", "count": 1, "extra": 1}', "extra"),
    ('{"name": "x", "count": 1, "tags": ["a", "b", "c"]}', "$.tags"),
    ('{"name": "x", "count": 1, "tags": [1]}', "$.tags[0]"),
    ('{"name": "x", "count": 1, "kind": "z"}', "$.kind"),
    ('[1]', "$"),
])
def test_schema_violations_are_named_by_path(reply, fragment):
    result = check_reply(reply, SCHEMA)
    assert not result.ok
    assert result.kind == "schema_violation"
    assert any(fragment in error for error in result.errors)


def test_keywords_it_cannot_check_are_reported_not_passed():
    result = check_reply('{"a": 1}', {"type": "object", "if": {}, "then": {}})
    assert result.ok
    assert "if" in result.unchecked
    assert "then" in result.unchecked


def test_annotation_keywords_are_not_gaps():
    result = check_reply("1", {"type": "integer", "title": "t", "description": "d"})
    assert result.ok and result.unchecked == ()
