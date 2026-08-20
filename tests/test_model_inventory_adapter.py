"""Ownership and compatibility tests for the Ollama inventory adapter."""

import pytest

import server
from sonder_runtime.adapters.model_inventory import inventory_rows
from sonder_runtime.adapters.model_transport import ModelCallError


def test_server_inventory_rows_delegates_to_packaged_adapter():
    assert server._inventory_rows is not inventory_rows
    assert server._inventory_rows({"models": [{"name": "alpha"}]}, "/api/tags") == [
        {"name": "alpha"}
    ]


@pytest.mark.parametrize(
    "payload",
    [None, [], {"models": {}}, {"models": "not-a-list"}],
)
def test_inventory_rows_rejects_malformed_envelopes(payload):
    with pytest.raises(ModelCallError) as exc_info:
        inventory_rows(payload, "/api/tags")
    assert exc_info.value.kind == "protocol"
    assert "invalid Ollama /api/tags response" in str(exc_info.value)


def test_inventory_rows_skips_malformed_rows_without_losing_valid_rows():
    payload = {"models": [{"name": "alpha"}, "bad", None, {"name": "beta"}]}
    assert inventory_rows(payload, "/api/ps") == [
        {"name": "alpha"},
        {"name": "beta"},
    ]
