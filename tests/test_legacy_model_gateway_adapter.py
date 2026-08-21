"""Canonical ownership and behavior tests for the legacy model gateway."""

import pytest

import server
import sonder_runtime.adapters.embeddings as embeddings
from sonder_runtime.adapters.legacy_model_gateway import LegacyModelGateway
from sonder_runtime.adapters.legacy_model_gateway import (
    LegacyModelGateway as CompatibilityGateway,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.domain.common.errors import InvalidInput


def test_strangler_name_preserves_identity_with_canonical_gateway():
    assert CompatibilityGateway is LegacyModelGateway


def test_generate_preserves_legacy_request_shape_and_response(monkeypatch):
    calls = []

    def sonder(prompt, *, history=None, tier=None):
        calls.append((prompt, history, tier))
        return "legacy response"

    monkeypatch.setattr(server, "sonder", sonder)
    response = LegacyModelGateway(generate=sonder).generate(
        ModelRequest("hello", "code", history=(("user", "prior"),)),
        local_owner_context(correlation_id="legacy-gateway"),
    )

    assert response.text == "legacy response"
    assert response.model == "code"
    assert response.tier == "code"
    assert calls == [("hello", [("user", "prior")], "code")]


def test_embed_preserves_order_and_validates_vectors(monkeypatch):
    monkeypatch.setattr(embeddings, "embed", lambda value: [float(len(value))])

    result = LegacyModelGateway().embed(
        ["a", "abcd"], local_owner_context(correlation_id="legacy-embed")
    )

    assert [item.vector for item in result] == [(1.0,), (4.0,)]
    assert [item.model for item in result] == ["local", "local"]


def test_generate_rejects_empty_prompt_before_legacy_call(monkeypatch):
    called = []
    monkeypatch.setattr(server, "sonder", lambda *args, **kwargs: called.append(1))

    with pytest.raises(InvalidInput):
        LegacyModelGateway().generate(
            ModelRequest("  ", "code"),
            local_owner_context(correlation_id="legacy-empty"),
        )

    assert called == []
