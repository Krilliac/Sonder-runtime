import functools

import server
from sonder_runtime.adapters.inference import ollama_endpoint
from sonder_runtime.bootstrap import legacy_root


def test_binding_a_configured_endpoint_rebinds_the_display(monkeypatch):
    monkeypatch.setattr(server, "BASE", "http://127.0.0.1:1")
    monkeypatch.setattr(
        server, "_ollama_display", functools.partial(ollama_endpoint.safe_display, "http://127.0.0.1:1"),
    )

    legacy_root.bind_ollama_endpoint(server, "http://127.0.0.1:11434")

    assert server.BASE == "http://127.0.0.1:11434"
    assert server._ollama_display.args == (server.BASE,)
    assert server._ollama_display() == ollama_endpoint.safe_display("http://127.0.0.1:11434")
