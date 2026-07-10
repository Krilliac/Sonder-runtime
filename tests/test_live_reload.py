import importlib
import os
import sys
import time

import live_reload
import pytest


@pytest.fixture(autouse=True)
def _enable_live_reload(monkeypatch):
    """Opt this unit-test module into the feature disabled by the global test sandbox."""
    monkeypatch.setenv("TRILOBITE_LIVE_RELOAD", "1")


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


def test_live_reload_can_be_disabled(monkeypatch):
    monkeypatch.setenv("TRILOBITE_LIVE_RELOAD", "0")
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
        live_reload._ERRORS.pop(module_name, None)


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


def test_serve_rebinds_reloaded_server(monkeypatch):
    import trilobite_serve as ts

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
    import trilobite_repl as repl

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
