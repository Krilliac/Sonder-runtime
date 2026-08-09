"""Typed evaluation-history boundary and compatibility coverage."""
from __future__ import annotations

import importlib

import pytest

from sonder_runtime.adapters import evaluation_history_store
from sonder_runtime.adapters.legacy.evaluation_history import (
    LegacyEvaluationHistoryReader,
)
from sonder_runtime.application.evaluation_history import EvaluationHistoryService

pytestmark = pytest.mark.unit


class _Reader:
    def __init__(self) -> None:
        self.calls = []

    def status(self, **filters):
        self.calls.append(filters)
        return {"schema": "test", "groups": [], "read_only": True}


def test_service_forwards_exact_identity_filters_to_reader_port():
    reader = _Reader()
    service = EvaluationHistoryService(reader)

    result = service.status(
        model="sonder:latest",
        model_digest="a" * 64,
        suite="unit",
        suite_version="3",
        suite_digest="b" * 64,
        tolerance=0.125,
        max_records=42,
    )

    assert result == {"schema": "test", "groups": [], "read_only": True}
    assert reader.calls == [{
        "model": "sonder:latest",
        "model_digest": "a" * 64,
        "suite": "unit",
        "suite_version": "3",
        "suite_digest": "b" * 64,
        "tolerance": 0.125,
        "max_records": 42,
    }]


def test_legacy_reader_resolves_store_lazily(monkeypatch):
    calls = []

    def fake_status(**kwargs):
        calls.append(kwargs)
        return {"groups": []}

    monkeypatch.setattr(evaluation_history_store, "history_status", fake_status)

    assert LegacyEvaluationHistoryReader().status(model="local") == {"groups": []}
    assert calls[0]["model"] == "local"


def test_root_compatibility_module_is_true_store_alias():
    legacy = importlib.import_module("eval_history")

    assert legacy is evaluation_history_store
