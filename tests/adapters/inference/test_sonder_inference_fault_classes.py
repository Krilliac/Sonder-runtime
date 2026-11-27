"""POOL-06: retry only provable non-execution (429 overloaded, proxy dial failure)."""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from sonder_runtime.adapters.inference.sonder_inference_gateway import (
    PrivateWorkerSpec,
    SonderInferenceConfig,
    SonderInferenceGateway,
    SonderInferenceUnreachable,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.domain.common.errors import CapacityExceeded, DependencyUnavailable

PRIMARY = "http://127.0.0.1:11437"
WORKER_1 = "https://192.0.2.10:8443"
WORKER_2 = "https://192.0.2.11:8443"

_HEALTH = {
    "status": "ready", "api_version": 1, "version": "0.1.0",
    "backends": [{"name": "llamaserver", "available": True, "capabilities": ["streaming"]}],
    "models": [{"id": "qwen3:8b", "backend": "llamaserver", "default": True}],
    "telemetry": {"level": "standard", "subscribers": 0, "retained": 0,
                  "capacity": 8192, "emitted": 0, "dropped": 0},
}
_OK = {
    "id": "c", "object": "chat.completion", "created": 1, "model": "qwen3:8b",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
    "sonder": {"api_version": 1, "request_id": "r"},
}


def _error(url, status, code="", headers=None):
    body = json.dumps({"error": {"message": "x", "type": "x", "code": code}}).encode()
    return urllib.error.HTTPError(url, status, "err", headers or {}, io.BytesIO(body))


class _Fleet:
    """Worker 1 fails with *fault*; worker 2 always answers."""

    def __init__(self, fault):
        self.fault = fault
        self.executed = {WORKER_1: 0, WORKER_2: 0}
        self.attempts = {WORKER_1: 0, WORKER_2: 0}

    def get(self, url, headers, timeout):
        for base in (PRIMARY, WORKER_1, WORKER_2):
            if url.startswith(base):
                health = dict(_HEALTH)
                if base == PRIMARY:
                    health["models"] = [{"id": "other-only", "backend": "llamaserver",
                                         "default": True}]
                return 200, json.dumps(health).encode()
        raise AssertionError(url)

    def post(self, url, payload, headers, timeout):
        base = WORKER_1 if url.startswith(WORKER_1) else WORKER_2
        self.attempts[base] += 1
        if base == WORKER_1:
            raise self.fault(url)
        self.executed[base] += 1
        return _OK


def _gateway(fleet, tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_ALLOW_REMOTE_INFERENCE", "1")
    monkeypatch.setenv("SONDER_INFERENCE_WORKER_TOKEN", "w" * 32)
    ca = tmp_path / "ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n-----END CERTIFICATE-----\n", encoding="utf-8")
    cfg = SonderInferenceConfig(
        base_url=PRIMARY, model="qwen3:8b", allow_remote=True,
        workers=(
            PrivateWorkerSpec(WORKER_1, str(ca), "SONDER_INFERENCE_WORKER_TOKEN", 2),
            PrivateWorkerSpec(WORKER_2, str(ca), "SONDER_INFERENCE_WORKER_TOKEN", 2),
        ),
    )
    return SonderInferenceGateway(cfg, get_transport=fleet.get, transport=fleet.post)


def _run(gw):
    return gw.generate(ModelRequest(prompt="hi", tier="code"),
                       local_owner_context(correlation_id="pool-06"))


def test_429_overloaded_is_retried_elsewhere_and_first_endpoint_executed_nothing(
    tmp_path, monkeypatch,
):
    fleet = _Fleet(lambda url: _error(url, 429, "overloaded"))
    response = _run(_gateway(fleet, tmp_path, monkeypatch))
    assert response.text == "ok"
    assert fleet.attempts[WORKER_1] == 1 and fleet.executed[WORKER_1] == 0
    assert fleet.executed[WORKER_2] == 1


def test_proxy_dial_failure_503_is_unreachable_and_retried_elsewhere(tmp_path, monkeypatch):
    fleet = _Fleet(lambda url: _error(
        url, 503, "upstream_unreachable", {"X-Sonder-Proxy-Upstream": "unreachable"}))
    response = _run(_gateway(fleet, tmp_path, monkeypatch))
    assert response.text == "ok"
    assert fleet.attempts[WORKER_1] == 1 and fleet.executed[WORKER_1] == 0
    assert fleet.executed[WORKER_2] == 1


@pytest.mark.parametrize("fault", [
    pytest.param(lambda url: _error(url, 429, "rate_limited"), id="429-other-code"),
    pytest.param(lambda url: _error(url, 429, ""), id="429-no-code"),
    pytest.param(lambda url: _error(url, 504, ""), id="504"),
    pytest.param(lambda url: _error(url, 502, ""), id="502-bare"),
    pytest.param(lambda url: _error(url, 500, ""), id="500"),
    pytest.param(lambda url: _error(url, 503, "upstream_unreachable"), id="503-no-header"),
    pytest.param(lambda url: _error(
        url, 503, "", {"X-Sonder-Proxy-Upstream": "unreachable"}), id="503-header-no-code"),
    pytest.param(lambda url: urllib.error.URLError(ConnectionResetError("reset")),
                 id="mid-stream-reset"),
])
def test_everything_else_is_one_attempt_and_final(tmp_path, monkeypatch, fault):
    fleet = _Fleet(fault)
    gw = _gateway(fleet, tmp_path, monkeypatch)
    with pytest.raises((CapacityExceeded, DependencyUnavailable)) as caught:
        _run(gw)
    assert not isinstance(caught.value, SonderInferenceUnreachable)
    assert fleet.attempts[WORKER_1] + fleet.attempts[WORKER_2] == 1
    assert fleet.executed[WORKER_2] == 0 or fleet.attempts[WORKER_1] == 0
