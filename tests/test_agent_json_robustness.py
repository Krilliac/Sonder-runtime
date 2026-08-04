"""_extract_agent_json tolerates fences and prose (7B-run finding)."""
from __future__ import annotations

import pytest

import server


def test_plain_object():
    assert server._extract_agent_json('{"tool": "status", "args": {}}')["tool"] == "status"


def test_json_fence_wrapper():
    text = '```json\n{"final": "done", "n": 3}\n```'
    out = server._extract_agent_json(text)
    assert out["final"] == "done" and out["n"] == 3


def test_bare_fence_wrapper():
    out = server._extract_agent_json('```\n{"tool": "file_read"}\n```')
    assert out["tool"] == "file_read"


def test_prose_before_and_after_object():
    text = 'Here is my decision:\n{"tool": "text_search", "args": {"q": "x"}}\nDone.'
    out = server._extract_agent_json(text)
    assert out["tool"] == "text_search"


def test_braces_inside_strings_do_not_break_scan():
    text = '{"final": "use a dict like {key: value} here", "ok": true}'
    out = server._extract_agent_json(text)
    assert out["ok"] is True
    assert "{key: value}" in out["final"]


def test_first_complete_object_wins_over_trailing_text():
    text = '{"tool": "status", "args": {}}\n\nsome trailing commentary {incomplete'
    out = server._extract_agent_json(text)
    assert out["tool"] == "status"


def test_truncated_json_still_raises():
    with pytest.raises(ValueError):
        server._extract_agent_json('{"final": "half a sen')


def test_no_object_raises():
    with pytest.raises(ValueError):
        server._extract_agent_json("no json here at all")
