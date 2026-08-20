from __future__ import annotations

import functools

import sonder_runtime.adapters.ollama.endpoint as ollama_endpoint
import server


def test_server_display_is_a_compatibility_alias_to_packaged_policy():
    assert isinstance(server._ollama_display, functools.partial)
    assert server._ollama_display.func is ollama_endpoint.safe_display
    assert server._ollama_display.args == (server.BASE,)


def test_legacy_zero_argument_display_contract_is_preserved():
    assert server._ollama_display() == ollama_endpoint.safe_display(server.BASE)
