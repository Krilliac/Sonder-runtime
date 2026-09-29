from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from sonder_runtime.platform.runtime_threads import Thread

import pytest

from sonder_runtime.adapters.inference.ollama_conformance import OllamaConformanceProbe
from sonder_runtime.domain.routing.backend_conformance import BackendCapability


class _Handler(BaseHTTPRequestHandler):
    state = {"digest": "sha256:" + "a" * 64, "version": "0.12.0", "label": "qwen",
             "masked": False, "rotate": False, "name": "qwen"}

    def log_message(self, *_args):
        return

    def _send(self, value):
        raw = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/api/version":
            return self._send({"version": self.state["version"]})
        return self._send({"models": [{"name": self.state["name"], "digest": self.state["digest"]}]})

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(size))
        if self.path == "/api/show":
            return self._send({"details": {"quantization_level": "Q4_K_M"}, "template": "{{ .Prompt }}",
                               "model_info": {"llama.context_length": 8192}})
        if self.state["rotate"]:
            self.state["digest"] = "b" * 64
        if payload.get("tools") and payload.get("format") and self.state["masked"]:
            return self._send({"model": self.state["label"], "message": {"content": '{"value":"alpha"}'}})
        if payload.get("format") and not payload.get("tools"):
            return self._send({"model": self.state["label"], "message": {"content": '{"value":"alpha"}'}})
        if payload.get("tools"):
            message = {"role": "assistant", "tool_calls": [{"function": {"name": "echo", "arguments": {"value": "alpha"}}}]}
            if any(m.get("role") == "tool" for m in payload.get("messages", [])):
                message = {"role": "assistant", "content": "done"}
            return self._send({"model": self.state["label"], "message": message})
        return self._send({"model": self.state["label"], "message": {"content": "hello"}})


@pytest.fixture
def server():
    handler = type("Handler", (_Handler,), {"state": dict(_Handler.state)})
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % httpd.server_address[1], handler.state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_identity_is_bound_to_version_digest_and_show(server):
    base, _state = server
    identity = OllamaConformanceProbe(base, "qwen").read_identity()
    assert identity.backend == "ollama"
    assert identity.model_digest == "a" * 64
    assert identity.backend_version == "0.12.0"
    assert identity.context_tokens == 8192


def test_run_publishes_measured_record_and_combined_tool_schema(server):
    base, _state = server
    probe = OllamaConformanceProbe(base, "qwen", context_tokens=8192)
    record = probe.run(timeout_seconds=10)
    assert record.synthetic is False
    assert record.identity is not None
    assert record.results[0].passed is True
    assert BackendCapability.TOOLS_WITH_SCHEMA in record.passed
    assert BackendCapability.VISION in record.unknown
    assert BackendCapability.LONG_CONTEXT in record.unknown


def test_schema_masking_records_failure_even_when_plain_tools_pass(server):
    base, state = server
    state["masked"] = True
    record = OllamaConformanceProbe(base, "qwen").run(timeout_seconds=10)
    assert BackendCapability.TOOL_NATIVE in record.passed
    assert BackendCapability.TOOLS_WITH_SCHEMA in record.failed
    encoded = record.to_dict()
    assert any(item["capability"] == "tools_with_schema" and item["passed"] is False
               for item in encoded["results"])


def test_identity_rotation_during_inference_cannot_publish_evidence(server):
    base, state = server
    state["rotate"] = True
    with pytest.raises(ValueError, match="identity changed"):
        OllamaConformanceProbe(base, "qwen").run(timeout_seconds=10)


def test_implicit_latest_tag_binds_to_the_same_artifact(server):
    base, state = server
    state.update(name="qwen:latest", label="qwen:latest")
    record = OllamaConformanceProbe(base, "qwen").run(timeout_seconds=10)
    assert record.identity.model == "qwen"
    assert BackendCapability.TOOL_NATIVE in record.passed


def test_digest_change_is_observed_and_model_label_mismatch_fails(server):
    base, state = server
    probe = OllamaConformanceProbe(base, "qwen")
    first = probe.read_identity()
    state["digest"] = "sha256:" + "b" * 64
    second = probe.read_identity()
    assert first.model_digest != second.model_digest
    state["label"] = "other"
    with pytest.raises(ValueError, match="model mismatch"):
        probe.run(timeout_seconds=5)


@pytest.mark.parametrize("url", ["http://example.com:11434", "https://127.0.0.1:11434", "http://127.0.0.1/path"])
def test_probe_refuses_non_loopback_or_ambiguous_urls(url):
    with pytest.raises(ValueError):
        OllamaConformanceProbe(url, "qwen")


def test_missing_digest_is_refused(server):
    base, state = server
    state["digest"] = "unknown"
    with pytest.raises(ValueError, match="digest"):
        OllamaConformanceProbe(base, "qwen").read_identity()


def test_tool_trace_requires_exact_single_echo_call():
    assert OllamaConformanceProbe._valid_echo_call(
        [{"function": {"name": "echo", "arguments": {"value": "alpha"}}}]
    )
    assert not OllamaConformanceProbe._valid_echo_call(
        [{"function": {"name": "echo", "arguments": {"value": "wrong"}}}]
    )
    assert not OllamaConformanceProbe._valid_echo_call(
        [{"function": {"name": "echo", "arguments": {"value": "alpha"}}},
         {"function": {"name": "echo", "arguments": {"value": "alpha"}}}]
    )
