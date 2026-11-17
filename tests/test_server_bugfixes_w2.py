"""Regression tests for server.py / serve.py bugfixes (#7, #14, #16, #18, #38, #41)."""
from __future__ import annotations

import json

import pytest

import server
from tests.test_http_approvals import _http_server, _local_open, _request

pytestmark = pytest.mark.unit


def test_route_request_filters_cloud_tiers_when_cloud_disabled(monkeypatch):
    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "0")
    monkeypatch.setattr(server, "_refresh_live_cloud_tiers", lambda: None)
    out = server.route_request("please use cloud-code")
    assert "tier: cloud-" not in out
    assert "tier: code" in out


def test_ensemble_answer_reports_served_model(monkeypatch):
    def fake_gen_factory(tier_label, model, *_a, **_k):
        def gen(_prompt, _history=None):
            gen.last_response_meta = {"served_model": "Qwen3.8-27B"}
            return "answer from qwen"
        gen.last_response_meta = {}
        return gen

    monkeypatch.setattr(server, "_ensemble_targets", lambda _t: ([("code", "qwen3.6:35b")], []))
    monkeypatch.setattr(server, "_make_tier_generate", fake_gen_factory)
    monkeypatch.setattr(server, "_post", lambda *_a, **_k: {})
    out = server.ensemble_answer("hello", tiers="code")
    assert "Qwen3.8-27B" in out


def test_slash_commands_route_on_non_default_selected_model(monkeypatch):
    _local_open(monkeypatch)
    monkeypatch.setattr(server, "resolve_discovered_model_record", lambda s: ("qwen3:8b", {"name": "qwen3:8b"}))
    monkeypatch.setattr(server, "prewarm_model", lambda *_a, **_k: False)
    with _http_server(monkeypatch) as port:
        status, _, payload = _request(
            port, "POST", "/v1/chat/completions",
            body=json.dumps({
                "model": "qwen3:8b",
                "messages": [{"role": "user", "content": "/help"}],
                "stream": False,
            }),
            headers={"Content-Type": "application/json"},
        )
    assert status == 200, payload
    text = json.loads(payload)["choices"][0]["message"]["content"]
    assert "/write" in text or "Available" in text or "command" in text.lower()
