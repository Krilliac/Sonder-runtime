import json
import time

import pytest

from sonder_runtime.domain.build.model import json_depth


def test_json_depth_basic():
    assert json_depth('{"a": [1, 2, 3]}') == 2
    assert json_depth('{"a": {"b": {"c": 1}}}') == 3
    assert json_depth('[]') == 1
    assert json_depth('{}') == 1
    assert json_depth('') == 0


def test_json_depth_with_strings():
    assert json_depth('{"a": " [1, 2, 3] "}') == 1
    assert json_depth('{"a": " \\" [1, 2, 3] \\" "}') == 1


def test_json_depth_large():
    large_string = '{"a": ' + '[' * 1000 + ']' * 1000 + '}'
    assert json_depth(large_string) == 1001


def test_json_depth_unterminated_escaped_quotes_is_linear():
    # The old regex-based implementation restarted at every quote in an
    # unterminated run of escaped quotes and was quadratic (12-19 s at 40k).
    n = 50000
    s = '"' + '\\"' * n

    start = time.process_time()
    depth = json_depth(s)
    elapsed = time.process_time() - start

    assert depth == 0
    assert elapsed < 2.0


def _expected_depth(value):
    if isinstance(value, dict):
        return 1 + max((_expected_depth(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_expected_depth(v) for v in value), default=0)
    return 0


_DOCS = [
    0,
    1.5,
    None,
    True,
    "",
    "plain",
    '"',
    '""',
    '\\',
    '\\\\',
    '\\"',
    '\\\\"',
    '\\\\\\"',
    'say "hi" \\ there',
    "[",
    "]",
    "{",
    "}",
    "[[[{{{",
    "]]]}}}",
    '{"a": [1, 2]}',
    "é中\U0001f600",
    "\u0001\u001f\n\t\r",
    [],
    {},
    [[]],
    [{}],
    {"a": []},
    {"a": {}},
    [[], [[]], []],
    [1, [2, [3, [4, [5]]]]],
    {"a": {"b": {"c": {"d": None}}}},
    {"[": "]", "{": "}"},
    {'"': '"', '\\': '\\'},
    {'k\\"ey': 'va\\\\"lue'},
    ["[", "{", "]", "}", '\\"[', '\\\\"]'],
    ["\\" * 7, '"' * 7, '\\"' * 7],
    {"a": ["]]]", {"b": "}}}"}], "c": "[[["},
    [{"x": [{"y": [{"z": "\\\\\\"}]}]}],
    {"nested": [[{"s": "a\\\"b[c{d"}], []], "e": {}},
    ["[", "{", "\\u005b"],
    [[1, 2], [3, [4, {"k": ["v", "]"]}]], "}"],
    {"deep": [[[[[[[[[["leaf"]]]]]]]]]]},
]


@pytest.mark.parametrize("obj", _DOCS, ids=lambda o: repr(o)[:40])
@pytest.mark.parametrize("ensure_ascii", [True, False])
@pytest.mark.parametrize("indent", [None, 2])
def test_json_depth_matches_json_loads(obj, ensure_ascii, indent):
    doc = json.dumps(obj, ensure_ascii=ensure_ascii, indent=indent)
    assert json_depth(doc) == _expected_depth(json.loads(doc))
