import server
from sonder_runtime.adapters.inference import ollama_endpoint


def test_display_follows_base_after_it_is_rebound(monkeypatch):
    monkeypatch.setattr(server, "BASE", "http://127.0.0.1:1")
    assert server._ollama_display() == ollama_endpoint.safe_display("http://127.0.0.1:1")
    server.BASE = "http://127.0.0.1:11434"  # as legacy_root.configure_application does
    assert server._ollama_display() == ollama_endpoint.safe_display("http://127.0.0.1:11434")
