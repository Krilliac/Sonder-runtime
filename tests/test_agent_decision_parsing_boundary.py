"""Agent decision parsing lives in the domain; the root name is an alias."""
import pytest

import server
from sonder_runtime.domain.agents import decision_parsing


def test_root_helper_is_an_identity_preserving_alias():
    assert server._extract_agent_json is decision_parsing.extract_agent_json


def test_plain_and_fenced_json_parse_to_the_same_decision():
    plain = decision_parsing.extract_agent_json('{"tool": "status", "args": {}}')
    fenced = decision_parsing.extract_agent_json('```json\n{"tool": "status", "args": {}}\n```')
    bare_fence = decision_parsing.extract_agent_json('```\n{"tool": "status", "args": {}}\n```')
    assert plain == fenced == bare_fence == {"tool": "status", "args": {}}


def test_prose_framing_and_braces_inside_strings_are_tolerated():
    text = 'Sure, here is my decision:\n{"tool": "file_read", "args": {"path": "a{b}c"}}\nLet me know.'
    out = decision_parsing.extract_agent_json(text)
    assert out == {"tool": "file_read", "args": {"path": "a{b}c"}}
    quoted = 'ok {"final": "a \\"}\\" brace"} trailing'
    assert decision_parsing.extract_agent_json(quoted) == {"final": 'a "}" brace'}


def test_truncated_or_missing_json_raises_for_the_repair_loop():
    with pytest.raises(ValueError):
        decision_parsing.extract_agent_json('{"final": "half a sen')
    with pytest.raises(ValueError):
        decision_parsing.extract_agent_json("no json here at all")
    with pytest.raises(ValueError):
        decision_parsing.extract_agent_json("")
