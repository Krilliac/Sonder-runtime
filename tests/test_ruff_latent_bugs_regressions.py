"""Regressions for latent defects that ruff flagged in the runtime package."""
from __future__ import annotations

import builtins
import collections
import typing

import pytest


def test_check_config_skip_callable_survives_exception_unbinding(monkeypatch):
    from sonder_runtime.bootstrap import config_loading

    real_import = builtins.__import__

    def failing_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "sonder_runtime.platform" and "config" in (fromlist or ()):
            raise ImportError("simulated config import failure")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", failing_import)
    check = config_loading.check_config()
    monkeypatch.setattr(builtins, "__import__", real_import)

    result = check()
    assert result == {
        "status": "skipped",
        "detail": "sonder_config unavailable (simulated config import failure)",
    }


def test_ports_package_exports_each_name_once_and_unambiguously():
    from sonder_runtime.application import ports
    from sonder_runtime.application.ports import execution_world, specialized_lifecycle

    duplicates = [
        name for name, count in collections.Counter(ports.__all__).items() if count > 1
    ]
    assert duplicates == []
    assert ports.CleanupResult is specialized_lifecycle.CleanupResult
    assert ports.ExecutionWorldCleanupResult is execution_world.CleanupResult
    assert "ExecutionWorldCleanupResult" in ports.__all__


@pytest.mark.parametrize(
    "target",
    [
        "sonder_runtime.adapters.execution.process_jobs:SubprocessJobProvider.__init__",
        "sonder_runtime.application.session.query_export:_redact",
    ],
)
def test_annotations_resolve_at_runtime(target):
    import importlib

    module_name, _, qualname = target.partition(":")
    obj = importlib.import_module(module_name)
    for part in qualname.split("."):
        obj = getattr(obj, part)
    hints = typing.get_type_hints(obj)
    assert hints


def test_compaction_star_import_exports_only_string_attributes():
    import sonder_runtime.application.compaction as compaction

    namespace: dict[str, object] = {}
    exec("from sonder_runtime.application.compaction import *", namespace)
    for name in compaction.__all__:
        assert isinstance(name, str)
        assert hasattr(compaction, name)
        assert name in namespace


def test_weather_forecast_rows_use_their_own_day_values():
    from sonder_runtime.adapters.weather import format_weather

    text = format_weather({
        "query": "x",
        "forecast": {
            "daily": {
                "time": ["2026-01-01", "2026-01-02"],
                "temperature_2m_max": [1, 2],
                "temperature_2m_min": [-1],
            },
        },
    })
    assert "- 2026-01-01: " in text and "high 1, low -1" in text
    assert "- 2026-01-02: " in text and "high 2, low ?" in text
