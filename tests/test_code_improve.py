"""The guards that make single-function improvement safe.

Each rejection test pins a failure that was measured slipping through a green
test suite during the self-modification work; see code_improve for the origin
of each rule.
"""
import ast

import code_improve as ci


_MODULE = '''\
import os


def alpha(x):
    return x + 1


def beta(value, default):
    if not value:
        return int(default)
    return value


def gamma(schema):
    expected = schema.get("type", "any")
    return expected
'''


def test_list_and_extract_functions():
    assert ci.list_functions(_MODULE) == ["alpha", "beta", "gamma"]
    fn = ci.extract_function(_MODULE, "beta")
    assert fn.startswith("def beta(value, default):")
    assert "return int(default)" in fn
    assert ci.extract_function(_MODULE, "nope") is None


def test_splice_replaces_one_function_and_keeps_neighbours():
    reply = "def alpha(x):\n    # clamp negatives\n    return max(0, x + 1)\n"
    out = ci.splice_function(_MODULE, reply)
    assert out is not None
    ast.parse(out)  # must still parse
    assert "return max(0, x + 1)" in out
    assert "def beta(value, default):" in out
    assert "def gamma(schema):" in out


def test_splice_rejects_a_function_the_module_does_not_have():
    assert ci.splice_function(_MODULE, "def invented(z):\n    return z\n") is None
    assert ci.splice_function(_MODULE, "just prose, no def") is None


def test_splice_handles_a_multiline_signature():
    module = "def f(\n    a,\n    b,\n):\n    return a + b\n\n\nX = 1\n"
    reply = "def f(\n    a,\n    b,\n):\n    return a - b\n"
    out = ci.splice_function(module, reply)
    ast.parse(out)
    assert "return a - b" in out and "X = 1" in out and "return a + b" not in out


def _ask(reply):
    return lambda prompt, tier: reply


def test_improve_accepts_a_genuine_guard():
    reply = (
        "def gamma(schema):\n"
        "    expected = schema.get(\"type\", \"any\")\n"
        "    if not isinstance(expected, str):\n"
        "        expected = \"any\"\n"
        "    return expected\n"
    )
    res = ci.improve_function(_MODULE, "gamma", _ask(reply))
    assert res["ok"], res["reason"]
    ast.parse(res["edited"])
    assert res["diff"]
    assert "isinstance" in res["edited"]


def test_improve_rejects_a_comment_only_change():
    reply = "def alpha(x):\n    return x + 1  # add one\n"
    res = ci.improve_function(_MODULE, "alpha", _ask(reply))
    assert not res["ok"]
    assert "comment-only" in res["reason"]


def test_improve_rejects_a_rewritten_return_contract_change():
    reply = "def beta(value, default):\n    if not value:\n        return None\n    return value\n"
    res = ci.improve_function(_MODULE, "beta", _ask(reply))
    assert not res["ok"]
    assert "contract change" in res["reason"]


def test_improve_rejects_a_defaulted_get_turned_strict():
    reply = (
        "def gamma(schema):\n"
        "    if \"type\" not in schema:\n"
        "        raise KeyError(\"type\")\n"
        "    return schema[\"type\"]\n"
    )
    res = ci.improve_function(_MODULE, "gamma", _ask(reply))
    assert not res["ok"]
    # Either the raise-rewrite or the defaulted-get guard may fire first; both
    # are correct rejections of the same contract change.
    assert "contract" in res["reason"] or "defaulted" in res["reason"]


def test_improve_rejects_a_deletion():
    reply = "def beta(value, default):\n    pass\n"
    res = ci.improve_function(_MODULE, "beta", _ask(reply))
    assert not res["ok"]
    assert "deletion" in res["reason"]


def test_improve_rejects_a_net_new_print():
    reply = "def alpha(x):\n    print(x)\n    return x + 1\n"
    res = ci.improve_function(_MODULE, "alpha", _ask(reply))
    assert not res["ok"]
    assert "print()" in res["reason"]


def test_improve_treats_a_model_error_string_as_failure_not_code():
    res = ci.improve_function(_MODULE, "alpha",
                              _ask("ERROR: no model produced an answer."))
    assert not res["ok"]
    assert "model unavailable" in res["reason"]


def test_improve_reports_a_missing_function():
    res = ci.improve_function(_MODULE, "missing", _ask("whatever"))
    assert not res["ok"]
    assert "no top-level function" in res["reason"]
