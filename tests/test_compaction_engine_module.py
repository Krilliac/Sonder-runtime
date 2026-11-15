"""The compaction engine is a real module of the compaction package."""
from __future__ import annotations

import importlib

import pytest

pytestmark = pytest.mark.unit

ENGINE_MODULE = "sonder_runtime.application.compaction.engine"
ENGINE_NAMES = (
    "CompactionApplicationService",
    "DeterministicCompactionEngine",
    "CompactionEngine",
    "canonical_summary",
)


def test_engine_is_importable_by_its_dotted_name():
    engine = importlib.import_module(ENGINE_MODULE)
    assert tuple(engine.__all__) == ENGINE_NAMES
    assert engine.CompactionApplicationService.__module__ == ENGINE_MODULE


def test_package_and_legacy_path_expose_the_same_objects():
    engine = importlib.import_module(ENGINE_MODULE)
    package = importlib.import_module("sonder_runtime.application.compaction")
    legacy = importlib.import_module("sonder_runtime.application.compaction.legacy")
    assert tuple(legacy.__all__) == ENGINE_NAMES
    for name in ENGINE_NAMES:
        assert getattr(package, name) is getattr(engine, name)
        assert getattr(legacy, name) is getattr(engine, name)
        assert name in package.__all__


def test_no_synthetic_loader_module_is_registered():
    import sys

    importlib.import_module("sonder_runtime.application.compaction")
    assert "sonder_runtime.application._compaction_legacy" not in sys.modules
