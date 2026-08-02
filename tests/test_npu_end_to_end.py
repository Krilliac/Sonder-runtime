"""Full-stack accelerator tests: policy file -> npu_service -> broker ->
real simulator worker subprocess, with no fakes in the path."""
import json
import time

import pytest

import npu_broker
import npu_service
import runtime_policy
from tests.npu_helpers import (
    embedding_payload,
    isolate_broker_env,
    routing_payload,
)


@pytest.fixture
def live_stack(monkeypatch, tmp_path):
    manifest_dir = tmp_path / "npu-manifests"
    manifest_dir.mkdir()
    isolate_broker_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SONDER_NPU_DETECTED", "0")
    monkeypatch.setenv("SONDER_NPU_MANIFEST_DIR", str(manifest_dir))
    monkeypatch.setenv(
        "SONDER_RUNTIME_POLICY", str(tmp_path / "runtime_policy.json"),
    )
    npu_service.reset_for_tests()
    npu_broker.reset_for_tests()
    yield manifest_dir
    npu_broker.reset_for_tests()


def _wait_for(predicate, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value is not None:
            return value
        time.sleep(0.05)
    return None


def test_route_decide_end_to_end_with_simulator(live_stack):
    (live_stack / "exec-route-v1.json").write_text(
        json.dumps(routing_payload(live_stack)), encoding="utf-8",
    )
    runtime_policy.update(npu={"mode": "off", "routing": "prefer"})
    prompt = (
        "Inspect the repository, diagnose the API, then fix the app, "
        "then run and validate all tests across every module."
    )
    # Cold start must fall back instantly (lazy start, non-blocking).
    assert npu_service.route_decide(prompt) is None
    decision = _wait_for(lambda: npu_service.route_decide(prompt))
    assert decision is not None
    assert decision["mode"] in ("workbench", "autopilot")
    assert decision["source"] == "cpu simulator"
    assert "cpu-sim" in decision["reason"]
    assert 0.6 <= decision["confidence"] <= 1.0
    assert decision["tier"] in ("fast", "code", "general")


def test_embed_for_space_end_to_end_refuses_simulator_substitution(live_stack):
    revision = "ollama-manifest-sha256:" + "a" * 64
    payload = embedding_payload(
        live_stack,
        space={"model": "nomic-embed-text:latest", "revision": revision},
    )
    (live_stack / "embed-tiny-v1.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )
    runtime_policy.update(npu={"mode": "off", "embeddings": "prefer"})
    args = (
        "hello accelerated world", "nomic-embed-text:latest", revision, 8,
    )
    assert npu_service.embed_for_space(*args) is None  # cold, lazy start
    assert _wait_for(
        lambda: (npu_service.status().get("utility_ready") or None),
    ) is True
    assert npu_service.status().get("runtime_ready") is False
    assert npu_service.embed_for_space(*args) is None
    # A different revision must never be served, even while warm.
    assert npu_service.embed_for_space(
        "hello accelerated world", "nomic-embed-text:latest",
        "ollama-manifest-sha256:" + "b" * 64,
        expected_dimension=8,
    ) is None


def test_status_end_to_end_reports_probe_results(live_stack):
    (live_stack / "exec-route-v1.json").write_text(
        json.dumps(routing_payload(live_stack)), encoding="utf-8",
    )
    runtime_policy.update(npu={"mode": "shadow"})
    state = npu_service.status(probe=True)
    assert state["enabled"] is True
    ready = _wait_for(
        lambda: (npu_service.status().get("utility_ready") or None),
    )
    assert ready is True
    state = npu_service.status()
    assert state["detected"] is False  # simulator never claims silicon
    assert state["runtime_ready"] is False
    assert state["broker"]["models"][0]["simulated"] is True
    text = npu_service.format_status(state)
    assert "cpu-sim" in text
    assert "never a model tier" in text
