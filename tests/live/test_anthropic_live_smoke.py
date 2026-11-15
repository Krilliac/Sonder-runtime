"""Opt-in live Anthropic smoke: one tiny prompt, ``max_tokens`` 16, real spend.

Never runs in bare CI.  To run it:

    SONDER_LIVE_ANTHROPIC=1 SONDER_ANTHROPIC_API_KEY=... \
        pytest --run-network --run-model -m 'model and network' \
        tests/live/test_anthropic_live_smoke.py

The variables are read from the live-provider capture that ``conftest.py``
takes *before* its ambient ``SONDER_*`` scrub and restores only through the
``live_provider_environment`` fixture: reading ``os.environ`` directly here
would always see them already scrubbed and the test could never run.  The
optional ``anthropic`` SDK must be installed (``pip install anthropic``).

With a 16-token ceiling a model that thinks first may spend the whole budget
before it writes text; the gateway then reports "no usable text".  That still
proves a real, billed round trip, so the test accepts it and asserts on the
recorded usage instead of on the reply text.
"""
from __future__ import annotations

import os

import pytest

from sonder_runtime.adapters.inference.anthropic_gateway import AnthropicGateway, sdk_installed
from sonder_runtime.adapters.model_request_admission import HostModelRequestAdmission
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.domain.common.errors import DependencyUnavailable

pytestmark = [pytest.mark.model, pytest.mark.network]

# The cheapest documented model (claude-api/shared/models.md); override with
# SONDER_ANTHROPIC_MODEL.
_LIVE_DEFAULT_MODEL = "claude-haiku-5-5"


def test_live_anthropic_smoke(live_provider_environment, monkeypatch):
    del live_provider_environment
    if os.environ.get("SONDER_LIVE_ANTHROPIC") != "1":
        pytest.skip("set SONDER_LIVE_ANTHROPIC=1 (and SONDER_ANTHROPIC_API_KEY) to spend real money")
    if not os.environ.get("SONDER_ANTHROPIC_API_KEY", "").strip():
        pytest.skip("SONDER_ANTHROPIC_API_KEY is not set")
    if not sdk_installed():
        pytest.skip("the optional anthropic SDK is not installed (pip install anthropic)")
    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")
    gateway = AnthropicGateway(request_admission=HostModelRequestAdmission())
    model = os.environ.get("SONDER_ANTHROPIC_MODEL", "").strip() or _LIVE_DEFAULT_MODEL
    context = local_owner_context(
        correlation_id="live-anthropic-smoke", timeout_seconds=60, cloud_allowed=True,
    )
    request = ModelRequest(
        prompt="Reply with exactly: SONDER_ANTHROPIC_OK", tier="fast",
        options={"model": model, "num_predict": 16},
    )
    try:
        response = gateway.generate(request, context)
    except DependencyUnavailable as exc:
        assert "no usable text" in str(exc)  # thinking consumed the 16-token budget
    else:
        assert response.text.strip()
        assert response.tokens_out is not None and response.tokens_out <= 16
    usage = gateway.last_usage
    assert usage is not None and usage["model"] == model
    assert usage["completion_tokens"] <= 16
    assert usage["key_slot"] in (1, 2)
