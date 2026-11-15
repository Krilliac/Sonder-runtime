"""An admission refusal in the conformance battery is "not measured", never a failure."""
from __future__ import annotations

import contextlib
import time

import pytest

from sonder_runtime.adapters.inference import capability_refresh, ollama_conformance
from sonder_runtime.adapters.inference import gpu_admission as adapter
from sonder_runtime.adapters.inference.capability_evidence import load_production_evidence
from sonder_runtime.domain.routing.backend_conformance import (
    BackendCapability, BackendConformanceRecord, BackendIdentity, ProbeResult,
)
from tests.support.gpu_admission_harness import DIGEST, MODEL, FakeOllama, make_service

ORIGIN = "http://127.0.0.1:11434"


class ConformanceOllama(FakeOllama):
    """FakeOllama with the identity documents the conformance battery validates."""

    def _respond(self, path, body):
        if path == "/api/tags":
            return {"models": [{"name": MODEL, "model": MODEL, "digest": DIGEST,
                                "size": self.file_size}]}
        if path == "/api/show":
            return {"model_info": {"llama.context_length": 32768},
                    "details": {"quantization_level": "Q8_0", "family": "qwen3"},
                    "template": "{{ .Prompt }}"}
        return super()._respond(path, body)


def _loaded_at_8192(tmp_path, monkeypatch):
    fake = ConformanceOllama()
    fake.loaded[MODEL] = 8192
    service = make_service(tmp_path, fake, max_queue_wait_seconds=1)
    with service.admit("/api/chat", {"model": MODEL, "messages": [{"role": "user", "content": "x"}],
                                     "options": {"num_ctx": 8192}}, origin=ORIGIN):
        pass
    assert service._the_store().get_pin(ORIGIN, adapter.model_key(MODEL)).num_ctx == 8192
    monkeypatch.setattr(ollama_conformance.gpu_admission, "process_service", lambda: service)
    monkeypatch.setattr(ollama_conformance, "open_url", fake.open_url)
    return fake, service


def test_a_ctx_pin_conflict_during_refresh_keeps_the_previous_evidence(tmp_path, monkeypatch):
    fake, _service = _loaded_at_8192(tmp_path, monkeypatch)
    monkeypatch.setattr(capability_refresh, "configured_local_models", lambda _home: (MODEL,))
    evidence = load_production_evidence(tmp_path)
    identity = BackendIdentity("ollama", MODEL, DIGEST, "Q8_0", "0.0-test", "b" * 64,
                               "c" * 64, 4096, "fixture")
    previous = BackendConformanceRecord(
        "ollama", MODEL, time.time() - 60,
        (ProbeResult(BackendCapability.CHAT, True, "plain_chat_passed"),
         ProbeResult(BackendCapability.STRUCTURED, True, "live_protocol_passed")),
        probe_version=3, synthetic=False, identity=identity)
    evidence.save(previous)

    result = capability_refresh.refresh_capabilities(
        origin=ORIGIN, home=tmp_path, timeout_seconds=30, context_tokens=4096)

    outcome = result["models"][0]
    assert outcome["status"] == "deferred"
    assert outcome["reason"] == "ctx_pin_conflict"
    stored = load_production_evidence(tmp_path).load("ollama", MODEL)
    assert stored == previous, "an admission refusal replaced the earlier evidence"
    assert not stored.failed
    assert fake.chat_bodies == [] and fake.reloads == 0 and fake.loaded[MODEL] == 8192


def test_the_battery_raises_an_admission_refusal_instead_of_recording_a_failure(
        tmp_path, monkeypatch):
    _loaded_at_8192(tmp_path, monkeypatch)
    probe = ollama_conformance.OllamaConformanceProbe(ORIGIN, MODEL, context_tokens=4096)
    with pytest.raises(adapter.GpuAdmissionRefused) as caught:
        probe.run(timeout_seconds=30)
    assert caught.value.refusal.reason.value == "ctx_pin_conflict"


def test_a_request_with_no_window_is_sent_at_the_inherited_pin(tmp_path, monkeypatch):
    fake, _service = _loaded_at_8192(tmp_path, monkeypatch)
    probe = ollama_conformance.OllamaConformanceProbe(ORIGIN, MODEL, context_tokens=8192)
    probe._deadline = time.monotonic() + 30.0
    payload = {"model": MODEL, "stream": False, "messages": [{"role": "user", "content": "hi"}]}
    probe._request("/api/chat", payload, timeout=5.0)
    assert (fake.chat_bodies[-1].get("options") or {}).get("num_ctx") == 8192
    assert fake.reloads == 0


def test_the_transport_timeout_is_what_remains_after_the_admission_wait(monkeypatch):
    seen = []

    class SlowAdmission:
        @contextlib.contextmanager
        def admit(self, path, payload, **_kwargs):
            time.sleep(0.4)
            yield adapter.passthrough_ticket(payload)

    def open_url(request, timeout=None, **_kwargs):
        seen.append(timeout)
        raise OSError("stop after the timeout is observed")

    monkeypatch.setattr(ollama_conformance.gpu_admission, "process_service", SlowAdmission)
    monkeypatch.setattr(ollama_conformance, "open_url", open_url)
    probe = ollama_conformance.OllamaConformanceProbe(ORIGIN, MODEL)
    probe._deadline = time.monotonic() + 1.0
    with pytest.raises(OSError):
        probe._request("/api/version", None, timeout=5.0)
    assert seen and seen[0] <= 0.65
