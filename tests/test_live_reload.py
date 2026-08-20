import importlib
import os
import sys
import time

import live_reload
import pytest


@pytest.fixture(autouse=True)
def _enable_live_reload(monkeypatch):
    """Opt this unit-test module into the feature disabled by the global test sandbox."""
    monkeypatch.setenv("SONDER_LIVE_RELOAD", "1")


def test_reload_changed_modules_reloads_source_edit(monkeypatch, tmp_path):
    module_name = "live_reload_sample_mod"
    module_path = tmp_path / (module_name + ".py")
    module_path.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        mod = importlib.import_module(module_name)
        assert mod.VALUE == 1

        live_reload.reload_changed_modules([module_name])
        module_path.write_text("VALUE = 22\n", encoding="utf-8")
        future = time.time() + 2
        os.utime(module_path, (future, future))

        reloaded = live_reload.reload_changed_modules([module_name])[module_name]
        assert reloaded.VALUE == 22
    finally:
        sys.modules.pop(module_name, None)
        live_reload._MTIMES.pop(module_name, None)
        live_reload._SIGNATURES.pop(module_name, None)


def test_prime_closes_first_request_after_edit_window(monkeypatch, tmp_path):
    module_name = "live_reload_primed_mod"
    module_path = tmp_path / (module_name + ".py")
    module_path.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        importlib.import_module(module_name)
        live_reload.prime_modules([module_name])

        module_path.write_text("VALUE = 222\n", encoding="utf-8")
        # A Git operation can restore an older mtime.  The source signature,
        # rather than monotonic-mtime ordering, must still detect the edit.
        past = time.time() - 60
        os.utime(module_path, (past, past))

        reloaded = live_reload.reload_changed_modules([module_name])[module_name]
        assert reloaded.VALUE == 222
    finally:
        sys.modules.pop(module_name, None)
        live_reload._MTIMES.pop(module_name, None)
        live_reload._SIGNATURES.pop(module_name, None)


def test_live_reload_can_be_disabled(monkeypatch):
    monkeypatch.setenv("SONDER_LIVE_RELOAD", "0")
    assert live_reload.reload_changed_modules(["definitely_missing_mod"]) == {}


def test_reload_failure_keeps_old_module(monkeypatch, tmp_path):
    module_name = "live_reload_bad_edit_mod"
    module_path = tmp_path / (module_name + ".py")
    module_path.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        mod = importlib.import_module(module_name)
        live_reload.reload_changed_modules([module_name])

        module_path.write_text("VALUE = \n", encoding="utf-8")
        future = time.time() + 2
        os.utime(module_path, (future, future))

        kept = live_reload.reload_changed_modules([module_name])[module_name]
        assert kept is mod
        assert kept.VALUE == 1
        assert live_reload.snapshot([module_name])[0]["error"].startswith("SyntaxError")
    finally:
        sys.modules.pop(module_name, None)
        live_reload._MTIMES.pop(module_name, None)
        live_reload._SIGNATURES.pop(module_name, None)
        live_reload._ERRORS.pop(module_name, None)


