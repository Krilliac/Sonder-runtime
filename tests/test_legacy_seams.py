"""``late`` and ``value`` resolve at call time; the shim ratchet pins a name set."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

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


# ---- shim name-set ratchet ------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent

SERVER_WITH_SHIMS = '''
from sonder_runtime.application.evaluation import campaigns as _campaigns
from sonder_runtime.application.tasks import use_cases as _use_cases
from sonder_runtime.domain.corpus import TASKS

REEXPORTED = _campaigns.helper
TASKS_ALIAS = _campaigns


def alpha(text):
    """Docstring is allowed."""
    return _campaigns.run(text, deps=_DEPS)


def beta(text):
    return _use_cases.show(text, deps=_DEPS)


def plain_delegate(text):
    return _campaigns.run(text)


def not_thin(text):
    value = text.strip()
    return _campaigns.run(value, deps=_DEPS)
'''


def _load_gate():
    spec = importlib.util.spec_from_file_location(
        "check_lint_ratchet_shims_under_test", REPO_ROOT / "scripts" / "check_lint_ratchet.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gate(tmp_path, monkeypatch):
    module = _load_gate()
    (tmp_path / "server.py").write_text(SERVER_WITH_SHIMS, encoding="utf-8")
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "BASELINE_PATH", tmp_path / "lint_baseline.json")
    monkeypatch.setattr(module, "SIZE_RATCHET_MODULES", ("server.py",))
    monkeypatch.setattr(module, "SERVER_SOURCE", "server.py")
    monkeypatch.setattr(module, "_ruff", lambda select, *, exclude_tests: [])
    return module


def _baseline(tmp_path):
    return json.loads((tmp_path / "lint_baseline.json").read_text(encoding="utf-8"))


def _write_listed(tmp_path, names):
    payload = {"lint": {}, "module_lines": {"server.py": 10_000}, "server_shims": list(names)}
    (tmp_path / "lint_baseline.json").write_text(json.dumps(payload), encoding="utf-8")


def test_only_a_deps_forwarding_wrapper_is_a_shim(gate):
    import ast

    tree = ast.parse(SERVER_WITH_SHIMS)
    assert gate.find_server_shims(tree) == {"alpha", "beta"}
    assert gate.find_server_reexports(tree) >= {"REEXPORTED", "TASKS_ALIAS", "TASKS"}
    assert "plain_delegate" not in gate.find_server_shims(tree)
    assert "not_thin" not in gate.find_server_shims(tree)


def test_the_repository_baseline_starts_empty_and_the_current_server_has_no_shim():
    data = json.loads((REPO_ROOT / "scripts" / "lint_baseline.json").read_text(encoding="utf-8"))
    assert data["server_shims"] == []
    real = _load_gate()
    wrappers, _reexports = real._server_shims()
    assert wrappers == set()
    # The detector is not vacuous on the real file: it sees plenty of packaged names.
    assert len(_reexports) > 50


def test_an_unlisted_thin_wrapper_shim_fails(gate, tmp_path, capsys):
    _write_listed(tmp_path, ["alpha"])
    assert gate.main([]) == 1
    out = capsys.readouterr().out
    assert "server shim beta: thin wrapper of packaged code is not in server_shims" in out
    assert "alpha" not in out


def test_all_wrappers_listed_passes(gate, tmp_path):
    _write_listed(tmp_path, ["alpha", "beta"])
    assert gate.main([]) == 0


def test_replacing_a_baseline_name_with_a_different_name_at_equal_count_fails(gate, tmp_path, capsys):
    _write_listed(tmp_path, ["alpha", "beta"])
    assert gate.main([]) == 0
    # Same count (two names), one protected name swapped for one that is not a shim.
    _write_listed(tmp_path, ["alpha", "gamma"])
    assert gate.main([]) == 1
    out = capsys.readouterr().out
    assert "server shim beta: thin wrapper" in out          # beta became unlisted
    assert "server shim gamma: listed in server_shims" in out  # gamma is not a shim


def test_a_listed_name_that_stopped_being_a_wrapper_fails_and_update_prunes_it(gate, tmp_path, capsys):
    _write_listed(tmp_path, ["alpha", "beta", "retired"])
    assert gate.main([]) == 1
    assert "server shim retired: listed in server_shims" in capsys.readouterr().out
    assert gate.main(["--update"]) == 0
    assert _baseline(tmp_path)["server_shims"] == ["alpha", "beta"]
    assert gate.main([]) == 0


def test_update_never_adds_a_shim(gate, tmp_path, capsys):
    _write_listed(tmp_path, ["alpha"])
    assert gate.main(["--update"]) == 0
    assert _baseline(tmp_path)["server_shims"] == ["alpha"]
    assert gate.main([]) == 1
    assert "server shim beta" in capsys.readouterr().out


def test_add_shim_lists_a_real_wrapper_and_refuses_anything_else(gate, tmp_path, capsys):
    _write_listed(tmp_path, [])
    assert gate.main(["--add-shim", "not_thin"]) == 1
    assert gate.main(["--add-shim", "missing_name"]) == 1
    assert _baseline(tmp_path)["server_shims"] == []
    assert gate.main(["--add-shim", "alpha", "--add-shim", "beta"]) == 0
    assert _baseline(tmp_path)["server_shims"] == ["alpha", "beta"]
    assert gate.main([]) == 0


def test_a_listed_reexport_counts_as_still_a_shim(gate, tmp_path):
    _write_listed(tmp_path, ["alpha", "beta", "REEXPORTED"])
    assert gate.main([]) == 0


def test_a_malformed_server_shims_entry_is_a_tool_failure(gate, tmp_path):
    payload = {"lint": {}, "module_lines": {}, "server_shims": "alpha"}
    (tmp_path / "lint_baseline.json").write_text(json.dumps(payload), encoding="utf-8")
    assert gate.main([]) == 2
