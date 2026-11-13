"""The status tools must describe the code the process runs, not the checkout.

A live source refresh re-executes ``server.py`` but leaves other imported
``sonder_runtime`` modules at their import-time code. ``loaded_source`` keeps the
process-lifetime record that lets ``mcp_runtime_status`` and
``runtime_source_update_status`` say so.
"""
import importlib
import importlib.util
import os
import sys

import pytest

import server
from sonder_runtime.adapters import loaded_source
from sonder_runtime.domain import mcp_runtime_formatting as rendering


@pytest.fixture
def clean_baseline():
    """Isolate the module baseline; keep the commit the real server stored."""
    saved_commit = loaded_source._COMMIT[0]
    loaded_source.reset()
    yield
    loaded_source.reset()
    loaded_source._COMMIT[0] = saved_commit


@pytest.fixture
def made_modules():
    names = []
    yield names
    for name in names:
        sys.modules.pop(name, None)


def _make_module(made, name, path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    made.append(name)
    spec.loader.exec_module(module)
    return module


def _rewrite(path, text):
    before = path.stat()
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 5_000_000_000))


# --- observe / stale_modules -------------------------------------------------


def test_only_the_changed_package_module_is_reported(tmp_path, clean_baseline, made_modules):
    x = tmp_path / "sonder_runtime" / "x.py"
    y = tmp_path / "y.py"
    _make_module(made_modules, "ls_test_x", x, "VALUE = 1\n")
    _make_module(made_modules, "ls_test_y", y, "VALUE = 1\n")
    loaded_source.observe(tmp_path)
    assert loaded_source.stale_modules(tmp_path) == []

    _rewrite(x, "VALUE = 2222\n")

    assert loaded_source.stale_modules(tmp_path) == ["ls_test_x"]
    _rewrite(y, "VALUE = 3333\n")
    assert loaded_source.stale_modules(tmp_path) == ["ls_test_x", "ls_test_y"]


def test_venv_and_outside_modules_are_never_reported(tmp_path, clean_baseline, made_modules):
    root = tmp_path / "root"
    site = root / "venv" / "Lib" / "site-packages" / "dep.py"
    outside = tmp_path / "outside" / "other.py"
    _make_module(made_modules, "ls_test_dep", site, "A = 1\n")
    _make_module(made_modules, "ls_test_outside", outside, "A = 1\n")
    loaded_source.observe(root)
    _rewrite(site, "A = 22222\n")
    _rewrite(outside, "A = 22222\n")

    assert loaded_source.stale_modules(root) == []
    assert loaded_source._BASELINE == {}


def test_exclude_filters_at_report_time_even_if_observed_without_it(
    tmp_path, clean_baseline, made_modules
):
    x = tmp_path / "sonder_runtime" / "x.py"
    _make_module(made_modules, "ls_test_x", x, "VALUE = 1\n")
    loaded_source.observe(tmp_path)
    assert "ls_test_x" in loaded_source._BASELINE
    _rewrite(x, "VALUE = 2222\n")

    assert loaded_source.stale_modules(tmp_path) == ["ls_test_x"]
    assert loaded_source.stale_modules(tmp_path, exclude=["ls_test_x"]) == []


def test_a_deleted_source_file_is_reported(tmp_path, clean_baseline, made_modules):
    x = tmp_path / "sonder_runtime" / "x.py"
    _make_module(made_modules, "ls_test_x", x, "VALUE = 1\n")
    loaded_source.observe(tmp_path)
    x.unlink()

    assert loaded_source.stale_modules(tmp_path) == ["ls_test_x"]


