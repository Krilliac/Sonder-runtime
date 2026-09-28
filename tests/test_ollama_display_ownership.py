from __future__ import annotations


from sonder_runtime.adapters.inference import ollama_endpoint
import server


def test_server_display_is_a_compatibility_alias_to_packaged_policy(monkeypatch):
    # The display reads BASE per call, so it can never name a stale endpoint
    # after BASE is rebound (typed config install, test monkeypatching).
    monkeypatch.setattr(server, "BASE", "http://127.0.0.1:11434")
    assert server._ollama_display() == ollama_endpoint.safe_display("http://127.0.0.1:11434")
    monkeypatch.setattr(server, "BASE", "https://worker.example:11434")
    assert server._ollama_display() == ollama_endpoint.safe_display("https://worker.example:11434")


def test_legacy_zero_argument_display_contract_is_preserved():
    assert server._ollama_display() == ollama_endpoint.safe_display(server.BASE)
