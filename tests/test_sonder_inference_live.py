"""Opt-in live check of SonderInferenceGateway against a real ``sonder-infer serve``.

Start a server, then opt in to both live markers::

    sonder-infer serve --backend mock --port 18437 &
    SONDER_TEST_INFERENCE_URL=http://127.0.0.1:18437 \\
        python -m pytest -q --run-network --run-model tests/test_sonder_inference_live.py

Without the URL (or without both opt-in flags) the test is skipped, so bare
CI never contacts a provider.  The mock backend emits synthetic text: this
test asserts wiring, correlation and labelling only, never output quality.
"""
from __future__ import annotations

import os

import pytest

from sonder_runtime.adapters.inference.sonder_inference_gateway import (
    SonderInferenceConfig,
    SonderInferenceGateway,
    SonderInferenceUnreachable,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest

pytestmark = [pytest.mark.model, pytest.mark.network]


def test_live_sonder_inference_round_trip(live_provider_environment):
    del live_provider_environment
    url = os.environ.get("SONDER_TEST_INFERENCE_URL", "").strip()
    if not url:
        pytest.skip("set SONDER_TEST_INFERENCE_URL to a running `sonder-infer serve`")
    gateway = SonderInferenceGateway(SonderInferenceConfig(
        base_url=url,
        model=os.environ.get("SONDER_INFERENCE_MODEL", "").strip() or "default",
        api_key=os.environ.get("SONDER_INFERENCE_API_KEY", "").strip(),
        health_ttl_seconds=0.0,
    ))

    status = gateway.provider_status()["sonder_inference"]
    assert status["state"] == "ready", status["detail"]
    assert status["api_version"] == 1
    assert status["models"], "server lists no models"
    assert status["telemetry"]["sse_url"].startswith(url)

    context = local_owner_context(
        correlation_id="live-sonder-inference-1", source="http", timeout_seconds=60,
    )
    response = gateway.generate(
        ModelRequest(prompt="Say hello.", tier="fast", options={"num_predict": 16}),
        context,
    )
    assert response.text.strip()
    assert response.model and response.model != "default"
    assert response.tokens_out is None or response.tokens_out > 0

    observation = gateway.observe_identity()
    if status["synthetic"]:
        # The mock backend is labelled synthetic end to end and can never be
        # routing evidence, even when every digest is present.
        assert observation.synthetic is True
        assert gateway.routing_identity() is None
    if observation.identity is None:
        assert observation.reason

    with pytest.raises(SonderInferenceUnreachable):
        SonderInferenceGateway(
            SonderInferenceConfig(base_url="http://127.0.0.1:9"),
        ).generate(ModelRequest(prompt="x", tier="fast"), context)