def test_reload_removal_drops_stale_module_symbols(monkeypatch, tmp_path):
    """A security removal must not leave the retired callable live in memory."""
    module_name = "live_reload_removed_symbol_mod"
    module_path = tmp_path / (module_name + ".py")
    module_path.write_text("KEEP = 1\nRETIRED_CHECK = lambda: 'old'\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        old = importlib.import_module(module_name)
        live_reload.reload_changed_modules([module_name])

        module_path.write_text("KEEP = 2\n", encoding="utf-8")
        future = time.time() + 2
        os.utime(module_path, (future, future))

        refreshed = live_reload.reload_changed_modules([module_name])[module_name]
        assert refreshed is not old
        assert refreshed.KEEP == 2
        assert not hasattr(refreshed, "RETIRED_CHECK")
        assert sys.modules[module_name] is refreshed
    finally:
        sys.modules.pop(module_name, None)
        live_reload._MTIMES.pop(module_name, None)
        live_reload._SIGNATURES.pop(module_name, None)
        live_reload._ERRORS.pop(module_name, None)


def test_reload_preserves_only_explicitly_guarded_private_state(monkeypatch, tmp_path):
    module_name = "live_reload_preserved_state_mod"
    module_path = tmp_path / (module_name + ".py")
    module_path.write_text(
        'if "_STATE" not in globals():\n    _STATE = []\nVALUE = 1\n',
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        original = importlib.import_module(module_name)
        original._STATE.append("kept")
        live_reload.reload_changed_modules([module_name])

        module_path.write_text(
            'if "_STATE" not in globals():\n    _STATE = []\nVALUE = 2\n',
            encoding="utf-8",
        )
        future = time.time() + 2
        os.utime(module_path, (future, future))

        refreshed = live_reload.reload_changed_modules([module_name])[module_name]
        assert refreshed.VALUE == 2
        assert refreshed._STATE is original._STATE
        assert refreshed._STATE == ["kept"]
    finally:
        sys.modules.pop(module_name, None)
        live_reload._MTIMES.pop(module_name, None)
        live_reload._SIGNATURES.pop(module_name, None)
        live_reload._ERRORS.pop(module_name, None)


def test_reload_rebinds_existing_importer_module_references(monkeypatch, tmp_path):
    dependency_name = "live_reload_imported_dependency"
    importer_name = "live_reload_existing_importer"
    dependency_path = tmp_path / (dependency_name + ".py")
    dependency_path.write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / (importer_name + ".py")).write_text(
        "import %s\n" % dependency_name, encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        dependency = importlib.import_module(dependency_name)
        importer = importlib.import_module(importer_name)
        assert getattr(importer, dependency_name) is dependency
        live_reload.reload_changed_modules([dependency_name])

        dependency_path.write_text("VALUE = 2\n", encoding="utf-8")
        future = time.time() + 2
        os.utime(dependency_path, (future, future))

        refreshed = live_reload.reload_changed_modules([dependency_name])[dependency_name]
        assert getattr(importer, dependency_name) is refreshed
        assert getattr(importer, dependency_name).VALUE == 2
    finally:
        sys.modules.pop(importer_name, None)
        sys.modules.pop(dependency_name, None)
        live_reload._MTIMES.pop(dependency_name, None)
        live_reload._SIGNATURES.pop(dependency_name, None)
        live_reload._ERRORS.pop(dependency_name, None)


def test_server_rebinds_reloaded_modules(monkeypatch):
    import server

    original = server.personas
    replacement = object()
    monkeypatch.setattr(
        server.live_reload,
        "reload_changed_modules",
        lambda names: {"personas": replacement},
    )
    try:
        server._maybe_live_reload()
        assert server.personas is replacement
    finally:
        server.personas = original


def test_workflow_live_reload_stays_behind_package_adapter(monkeypatch, tmp_path):
    import server
    from sonder_runtime.adapters.filesystem import workflow_store as packaged

    monkeypatch.setattr(packaged, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("SONDER_WORKFLOWS", raising=False)
    monkeypatch.setattr(
        server.live_reload,
        "reload_changed_modules",
        lambda names: {"workflow_store": packaged},
    )

    server._maybe_live_reload()
    assert "status_sweep" in server.workflow_list()
    assert "workflow_store" not in server.__dict__


def test_server_rebinds_log_inspect_alias_without_replacing_tool(monkeypatch):
    import server

    original_module = server.log_inspect_module
    original_tool = server.log_inspect
    replacement = object()
    monkeypatch.setattr(
        server.live_reload,
        "reload_changed_modules",
        lambda names: {"log_inspect": replacement},
    )
    try:
        server._maybe_live_reload()
        assert server.log_inspect_module is replacement
        assert server.log_inspect is original_tool
        assert callable(server.log_inspect)
    finally:
        server.log_inspect_module = original_module
        server.log_inspect = original_tool


def test_server_rebinds_capability_aliases_without_replacing_tools(monkeypatch):
    import server

    replacements = {
        "local_service_probe": object(),
        "dependency_inventory": object(),
        "json_patch_tool": object(),
    }
    originals = {
        "local_probe": server.local_probe,
        "dependency_inventory_tool": server.dependency_inventory_tool,
        "json_patch_tool": server.json_patch_tool,
    }
    wrappers = {
        "local_service_probe": server.local_service_probe,
        "dependency_inventory": server.dependency_inventory,
        "json_patch": server.json_patch,
    }
    monkeypatch.setattr(
        server.live_reload,
        "reload_changed_modules",
        lambda names: replacements,
    )
    try:
        server._maybe_live_reload()
        assert server.local_probe is replacements["local_service_probe"]
        assert server.dependency_inventory_tool is replacements["dependency_inventory"]
        assert server.json_patch_tool is replacements["json_patch_tool"]
        assert server.local_service_probe is wrappers["local_service_probe"]
        assert server.dependency_inventory is wrappers["dependency_inventory"]
        assert server.json_patch is wrappers["json_patch"]
        assert all(callable(value) for value in wrappers.values())
        assert {
            "local_service_probe", "dependency_inventory", "json_patch_tool",
        }.issubset(server.LIVE_RELOAD_MODULES)
    finally:
        server.local_probe = originals["local_probe"]
        server.dependency_inventory_tool = originals["dependency_inventory_tool"]
        server.json_patch_tool = originals["json_patch_tool"]


def test_server_prime_live_reload_upgrades_legacy_helper(monkeypatch):
    import server

    legacy = object()
    primed = []

    class Refreshed:
        @staticmethod
        def prime_modules(names):
            primed.append(list(names))

    monkeypatch.setattr(server, "live_reload", legacy)
    monkeypatch.setattr(server.importlib, "reload", lambda module: Refreshed)

    assert server._prime_live_reload_modules() is True
    assert server.live_reload is Refreshed
    assert primed == [server.LIVE_RELOAD_MODULES]


def test_serve_rebinds_reloaded_server(monkeypatch):
    import sonder_runtime.interfaces.http.serve as ts

    original = ts.server
    replacement = object()
    monkeypatch.setattr(
        ts.live_reload,
        "reload_changed_modules",
        lambda names: {"server": replacement},
    )
    try:
        ts._maybe_live_reload()
        assert ts.server is replacement
    finally:
        ts.server = original


def test_repl_rebinds_reloaded_personas(monkeypatch):
    import sonder_runtime.interfaces.repl.repl as repl

    original = repl.personas
    replacement = object()
    monkeypatch.setattr(
        repl.live_reload,
        "reload_changed_modules",
        lambda names: {"personas": replacement},
    )
    try:
        repl._maybe_live_reload()
        assert repl.personas is replacement
    finally:
        repl.personas = original


def test_unchanged_module_is_not_reported_as_changed(monkeypatch, tmp_path):
    """reload_changed_modules must return only names that actually reloaded.

    The `signature == old_signature` branch used to add the module to the
    result even though nothing changed, so every caller rebound every watched
    module on every request. Harmless (rebinding to itself) but the returned
    set contradicted the function's name and docstring.
    """
    module_name = "live_reload_unchanged_mod"
    module_path = tmp_path / (module_name + ".py")
    module_path.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        importlib.import_module(module_name)
        # First call primes the signature; second call, with no source edit,
        # must report nothing.
        live_reload.reload_changed_modules([module_name])
        second = live_reload.reload_changed_modules([module_name])
        assert module_name not in second, (
            "an unchanged module was reported as reloaded"
        )
    finally:
        sys.modules.pop(module_name, None)
        live_reload._MTIMES.pop(module_name, None)
        live_reload._SIGNATURES.pop(module_name, None)


def test_server_live_reload_rebinds_task_application_modules(monkeypatch):
    import server

    service_module = object()
    adapter_module = object()
    monkeypatch.setattr(server, "task_use_cases", server.task_use_cases)
    monkeypatch.setattr(server, "task_state_adapter", server.task_state_adapter)
    monkeypatch.setattr(server, "_refresh_runtime_policy", lambda **kwargs: None)
    monkeypatch.setattr(
        server.live_reload,
        "reload_changed_modules",
        lambda names: {
            "sonder_runtime.application.tasks.use_cases": service_module,
            "sonder_runtime.adapters.task_store": adapter_module,
        },
    )

    server._maybe_live_reload()

    assert server.task_use_cases is service_module
    assert server.task_state_adapter is adapter_module


def test_server_watches_package_owned_evaluation_history_store():
    import server

    assert (
        "sonder_runtime.adapters.evaluation_history_store"
        in server.LIVE_RELOAD_MODULES
    )
    assert "eval_history" not in server.LIVE_RELOAD_MODULES


def test_server_live_reload_rebinds_evaluation_history_modules(monkeypatch):
    import server

    service_module = object()
    adapter_module = object()
    # _maybe_live_reload intentionally rebinds these server globals. Register
    # their originals with monkeypatch so this test cannot poison later tests
    # in the same interpreter.
    monkeypatch.setattr(
        server, "eval_history_use_cases", server.eval_history_use_cases
    )
    monkeypatch.setattr(
        server, "eval_history_adapter", server.eval_history_adapter
    )
    monkeypatch.setattr(
        server.live_reload,
        "reload_changed_modules",
        lambda names: {
            "sonder_runtime.application.evaluation_history.use_cases": (
                service_module
            ),
            "sonder_runtime.adapters.eval_history_reader": adapter_module,
        },
    )
    monkeypatch.setattr(server, "_refresh_runtime_policy", lambda **kwargs: None)

    server._maybe_live_reload()

    assert server.eval_history_use_cases is service_module
    assert server.eval_history_adapter is adapter_module


def test_server_reloads_authoritative_reward_rules(monkeypatch):
    import server

    original = server.reward_rules
    replacement = object()
    monkeypatch.setattr(
        server.live_reload,
        "reload_changed_modules",
        lambda names: {"sonder_runtime.domain.memory.rules": replacement},
    )
    try:
        server._maybe_live_reload()
        assert server.reward_rules is replacement
        assert "sonder_runtime.domain.memory.rules" in server.LIVE_RELOAD_MODULES
        assert "reward" not in server.LIVE_RELOAD_MODULES
    finally:
        server.reward_rules = original
