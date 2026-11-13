"""detach_owned_application against a legacy module that is still importing."""
from __future__ import annotations

import sys
import threading
from types import ModuleType

import pytest

from sonder_runtime.bootstrap import legacy_root
from sonder_runtime.bootstrap.application_graph import Application


def _application() -> Application:
    # Exact type identity is all detach checks; no providers are needed.
    return object.__new__(Application)


def _legacy(monkeypatch, **attributes):
    module = ModuleType("server")
    for name, value in attributes.items():
        setattr(module, name, value)
    monkeypatch.setitem(sys.modules, "server", module)
    return module


def test_a_legacy_module_still_importing_has_nothing_to_detach(monkeypatch):
    # The owned HTTP runtime's catalog warm-up thread imports ``server``
    # lazily.  A stop that ran while that import was still executing found
    # the module without its _APP_GRAPH_LOCK yet, raised "busy", and the
    # child exited non-zero: STOPPED_UNCLEAN after a clean drain.
    _legacy(monkeypatch)  # partially executed: no lock defined yet
    monkeypatch.setattr(legacy_root, "_owned_application", None)
    assert legacy_root.detach_owned_application(_application()) is False


def test_a_bound_graph_without_a_lock_is_still_refused(monkeypatch):
    application = _application()
    _legacy(monkeypatch)
    monkeypatch.setattr(legacy_root, "_owned_application", application)
    with pytest.raises(RuntimeError, match="inconsistent"):
        legacy_root.detach_owned_application(application)


def test_a_composed_module_detaches_its_bound_graph(monkeypatch):
    application = _application()
    legacy = _legacy(monkeypatch, _APP_GRAPH_LOCK=threading.Lock(), _APP_GRAPH=application)
    monkeypatch.setattr(legacy_root, "_owned_application", application)
    assert legacy_root.detach_owned_application(application) is True
    assert legacy._APP_GRAPH is None and legacy_root._owned_application is None


def test_a_held_composition_lock_is_still_busy(monkeypatch):
    _legacy(monkeypatch, _APP_GRAPH_LOCK=_NeverFree())
    with pytest.raises(RuntimeError, match="busy"):
        legacy_root.detach_owned_application(_application())


class _NeverFree:
    def acquire(self, timeout=-1):
        return False

    def release(self):  # pragma: no cover - never acquired
        raise AssertionError("released a lock that was never acquired")
