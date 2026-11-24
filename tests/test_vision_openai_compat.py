"""Unit tests for OpenAI-compatible local multimodal vision routing (e.g. Bonsai-2 mmproj)."""
from __future__ import annotations

import json
from unittest import mock

import pytest

import server

pytestmark = pytest.mark.unit


class _FakeResp:
    def __init__(self, body: dict):
        self._raw = json.dumps(body).encode("utf-8")

    def read(self, _limit: int = -1) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_vision_analyze_routes_to_openai_compatible_when_bound(monkeypatch):
    seen = {}

    class _Opener:
        def open(self, req, timeout=None):
            seen["url"] = req.full_url
            seen["payload"] = json.loads(req.data.decode("utf-8"))
            seen["timeout"] = timeout
            return _FakeResp({"choices": [{"message": {"content": "A glowing neon cube."}}]})

    monkeypatch.setenv("SONDER_VISION_PROVIDER", "openai-compatible")
    monkeypatch.setenv("SONDER_VISION", "bonsai-27b-pq2")
    monkeypatch.setenv("SONDER_OPENAI_BASE_URL", "http://127.0.0.1:11439")
    monkeypatch.setattr(server, "_vision_input", lambda *a, **k: ("fake.png", {"format": "png"}, "QUJD"))

    with mock.patch("urllib.request.build_opener", return_value=_Opener()):
        out = server._vision_analyze_impl("fake.png", "What is in this image?")

    assert out == "A glowing neon cube."
    assert seen["url"] == "http://127.0.0.1:11439/v1/chat/completions"
    assert seen["payload"]["model"] == "bonsai-27b-pq2"
    assert seen["payload"]["chat_template_kwargs"] == {"enable_thinking": False}
    user_parts = seen["payload"]["messages"][1]["content"]
    assert user_parts[0] == {"type": "text", "text": "What is in this image?"}
    assert user_parts[1]["image_url"]["url"] == "data:image/png;base64,QUJD"


def test_vision_openai_compatible_rejects_non_loopback_endpoint(monkeypatch):
    monkeypatch.setenv("SONDER_VISION_PROVIDER", "openai-compatible")
    monkeypatch.setenv("SONDER_VISION", "bonsai-27b-pq2")
    monkeypatch.delenv("SONDER_VISION_BASE_URL", raising=False)
    monkeypatch.setenv("SONDER_OPENAI_BASE_URL", "https://192.168.1.99:18080")
    monkeypatch.setattr(server, "_vision_input", lambda *a, **k: ("fake.png", {"format": "png"}, "QUJD"))

    with pytest.raises(server.ModelCallError, match="loopback"):
        server._vision_analyze_impl("fake.png", "Describe")


def test_vision_base_url_override_prefers_explicit_loopback(monkeypatch):
    seen = {}

    class _Opener:
        def open(self, req, timeout=None):
            seen["url"] = req.full_url
            return _FakeResp({"choices": [{"message": {"content": "Direct mmproj ok."}}]})

    monkeypatch.setenv("SONDER_VISION_PROVIDER", "openai-compatible")
    monkeypatch.setenv("SONDER_VISION", "bonsai-27b-pq2")
    monkeypatch.setenv("SONDER_OPENAI_BASE_URL", "http://127.0.0.1:11439")
    monkeypatch.setenv("SONDER_VISION_BASE_URL", "http://127.0.0.1:18080")
    monkeypatch.setattr(server, "_vision_input", lambda *a, **k: ("fake.png", {"format": "png"}, "QUJD"))

    with mock.patch("urllib.request.build_opener", return_value=_Opener()):
        out = server._vision_analyze_impl("fake.png", "What is this?")

    assert out == "Direct mmproj ok."
    assert seen["url"] == "http://127.0.0.1:18080/v1/chat/completions"
