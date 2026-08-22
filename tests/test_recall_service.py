"""Typed semantic-recall seam and compatibility behavior."""
from __future__ import annotations

import inspect
import sqlite3
import sys
from types import SimpleNamespace

import pytest

import server
from sonder_runtime.adapters import recall as recall_adapter
from sonder_runtime.adapters.recall_gateway import LegacyRecallGateway
from sonder_runtime.application.recall import RecallService
from sonder_runtime.domain.common.errors import DependencyUnavailable, InvalidInput


class CapturingGateway:
    def __init__(self):
        self.calls = []

    def recall(self, connection, task, **options):
        self.calls.append((connection, task, options))
        return ["task -> exact result"]


def test_service_forwards_the_complete_typed_contract_without_reformatting():
    gateway = CapturingGateway()
    service = RecallService(gateway)
    connection = object()
    embed = lambda _text: [1.0, 0.0]

    result = service.retrieve(
        connection, "task", k=3, embed_fn=embed, min_sim=0.9,
        qv=[1.0, 0.0], exclude_session="session", project="project",
        include_all_projects=True, embedding_model="embed-v2",
        embedding_revision="revision",
    )

    assert result == ["task -> exact result"]
    assert gateway.calls == [(connection, "task", {
        "k": 3, "embed_fn": embed, "min_sim": 0.9, "qv": [1.0, 0.0],
        "exclude_session": "session", "project": "project",
        "include_all_projects": True, "embedding_model": "embed-v2",
        "embedding_revision": "revision",
    })]


@pytest.mark.parametrize("limit", [True, 0, -1, 21, 2.5, "2"])
def test_service_rejects_invalid_or_unbounded_limits(limit):
    gateway = CapturingGateway()

    with pytest.raises(InvalidInput, match="recall limit"):
        RecallService(gateway).retrieve(None, "task", k=limit)

    assert gateway.calls == []


@pytest.mark.parametrize("threshold", [True, float("nan"), float("inf"), -1.1, 1.1])
def test_service_rejects_invalid_similarity_thresholds(threshold):
    gateway = CapturingGateway()

    with pytest.raises(InvalidInput, match="similarity threshold"):
        RecallService(gateway).retrieve(None, "task", min_sim=threshold)

    assert gateway.calls == []


def test_service_bounds_query_before_calling_gateway():
    gateway = CapturingGateway()

    with pytest.raises(InvalidInput, match="exceeds"):
        RecallService(gateway).retrieve(None, "x" * 64_001)

    assert gateway.calls == []


def test_service_preserves_successful_error_prefixed_data():
    gateway = CapturingGateway()
    gateway.recall = lambda *_args, **_kwargs: ["ERROR: stored compiler output"]

    assert RecallService(gateway).retrieve(None, "task") == [
        "ERROR: stored compiler output"
    ]


def test_adapter_rejects_malformed_environment_threshold_without_echo(monkeypatch):
    secret = "not-a-number-secret"
    monkeypatch.setenv("SONDER_RECALL_MIN_SIM", secret)

    with pytest.raises(InvalidInput) as raised:
        recall_adapter.recall(None, "task", qv=[1.0, 0.0])

    assert str(raised.value) == "recall similarity threshold is invalid"
    assert secret not in str(raised.value)


def test_legacy_gateway_maps_storage_errors_without_disclosing_details(monkeypatch):
    secret = r"C:\private\memory.db"
    replacement = SimpleNamespace(
        recall=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.DatabaseError(f"database corruption at {secret}")
        )
    )
    monkeypatch.setitem(sys.modules, "sonder_runtime.adapters.recall", replacement)

    with pytest.raises(DependencyUnavailable) as raised:
        LegacyRecallGateway().recall(None, "task")

    assert str(raised.value) == "semantic recall storage is unavailable"
    assert secret not in str(raised.value)


def test_legacy_gateway_resolves_live_adapter_module(monkeypatch):
    calls = []
    replacement = SimpleNamespace(
        recall=lambda connection, task, **options: calls.append(
            (connection, task, options)
        ) or ["live result"]
    )
    monkeypatch.setitem(sys.modules, "sonder_runtime.adapters.recall", replacement)

    assert LegacyRecallGateway().recall("connection", "task", k=4) == ["live result"]
    assert calls == [("connection", "task", {"k": 4})]


def test_server_live_reload_updates_adapter(monkeypatch):
    replacement = SimpleNamespace(recall=lambda *_args, **_kwargs: ["reloaded"])
    monkeypatch.setattr(
        server.live_reload, "reload_changed_modules",
        lambda _names: {"sonder_runtime.adapters.recall": replacement},
    )
    monkeypatch.setattr(server, "_refresh_runtime_policy", lambda create=True: None)
    monkeypatch.setitem(sys.modules, "sonder_runtime.adapters.recall", recall_adapter)

    server._maybe_live_reload()

    assert sys.modules["sonder_runtime.adapters.recall"] is replacement
    assert LegacyRecallGateway().recall(None, "task") == ["reloaded"]


def test_server_uses_application_recall_without_root_import():
    source = inspect.getsource(server._answer)
    assert "_application().recall.retrieve(" in source
    assert "recall.recall(" not in source
