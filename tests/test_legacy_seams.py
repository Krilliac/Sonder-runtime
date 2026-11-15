"""``late`` and ``value`` resolve at call time; the shim ratchet pins a name set."""
from __future__ import annotations

import pytest

from sonder_runtime.bootstrap.legacy_seams import LegacySeamNameError, late, value


# ---- late / value --------------------------------------------------------------

def test_late_sees_a_namespace_patch_made_after_construction():
    namespace = {"ask": lambda text: "real:" + text}
    ask = late(namespace, "ask")
    assert ask("a") == "real:a"

    namespace["ask"] = lambda text: "patched:" + text  # what monkeypatch.setattr(server, ...) does
    assert ask("a") == "patched:a"


def test_late_forwards_positional_and_keyword_arguments_and_the_result():
    seen = []

    def target(*args, **kwargs):
        seen.append((args, kwargs))
        return "done"

    assert late({"target": target}, "target")(1, 2, key="v") == "done"
    assert seen == [((1, 2), {"key": "v"})]


def test_late_may_be_built_before_the_name_exists():
    namespace: dict = {}
    ask = late(namespace, "ask")
    namespace["ask"] = lambda: "defined later"
    assert ask() == "defined later"


def test_value_sees_a_rebound_name_after_construction():
    namespace = {"TIERS": {"a": 1}}
    tiers = value(namespace, "TIERS")
    assert tiers() == {"a": 1}

    namespace["TIERS"] = {"b": 2}  # a ``global`` rebind in server.py
    assert tiers() == {"b": 2}


def test_value_returns_the_same_object_not_a_copy():
    holder = {"k": 1}
    assert value({"holder": holder}, "holder")() is holder


def test_late_with_a_missing_name_raises_a_key_error_naming_it():
    ask = late({}, "no_such_function")
    with pytest.raises(KeyError) as caught:
        ask()
    assert isinstance(caught.value, LegacySeamNameError)
    assert caught.value.name == "no_such_function"
    assert "no_such_function" in str(caught.value)


def test_value_with_a_missing_name_raises_a_key_error_naming_it():
    with pytest.raises(KeyError, match="no_such_state") as caught:
        value({}, "no_such_state")()
    assert isinstance(caught.value, LegacySeamNameError)


def test_a_key_error_raised_by_the_target_is_not_relabelled():
    def target():
        raise KeyError("inner")

    with pytest.raises(KeyError) as caught:
        late({"target": target}, "target")()
    assert not isinstance(caught.value, LegacySeamNameError)
    assert caught.value.args == ("inner",)


def test_late_reads_a_real_module_namespace_through_globals_style_mapping():
    import types

    module = types.ModuleType("fake_server")
    module.sonder = lambda prompt: "one:" + prompt
    ask = late(vars(module), "sonder")
    assert ask("x") == "one:x"
    module.sonder = lambda prompt: "two:" + prompt
    assert ask("x") == "two:x"
