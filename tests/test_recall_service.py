"""Typed semantic-recall seam and compatibility behavior."""
from __future__ import annotations

import importlib
import inspect
import sys
from types import SimpleNamespace

import recall
import server
from sonder_runtime.adapters import recall as recall_adapter
from sonder_runtime.adapters.legacy.recall import LegacyRecallGateway
from sonder_runtime.application.recall import RecallService


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


def test_root_recall_is_a_true_reload_safe_compatibility_alias():
    assert recall is recall_adapter
    original = recall.MAX_RESP_CHARS
    recall.MAX_RESP_CHARS = 17
    assert recall_adapter.MAX_RESP_CHARS == 17
    recall.MAX_RESP_CHARS = original
    assert importlib.reload(recall) is recall_adapter
    assert sys.modules["recall"] is sys.modules["sonder_runtime.adapters.recall"]


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


def test_server_live_reload_updates_adapter_and_preserves_root_alias(monkeypatch):
    replacement = SimpleNamespace(recall=lambda *_args, **_kwargs: ["reloaded"])
    monkeypatch.setattr(
        server.live_reload, "reload_changed_modules",
        lambda _names: {"sonder_runtime.adapters.recall": replacement},
    )
    monkeypatch.setattr(server, "_refresh_runtime_policy", lambda create=True: None)
    monkeypatch.setitem(
        sys.modules, "sonder_runtime.adapters.recall", recall_adapter,
    )
    monkeypatch.setitem(sys.modules, "recall", recall_adapter)

    server._maybe_live_reload()

    assert sys.modules["sonder_runtime.adapters.recall"] is replacement
    assert sys.modules["recall"] is replacement
    assert LegacyRecallGateway().recall(None, "task") == ["reloaded"]


def test_server_uses_application_recall_without_root_import():
    source = inspect.getsource(server._answer)
    assert "_application().recall.retrieve(" in source
    assert "recall.recall(" not in source
