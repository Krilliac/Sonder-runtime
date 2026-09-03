"""Context pack argument normalization lives in the domain; root names are aliases."""
import pytest

import server
from sonder_runtime.domain.context import pack_arguments


def test_root_names_are_identity_preserving_aliases():
    assert server._context_pack_paths is pack_arguments.pack_paths
    assert server._context_pack_int is pack_arguments.pack_int
    assert server._context_pack_utf8_prefix is pack_arguments.pack_utf8_prefix


def test_paths_accept_json_or_lists_and_reject_malformed_items():
    assert pack_arguments.pack_paths('["a.py", " b.py "]') == ["a.py", "b.py"]
    assert pack_arguments.pack_paths(["c.py"]) == ["c.py"]
    for bad in ("not json", '{"a": 1}', "[]", "[1]", '[""]', '["a\\u0000b"]'):
        with pytest.raises(ValueError):
            pack_arguments.pack_paths(bad)


def test_integers_are_clamped_with_defaults():
    assert pack_arguments.pack_int("7", 3, 10) == 7
    assert pack_arguments.pack_int("x", 3, 10) == 3
    assert pack_arguments.pack_int(0, 3, 10) == 1
    assert pack_arguments.pack_int(99, 3, 10) == 10
    assert pack_arguments.pack_int(None, 5, 10) == 5


def test_utf8_prefix_never_splits_a_codepoint():
    assert pack_arguments.pack_utf8_prefix("abc", 10) == ("abc", 3, False)
    assert pack_arguments.pack_utf8_prefix(None, 10) == ("", 0, False)
    assert pack_arguments.pack_utf8_prefix("aé", 2) == ("a", 1, True)
    assert pack_arguments.pack_utf8_prefix("héllo", 3) == ("hé", 3, True)
