"""End-to-end entrypoint checks for provider-bound agent/autopilot calls."""
from __future__ import annotations

from types import SimpleNamespace
import threading

import pytest

from sonder_runtime.adapters.provider_bindings import ProviderBindings
from sonder_runtime.application.ports.model_gateway import ModelResponse
from sonder_runtime.platform.runtime_threads import Thread


class _Gateway:
    def __init__(self):
        self.requests = []
        self.lock = threading.Lock()

    def generate(self, request, _context):
        with self.lock:
            self.requests.append(request)
        return ModelResponse(
            text='{"summary": "inspect", "success_criteria": ["evidence present"], '
            '"tasks": [{"id": "t1", "title": "inspect", '
            '"kind": "validate", "instruction": "collect evidence", '
            '"depends_on": []}]}',
            model="fake-inference", tier=request.tier, tokens_in=11, tokens_out=5,
        )


def _graph(gateway):
    bindings = ProviderBindings.uniform("sonder_inference")
    return SimpleNamespace(provider_bindings=bindings, model_gateway=gateway)


def _run():
    return {
        "objective": "inspect the repository",
        "project": ".",
        "policy": "workspace",
        "tier": "code",
        "allow_web": False,
        "adaptive": False,
        "max_tasks": 3,
        "max_replans": 0,
    }


def test_actual_autopilot_planner_uses_fake_sonder_inference_gateway(monkeypatch):
    import server

    gateway = _Gateway()
    graph = _graph(gateway)
    monkeypatch.setattr(server, "_APP_GRAPH", graph)
    monkeypatch.setattr(server, "_application", lambda: graph)
    monkeypatch.setattr(server, "_build_system", lambda *args, **kwargs: "system")
    monkeypatch.setattr(server._prompts, "render", lambda *args, **kwargs: "planner")
    monkeypatch.setattr(server, "_autopilot_allowed_tools", lambda _run: set())
    monkeypatch.setattr(server, "_serve_target", lambda *args, **kwargs: (
        "fake-model", False, "", "code",
    ))
    monkeypatch.setattr(server, "_refresh_runtime_policy", lambda *args, **kwargs: object())
    monkeypatch.setattr(server, "_post_model", lambda *args, **kwargs: pytest.fail(
        "Sonder-Inference planning must not call the Ollama HTTP transport"
    ))
    monkeypatch.setattr(server, "_auto_model_context", lambda *args, **kwargs: pytest.fail(
        "Sonder-Inference planning must not probe Ollama model context"
    ))

    results = []
    errors = []

    def invoke():
        try:
            results.append(server._autopilot_plan_model(_run()))
        except Exception as exc:
            errors.append(exc)

    threads = [Thread(target=invoke) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert not errors, errors
    assert len(results) == 6
    assert len(gateway.requests) == 6
    assert all(request.tier == "code" for request in gateway.requests)


def test_generate_text_entrypoint_honors_tier_binding_without_ollama_transport(monkeypatch):
    import server

    gateway = _Gateway()
    graph = _graph(gateway)
    monkeypatch.setattr(server, "_APP_GRAPH", graph)
    monkeypatch.setattr(server, "_application", lambda: graph)
    monkeypatch.setattr(server, "_refresh_live_cloud_tiers", lambda: None)
    monkeypatch.setattr(server, "_post_model", lambda *args, **kwargs: pytest.fail(
        "bound helper must not call Ollama"
    ))
    monkeypatch.setattr(server, "_auto_model_context", lambda *args, **kwargs: pytest.fail(
        "bound helper must not probe Ollama"
    ))
    monkeypatch.setitem(server.TIERS, "code", "fake-model")
    assert server._generate_text("hello", tier="code") == (
        '{"summary": "inspect", "success_criteria": ["evidence present"], '
        '"tasks": [{"id": "t1", "title": "inspect", '
        '"kind": "validate", "instruction": "collect evidence", '
        '"depends_on": []}]}'
    )
    assert len(gateway.requests) == 1
