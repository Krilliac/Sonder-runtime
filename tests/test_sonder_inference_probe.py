"""Sonder Inference identity reading, protocol probes and attestation CLI.

The CLI tests run ``scripts/backend_attest.py`` against a real loopback HTTP
server started in-process, so the stdlib transports, identity parsing and
evidence writing are the production code paths.  The server's responses are
fixtures, not model output, and nothing here is quality evidence.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scripts.backend_attest import attest, main
from sonder_runtime.adapters.inference.sonder_inference_gateway import (
    SonderInferenceConfig,
    SonderInferenceGateway,
)
from sonder_runtime.adapters.inference.sonder_inference_probe import (
    IdentityUnavailable,
    SonderInferenceIdentityReader,
    SonderInferenceProtocolProbe,
    SyntheticIdentityRefused,
)
from sonder_runtime.adapters.inference.openai_compat_gateway import (
    OpenAICompatibleConfig,
    OpenAICompatibleGateway,
)
from sonder_runtime.application.routing.backend_conformance import (
    RecentCapabilityEvidence,
)
from sonder_runtime.domain.routing.backend_conformance import (
    BackendCapability,
    BackendIdentity,
    EvidenceState,
)

IDENTITY = {
    "backend": "ollama", "model": "qwen", "model_digest": "a" * 64,
    "quantization": "Q4_K_M", "backend_version": "0.12.0", "tokenizer_digest": "b" * 64,
    "template_digest": "c" * 64, "context_tokens": 8192, "hardware": "cpu:host-1",
}


def _identity_doc(*, synthetic=False, identity=IDENTITY, reason=None, model="qwen"):
    return {"schema": "sonder.inference.identity/1", "model": model,
            "synthetic": synthetic, "backend_identity": identity, "reason": reason}


def _health(model="qwen", synthetic=False):
    return {"status": "ready", "api_version": 1, "version": "0.1.0", "synthetic": synthetic,
            "models": [{"id": model, "backend": "ollama", "default": True}]}


def _reply(prompt: str, model: str) -> dict:
    if "Return only a JSON object" in prompt:
        text = '{"tool":"echo","arguments":{"value":"alpha"}}'
    elif "Return only JSON" in prompt:
        text = '{"tool":"echo","continued":true}'
    else:
        text = "hello"
    return {"model": model, "choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


class Fake:
    def __init__(self, identity_doc, model="qwen", synthetic=False):
        self.identity_doc = identity_doc
        self.model = model
        self.synthetic = synthetic
        self.posts = []

    def get(self, url, headers, timeout):
        if "/v1/sonder/identity" in url:
            return 200, json.dumps(self.identity_doc).encode()
        return 200, json.dumps(_health(self.model, self.synthetic)).encode()

    def post(self, url, payload, headers, timeout):
        self.posts.append(payload)
        return _reply(payload["messages"][-1]["content"], self.model)


def _gateway(fake, model="qwen"):
    return SonderInferenceGateway(
        SonderInferenceConfig(base_url="http://127.0.0.1:18437", model=model),
        transport=fake.post, get_transport=fake.get,
    )


def test_reader_returns_measured_identity_and_refuses_synthetic_or_null():
    assert SonderInferenceIdentityReader(_gateway(Fake(_identity_doc())))() == (
        BackendIdentity.from_dict(IDENTITY)
    )
    with pytest.raises(SyntheticIdentityRefused, match="never attestation evidence"):
        SonderInferenceIdentityReader(_gateway(Fake(_identity_doc(synthetic=True))))()
    null = Fake(_identity_doc(identity=None, reason="tokenizer digest not measurable"))
    with pytest.raises(IdentityUnavailable, match="tokenizer digest"):
        SonderInferenceIdentityReader(_gateway(null))()
    with pytest.raises(TypeError):
        SonderInferenceIdentityReader(object())


def test_protocol_probe_records_identity_but_stays_diagnostic(tmp_path):
    fake = Fake(_identity_doc())
    gateway = _gateway(fake)
    reader = SonderInferenceIdentityReader(gateway, model="qwen")
    record = SonderInferenceProtocolProbe(gateway, identity_reader=reader).run(timeout_seconds=5)
    results = {item.capability: item for item in record.results}
    assert results[BackendCapability.CHAT].passed is True
    assert results[BackendCapability.STRUCTURED].passed is True
    assert results[BackendCapability.TOOL_NATIVE].passed is None
    assert results[BackendCapability.CANCELLATION].passed is None
    assert record.synthetic is True and record.identity == BackendIdentity.from_dict(IDENTITY)
    assert all(post["model"] == "qwen" for post in fake.posts)
    with pytest.raises(TypeError):
        SonderInferenceProtocolProbe(
            OpenAICompatibleGateway(OpenAICompatibleConfig("http://127.0.0.1:1")),
            identity_reader=reader,
        )


def test_protocol_probe_refuses_identity_that_changes_mid_run():
    fake = Fake(_identity_doc())
    gateway = SonderInferenceGateway(
        SonderInferenceConfig(base_url="http://127.0.0.1:18437", model="qwen",
                              health_ttl_seconds=0),
        transport=fake.post, get_transport=fake.get,
    )
    reader = SonderInferenceIdentityReader(gateway, model="qwen")
    probe = SonderInferenceProtocolProbe(gateway, identity_reader=reader)

    original_post = fake.post

    def rotating_post(url, payload, headers, timeout):
        fake.identity_doc = _identity_doc(identity={**IDENTITY, "model_digest": "d" * 64})
        return original_post(url, payload, headers, timeout)

    fake.post = rotating_post
    gateway._transport = rotating_post
    with pytest.raises(ValueError, match="changed"):
        probe.run(timeout_seconds=5)


def test_synthetic_evidence_never_satisfies_routing(tmp_path):
    fake = Fake(_identity_doc(synthetic=True))
    gateway = _gateway(fake)
    assert gateway.routing_identity("qwen") is None
    evidence = tmp_path / "capabilities.json"
    attest(gateway, backend="sonder-inference", model="qwen", evidence_path=evidence,
           timeout_seconds=5)
    store = RecentCapabilityEvidence(evidence)
    record = store.load("sonder-inference", "qwen")
    assert record.synthetic is True and record.identity is None
    verdict = store.assess(
        "qwen", frozenset({BackendCapability.CHAT}), backend="sonder-inference",
        identity=gateway.backend_identity("qwen"),
    )
    assert verdict.state is not EvidenceState.PASSED


# -- CLI against a real loopback server --------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    state: dict = {}

    def log_message(self, *args):
        return

    def _send(self, body):
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/v1/sonder/identity"):
            self._send(self.state["identity"])
        else:
            self._send(_health(self.state["model"], self.state["identity"]["synthetic"]))

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.state.setdefault("posts", []).append(payload)
        self._send(_reply(payload["messages"][-1]["content"], self.state["model"]))


@pytest.fixture
def server():
    handler = type("Handler", (_Handler,), {"state": {"model": "qwen", "identity": _identity_doc()}})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % httpd.server_address[1], handler.state
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cli_resolves_default_model_and_records_measured_identity(server, tmp_path, capsys):
    base, state = server
    evidence = tmp_path / "capabilities.json"
    assert main(["--backend", "sonder-inference", "--base-url", base,
                 "--evidence", str(evidence)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["model"] == "qwen" and output["identity_synthetic"] is False
    assert output["identity_declared"] is True and output["synthetic"] is True
    assert {post["model"] for post in state["posts"]} == {"qwen"}
    # The record is keyed by the backend the measured identity names.
    record = RecentCapabilityEvidence(evidence).load("ollama", "qwen")
    assert record.identity == BackendIdentity.from_dict(IDENTITY)


def test_cli_never_records_a_synthetic_identity(server, tmp_path, capsys):
    base, state = server
    state["identity"] = _identity_doc(synthetic=True)
    evidence = tmp_path / "capabilities.json"
    assert main(["--backend", "sonder-inference", "--base-url", base, "--model", "qwen",
                 "--evidence", str(evidence)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["identity_synthetic"] is True and output["identity_declared"] is False
    assert RecentCapabilityEvidence(evidence).load("sonder-inference", "qwen").identity is None

    with pytest.raises(SystemExit) as stopped:
        main(["--backend", "sonder-inference", "--base-url", base, "--model", "qwen",
              "--protocol-probes", "--evidence", str(tmp_path / "other.json")])
    assert stopped.value.code == 2
    assert not (tmp_path / "other.json").exists()


def test_cli_protocol_probes_bind_the_measured_identity(server, tmp_path, capsys):
    base, _state = server
    evidence = tmp_path / "capabilities.json"
    assert main(["--backend", "sonder-inference", "--base-url", base, "--model", "qwen",
                 "--protocol-probes", "--evidence", str(evidence)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert "structured" in output["passed"] and "tool_native" in output["unknown"]
    record = RecentCapabilityEvidence(evidence).load("ollama", "qwen")
    assert record.identity == BackendIdentity.from_dict(IDENTITY) and record.synthetic is True


@pytest.mark.parametrize("argv", [
    ["--backend", "sonder-inference", "--base-url", "https://gpu.example", "--dry-run"],
    ["--backend", "sonder-inference", "--base-url", "http://gpu.example", "--allow-cloud",
     "--dry-run"],
    ["--backend", "sonder-inference", "--identity-file", "/tmp/x.json", "--dry-run"],
    ["--backend", "sonder-inference", "--base-url", "http://u:p@127.0.0.1:1", "--dry-run"],
])
def test_cli_refuses_unconsented_or_ambiguous_inference_targets(argv, tmp_path):
    with pytest.raises(SystemExit) as stopped:
        main([*argv, "--evidence", str(tmp_path / "evidence.json")])
    assert stopped.value.code == 2
    assert not (tmp_path / "evidence.json").exists()


def test_cli_dry_run_does_not_contact_the_server(tmp_path, capsys):
    assert main(["--backend", "sonder-inference", "--base-url", "http://127.0.0.1:1",
                 "--dry-run", "--evidence", str(tmp_path / "e.json")]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["dry_run"] is True and output["backend"] == "sonder-inference"
    assert not (tmp_path / "e.json").exists()