def test_known_modules_cost_no_stat_on_a_second_observe(
    tmp_path, clean_baseline, made_modules, monkeypatch
):
    _make_module(made_modules, "ls_test_x", tmp_path / "sonder_runtime" / "x.py", "V = 1\n")
    _make_module(made_modules, "ls_test_y", tmp_path / "y.py", "V = 1\n")
    loaded_source.observe(tmp_path)
    assert {"ls_test_x", "ls_test_y"} <= set(loaded_source._BASELINE)

    calls = []
    real_stat = os.stat

    def counting(path, *args, **kwargs):
        calls.append(str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(loaded_source.os, "stat", counting)
    loaded_source.observe(tmp_path)

    assert calls == []


def test_modules_outside_the_package_cost_nothing_on_a_second_observe(
    tmp_path, clean_baseline, made_modules, monkeypatch
):
    """Most loaded modules are not package code; they must not be examined again."""
    root = tmp_path / "root"
    _make_module(made_modules, "ls_test_outside", tmp_path / "outside" / "o.py", "V = 1\n")
    _make_module(made_modules, "ls_test_x", root / "sonder_runtime" / "x.py", "V = 1\n")
    loaded_source.observe(root)

    examined = []
    real = loaded_source._package_source

    def counting(module, base):
        examined.append(getattr(module, "__name__", "?"))
        return real(module, base)

    monkeypatch.setattr(loaded_source, "_package_source", counting)
    loaded_source.observe(root)
    assert examined == []

    _make_module(made_modules, "ls_test_new", root / "sonder_runtime" / "n.py", "V = 1\n")
    loaded_source.observe(root)
    assert examined == ["ls_test_new"]


def test_a_file_refreshed_by_other_means_is_excluded_under_every_name(
    tmp_path, clean_baseline, made_modules
):
    """server.py can be loaded as ``__main__`` and as ``server``; neither is stale."""
    entry = tmp_path / "entry.py"
    x = tmp_path / "sonder_runtime" / "x.py"
    _make_module(made_modules, "ls_test_entry_main", entry, "V = 1\n")
    _make_module(made_modules, "ls_test_entry_again", entry, "V = 1\n")
    _make_module(made_modules, "ls_test_x", x, "V = 1\n")
    loaded_source.observe(tmp_path)
    _rewrite(entry, "V = 22222\n")
    _rewrite(x, "V = 22222\n")

    assert loaded_source.stale_modules(tmp_path) == [
        "ls_test_entry_again", "ls_test_entry_main", "ls_test_x"]
    assert loaded_source.stale_modules(tmp_path, exclude_files=[entry]) == ["ls_test_x"]


def test_another_source_root_starts_a_new_record(tmp_path, clean_baseline, made_modules):
    first, second = tmp_path / "first", tmp_path / "second"
    x = second / "sonder_runtime" / "x.py"
    _make_module(made_modules, "ls_test_x", x, "V = 1\n")
    loaded_source.observe(first)
    assert loaded_source._BASELINE == {}

    loaded_source.observe(second)
    assert "ls_test_x" in loaded_source._BASELINE
    _rewrite(x, "V = 22222\n")
    assert loaded_source.stale_modules(second) == ["ls_test_x"]


def test_a_replaced_module_object_is_rebaselined_not_reported(
    tmp_path, clean_baseline, made_modules
):
    x = tmp_path / "sonder_runtime" / "x.py"
    _make_module(made_modules, "ls_test_x", x, "VALUE = 1\n")
    loaded_source.observe(tmp_path)
    sys.modules.pop("ls_test_x")
    _rewrite(x, "VALUE = 2222\n")
    _make_module(made_modules, "ls_test_x", x, "VALUE = 2222\n")

    assert loaded_source.stale_modules(tmp_path) == []


def test_modules_without_a_source_file_are_ignored(tmp_path, clean_baseline, made_modules):
    import types

    bare = types.ModuleType("ls_test_bare")
    sys.modules["ls_test_bare"] = bare
    made_modules.append("ls_test_bare")

    loaded_source.observe(tmp_path)

    assert loaded_source.stale_modules(tmp_path) == []


# --- running_commit ----------------------------------------------------------


def test_running_commit_computes_once_and_keeps_the_first_value(clean_baseline):
    calls = []

    def first():
        calls.append("first")
        return "aaa"

    assert loaded_source.running_commit(first) == "aaa"
    assert loaded_source.running_commit(first) == "aaa"
    assert loaded_source.running_commit(lambda: "bbb") == "aaa"
    assert calls == ["first"]


def test_a_raising_compute_stores_none_and_does_not_raise(clean_baseline):
    def boom():
        raise RuntimeError("no git")

    assert loaded_source.running_commit(boom) is None
    assert loaded_source.running_commit(lambda: "late") is None


# --- formatter ---------------------------------------------------------------


def _state(**overrides):
    data = {
        "status": "current", "enabled": True, "registered_tools": 208, "refresh_count": 4,
        "last_surface_changed": True, "protocol_list_changed": True, "path": "/srv/server.py",
        "loaded_digest": "abcdef0123456789ff", "current_digest": "abcdef0123456789ff",
    }
    data.update(overrides)
    return data


_BASE_LINES = [
    "sonder MCP runtime",
    "  status: current | live source refresh: on",
    "  tools: 208 | atomic refreshes: 4 | last surface changed: yes",
    "  MCP tool-list updates: advertised",
    "  source registration: available",
    "  loaded/current: abcdef012345 / abcdef012345",
]
_ACTION = (
    "  ACTION: restart the Sonder MCP server; live source refresh reloads "
    "server.py and the watched helper modules only"
)


@pytest.mark.parametrize("empty", [None, [], ()])
def test_no_stale_modules_leaves_the_output_unchanged(empty):
    data = _state() if empty is None else _state(stale_modules=empty)
    text = rendering.format_mcp_runtime(data, recovery_action=lambda _p: "")
    assert text.splitlines() == _BASE_LINES


def test_three_stale_modules_are_listed_sorted_and_status_flips():
    data = _state(stale_modules=["c.mod", "a.mod", "b.mod"])
    lines = rendering.format_mcp_runtime(data, recovery_action=lambda _p: "").splitlines()
    assert lines == [
        "sonder MCP runtime",
        "  status: restart required | live source refresh: on",
        *_BASE_LINES[2:],
        "  package code: 3 loaded module(s) changed on disk after this process loaded "
        "them (a.mod, b.mod, c.mod)",
        _ACTION,
    ]


def test_five_stale_modules_are_truncated_with_a_count():
    data = _state(stale_modules=["e", "d", "c", "b", "a"])
    lines = rendering.format_mcp_runtime(data, recovery_action=lambda _p: "").splitlines()
    assert lines[6] == (
        "  package code: 5 loaded module(s) changed on disk after this process loaded "
        "them (a, b, c, +2 more)"
    )
    assert lines[7] == _ACTION
    assert lines[1] == "  status: restart required | live source refresh: on"


def test_other_statuses_are_not_rewritten_by_the_formatter():
    data = _state(status="error", stale_modules=["a"])
    text = rendering.format_mcp_runtime(data, recovery_action=lambda _p: "")
    assert "  status: error | live source refresh: on" in text


# --- reloadable_mcp ----------------------------------------------------------


_SAMPLE = '''from reloadable_mcp import ReloadableMCPServer

existing = globals().get("_PERSISTENT_MCP")
if isinstance(existing, ReloadableMCPServer):
    mcp = existing
    mcp.begin_module_refresh()
else:
    mcp = ReloadableMCPServer("sample")
_PERSISTENT_MCP = mcp

@mcp.tool()
def alpha() -> str:
    return "{version}"

mcp.finish_module_refresh(__name__, __file__, globals())
'''


@pytest.fixture
def live_mcp(tmp_path, monkeypatch, clean_baseline, made_modules):
    monkeypatch.setenv("SONDER_LIVE_RELOAD", "1")
    monkeypatch.setenv("SONDER_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.syspath_prepend(str(tmp_path))
    name = "ls_test_server_sample"
    path = tmp_path / (name + ".py")
    path.write_text(_SAMPLE.format(version="one"), encoding="utf-8")
    made_modules.append(name)
    mcp = importlib.import_module(name).mcp
    package = tmp_path / "sonder_runtime" / "pkg.py"
    _make_module(made_modules, "ls_test_pkg", package, "VALUE = 1\n")
    assert mcp.refresh_if_changed() == {"reloaded": False, "surface_changed": False}
    return mcp, path, package


def test_a_stale_package_module_makes_the_status_restart_required(live_mcp):
    mcp, _path, package = live_mcp
    assert mcp.runtime_snapshot()["status"] == "current"
    assert mcp.runtime_snapshot()["stale_modules"] == []

    _rewrite(package, "VALUE = 22222\n")
    state = mcp.runtime_snapshot()

    assert state["status"] == "restart required"
    assert state["stale_modules"] == ["ls_test_pkg"]
    text = rendering.format_mcp_runtime(state, recovery_action=lambda _p: "")
    assert "  status: restart required | live source refresh: on" in text
    assert "ls_test_pkg" in text


def test_exclude_modules_removes_watched_modules_from_the_snapshot(live_mcp):
    mcp, _path, package = live_mcp
    _rewrite(package, "VALUE = 22222\n")

    state = mcp.runtime_snapshot(exclude_modules=["ls_test_pkg"])

    assert state["stale_modules"] == []
    assert state["status"] == "current"


def test_error_refresh_pending_and_disabled_keep_their_precedence(live_mcp, monkeypatch):
    mcp, path, package = live_mcp
    _rewrite(package, "VALUE = 22222\n")

    mcp._last_error = "boom"
    assert mcp.runtime_snapshot()["status"] == "error"
    mcp._last_error = ""

    _rewrite(path, _SAMPLE.format(version="twotwo"))
    assert mcp.runtime_snapshot()["status"] == "refresh pending"

    monkeypatch.setenv("SONDER_LIVE_RELOAD", "0")
    assert mcp.runtime_snapshot()["status"] == "disabled (pending edit ignored)"


def test_disabled_without_a_pending_edit_still_reads_disabled(live_mcp, monkeypatch):
    mcp, _path, package = live_mcp
    _rewrite(package, "VALUE = 22222\n")
    monkeypatch.setenv("SONDER_LIVE_RELOAD", "0")

    assert mcp.runtime_snapshot()["status"] == "disabled"


def test_a_server_without_a_registered_source_reports_no_stale_modules(clean_baseline):
    from reloadable_mcp import ReloadableMCPServer

    bare = ReloadableMCPServer("bare")
    assert bare.runtime_snapshot()["stale_modules"] == []


# --- server wiring -----------------------------------------------------------


def test_server_takes_its_running_commit_from_the_process_lifetime_adapter():
    assert server._loaded_source is loaded_source
    stored = loaded_source._COMMIT[0]
    assert stored is not loaded_source._UNSET
    assert server.RUNNING_SOURCE_COMMIT == stored

    # A live refresh re-executes server.py's module-level line: simulate it.
    assert loaded_source.running_commit(lambda: "newcommit") == stored
    assert server.RUNNING_SOURCE_COMMIT == stored


def test_a_fast_forward_under_a_running_process_requires_restart(monkeypatch):
    monkeypatch.setattr(server, "RUNNING_SOURCE_COMMIT", "oldcommit0000")
    monkeypatch.setattr(
        server.git_tools,
        "runtime_update_status",
        lambda root, refresh=True: {"installed_commit": "newcommit1111"},
    )

    data = server.runtime_source_update_status_data(refresh=False)

    assert data["running_commit"] == "oldcommit0000"
    assert data["restart_required"] is True


def test_mcp_runtime_data_passes_the_watched_modules_as_exclusions(monkeypatch):
    seen = {}

    def fake(exclude_modules=()):
        seen["exclude"] = list(exclude_modules)
        return {"stale_modules": []}

    monkeypatch.setattr(server.mcp, "runtime_snapshot", fake)
    server.mcp_runtime_data()

    assert seen["exclude"] == list(server.LIVE_RELOAD_MODULES)


def test_the_adapter_is_not_a_live_reload_module():
    assert "sonder_runtime.adapters.loaded_source" not in server.LIVE_RELOAD_MODULES
