"""Optional live ModelGateway smoke, intentionally outside offline conformance.

Run explicitly with ``SONDER_LIVE_MODEL_GATEWAY=ollama`` (or ``openai``) and
``pytest -m 'model and network' tests/live/test_model_gateway_live_smoke.py``.
Remote OpenAI-compatible endpoints additionally require
``SONDER_LIVE_ALLOW_REMOTE=1``.  Bare CI only collects a skip and never calls a
provider.
"""
from __future__ import annotations

import os

import pytest

from sonder_runtime.adapters.ollama.gateway import OllamaGateway
from sonder_runtime.adapters.openai_compat.gateway import OpenAICompatibleGateway
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest

pytestmark = [pytest.mark.model, pytest.mark.network]


def test_live_model_gateway_smoke():
    backend = os.environ.get("SONDER_LIVE_MODEL_GATEWAY", "").strip().lower()
    if backend not in {"ollama", "openai"}:
        pytest.skip("set SONDER_LIVE_MODEL_GATEWAY=ollama or openai")
    gateway = OllamaGateway() if backend == "ollama" else OpenAICompatibleGateway()
    allow_remote = os.environ.get("SONDER_LIVE_ALLOW_REMOTE") == "1"
    context = local_owner_context(
        correlation_id="live-provider-smoke",
        timeout_seconds=30,
        cloud_allowed=allow_remote,
        remote_ollama_allowed=allow_remote,
    )
    response = gateway.generate(
        ModelRequest(prompt="Reply with exactly: SONDER_GATEWAY_OK", tier="fast"),
        context,
    )
    assert "SONDER_GATEWAY_OK" in response.text
