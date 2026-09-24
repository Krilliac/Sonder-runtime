from __future__ import annotations

import json

import pytest

from scripts.backend_attest import _read_host_identity, attest, main
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


def _identity():
    return BackendIdentity(
        backend="openai-compatible", model="fixture", model_digest="a" * 64,
        quantization="Q4_K_M", backend_version="v1", tokenizer_digest="b" * 64,
        template_digest="c" * 64, context_tokens=8192, hardware="cpu:host-1",
    )


def test_attest_fake_transport_records_only_typed_evidence(tmp_path):
    def transport(url, payload, headers, timeout):
        prompt = payload["messages"][-1]["content"]
        text = '{"tool":"echo","continued":true}' if "Return only JSON" in prompt else "ok"
        return {"choices": [{"message": {"content": text}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    evidence = tmp_path / "capabilities.json"
    result = attest(
        OpenAICompatibleGateway(
            OpenAICompatibleConfig("http://127.0.0.1:8080", model="fixture"),
            transport=transport,
        ),
        backend="openai-compatible",
        model="fixture",
        evidence_path=evidence,
        timeout_seconds=5,
    )
    assert result["failed"] == []
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    record = next(iter(payload["records"].values()))
    assert record["synthetic"] is True
    assert result["identity_bound"] is False
    assert "ok" not in evidence.read_text(encoding="utf-8")


def test_attest_dry_run_does_not_call_or_write(tmp_path):
    class ExplodingGateway:
        def generate(self, *_args, **_kwargs):
            raise AssertionError("dry-run contacted provider")

    evidence = tmp_path / "capabilities.json"
    result = attest(
        ExplodingGateway(), backend="openai-compatible", model="fixture",
        evidence_path=evidence, timeout_seconds=5, dry_run=True,
    )
    assert result["dry_run"] is True
    assert not evidence.exists()


def test_operator_identity_file_rejects_relative_alias_and_incomplete_identity(tmp_path):
    identity_file = tmp_path / "identity.json"
    with pytest.raises(ValueError, match="absolute"):
        _read_host_identity("identity.json")
    identity_file.write_text('{"backend":"openai-compatible"}', encoding="utf-8")
    with pytest.raises(ValueError, match="exact backend identity"):
        _read_host_identity(str(identity_file))
    alias = tmp_path / "alias.json"
    alias.symlink_to(identity_file)
    with pytest.raises(ValueError, match="ordinary"):
        _read_host_identity(str(alias))
    identity_file.write_text(json.dumps(BackendIdentity(
        backend="openai-compatible", model="fixture", model_digest="a" * 64,
        quantization="Q4_K_M", backend_version="v1", tokenizer_digest="b" * 64,
        template_digest="c" * 64, context_tokens=8192, hardware="cpu:host-1",
    ).to_dict()), encoding="utf-8")
    assert _read_host_identity(str(identity_file)).model_digest == "a" * 64


def test_opt_in_protocol_probes_do_not_certify_unbound_tools_or_identity(tmp_path):
    exchanges = []

    def transport(_url, payload, _headers, _timeout):
        messages = payload["messages"]
        exchanges.append(messages)
        prompt = messages[-1]["content"]
        if prompt.startswith("Reply with a nonempty"):
            reply = "hello"
        elif "value beta" in prompt:
            reply = '{"tool":"echo","arguments":{"value":"beta"}}'
        elif "CONTINUED:alpha" in prompt:
            reply = "CONTINUED:alpha"
        else:
            reply = '{"tool":"echo","arguments":{"value":"alpha"}}'
        return {"model": "fixture", "choices": [{"message": {"content": reply}}]}

    identity = _identity()
    evidence = tmp_path / "capabilities.json"
    result = attest(
        OpenAICompatibleGateway(
            OpenAICompatibleConfig("http://127.0.0.1:8080", model="fixture"),
            transport=transport,
        ),
        backend="openai-compatible", model="fixture", evidence_path=evidence,
        timeout_seconds=5, protocol_probes=True, identity=identity,
        identity_reader=lambda: identity,
    )
    assert result["synthetic"] is True
    assert set(result["passed"]) == {"chat", "structured"}
    assert set(result["unknown"]) == {
        "cancellation", "tool_native", "tool_fallback",
        "tool_sequential", "tool_continuation",
    }
    assert len(exchanges) == 2
    assert all(len(messages) == 1 and messages[0]["role"] == "user" for messages in exchanges)
    record = RecentCapabilityEvidence(evidence).load("openai-compatible", "fixture")
    assert record is not None and record.identity == identity
    assert BackendCapability.TOOL_NATIVE in record.unknown
    assert BackendCapability.TOOL_FALLBACK in record.unknown
    assert not result["identity_bound"] and result["identity_declared"]
    assert RecentCapabilityEvidence(evidence).assess(
        "fixture", frozenset({BackendCapability.CHAT}), backend="openai-compatible",
        identity=identity,
    ).state is EvidenceState.UNKNOWN
    assert "CONTINUED:alpha" not in evidence.read_text(encoding="utf-8")


def test_protocol_probes_refuse_provider_model_alias_and_changed_host_identity(tmp_path):
    identity = _identity()
    evidence = tmp_path / "capabilities.json"
    gateway = OpenAICompatibleGateway(
        OpenAICompatibleConfig("http://127.0.0.1:8080", model="fixture"),
        transport=lambda *_args: {
            "model": "aliased-model",
            "choices": [{"message": {"content": '{"tool":"echo","arguments":{"value":"alpha"}}'}}],
        },
    )
    result = attest(
        gateway, backend="openai-compatible", model="fixture", evidence_path=evidence,
        timeout_seconds=5, protocol_probes=True, identity=identity,
        identity_reader=lambda: identity,
    )
    assert "chat" in result["failed"] and "structured" in result["failed"]
    assert "tool_fallback" in result["unknown"]

    calls = 0

    def changing_identity():
        nonlocal calls
        calls += 1
        return _identity() if calls < 4 else BackendIdentity(
            **{**identity.to_dict(), "model_digest": "d" * 64},
        )

    with pytest.raises(ValueError, match="identity or transport changed"):
        attest(
            gateway, backend="openai-compatible", model="fixture",
            evidence_path=evidence, timeout_seconds=5, protocol_probes=True,
            identity=identity, identity_reader=changing_identity,
        )


def test_protocol_probes_refuse_transport_rotation_before_publishing(tmp_path):
    evidence = tmp_path / "capabilities.json"
    identity = _identity()

    def transport(_url, _payload, _headers, _timeout):
        gateway._transport = None
        return {"model": "fixture", "choices": [{"message": {"content": "hello"}}]}

    gateway = OpenAICompatibleGateway(
        OpenAICompatibleConfig("http://127.0.0.1:8080", model="fixture"),
        transport=transport,
    )
    with pytest.raises(ValueError, match="transport changed"):
        attest(
            gateway, backend="openai-compatible", model="fixture",
            evidence_path=evidence, timeout_seconds=5, protocol_probes=True,
            identity=identity, identity_reader=lambda: identity,
        )
    assert not evidence.exists()


def test_protocol_probes_reject_tool_json_that_the_runtime_lane_cannot_parse(tmp_path):
    identity = _identity()
    gateway = OpenAICompatibleGateway(
        OpenAICompatibleConfig("http://127.0.0.1:8080", model="fixture"),
        transport=lambda *_args: {
            "model": "fixture",
            "choices": [{"message": {"content": '{"tool":"echo","args":{"value":"alpha"}}'}}],
        },
    )
    result = attest(
        gateway, backend="openai-compatible", model="fixture",
        evidence_path=tmp_path / "capabilities.json", timeout_seconds=5,
        protocol_probes=True, identity=identity, identity_reader=lambda: identity,
    )
    assert "structured" in result["failed"]
    assert "tool_fallback" in result["unknown"]
    assert "tool_sequential" in result["unknown"]
    assert "tool_continuation" in result["unknown"]


def test_cli_protocol_probes_require_host_identity_file(tmp_path):
    evidence = tmp_path / "capabilities.json"
    with pytest.raises(SystemExit) as stopped:
        main(["--base-url", "http://127.0.0.1:8080", "--model", "fixture",
              "--protocol-probes", "--evidence", str(evidence)])
    assert stopped.value.code == 2
    assert not evidence.exists()


def test_cli_protocol_opt_in_reads_host_identity_and_publishes_only_checked_cases(
    tmp_path, monkeypatch, capsys,
):
    def transport(_url, payload, _headers, _timeout):
        prompt = payload["messages"][-1]["content"]
        if "value beta" in prompt:
            text = '{"tool":"echo","arguments":{"value":"beta"}}'
        elif "CONTINUED:alpha" in prompt:
            text = "CONTINUED:alpha"
        elif "Return only a JSON object" in prompt:
            text = '{"tool":"echo","arguments":{"value":"alpha"}}'
        else:
            text = "hello"
        return {"model": "fixture", "choices": [{"message": {"content": text}}]}

    # Instrument the real transport call path without contacting a provider.
    # This is a CLI wiring check, not live model-quality evidence.
    monkeypatch.setattr(OpenAICompatibleGateway, "_default_transport", staticmethod(transport))
    path = tmp_path / "identity.json"
    path.write_text(json.dumps(_identity().to_dict()), encoding="utf-8")
    evidence = tmp_path / "capabilities.json"
    assert main([
        "--base-url", "http://127.0.0.1:8080", "--model", "fixture",
        "--identity-file", str(path), "--protocol-probes",
        "--evidence", str(evidence),
    ]) == 0
    output = json.loads(capsys.readouterr().out)
    assert not output["identity_bound"] and output["identity_declared"]
    assert set(output["unknown"]) == {
        "cancellation", "tool_native", "tool_fallback",
        "tool_sequential", "tool_continuation",
    }
    assert RecentCapabilityEvidence(evidence).load("openai-compatible", "fixture").identity == _identity()
    assert RecentCapabilityEvidence(evidence).load("openai-compatible", "fixture").synthetic is True


def test_default_cli_cannot_certify_unrelated_identity_from_request_model_label(
    tmp_path, monkeypatch, capsys,
):
    def transport(_url, payload, _headers, _timeout):
        prompt = payload["messages"][-1]["content"]
        text = ('{"tool":"echo","continued":true}'
                if "Return only JSON" in prompt else "hello")
        # The normal gateway derives ModelResponse.model from the request, so
        # a matching response label says nothing about these declared digests.
        return {"model": "fixture", "choices": [{"message": {"content": text}}]}

    monkeypatch.setattr(OpenAICompatibleGateway, "_default_transport", staticmethod(transport))
    unrelated = BackendIdentity(**{**_identity().to_dict(), "model_digest": "d" * 64})
    identity_file = tmp_path / "other-deployment.json"
    identity_file.write_text(json.dumps(unrelated.to_dict()), encoding="utf-8")
    evidence_file = tmp_path / "capabilities.json"
    assert main([
        "--base-url", "http://127.0.0.1:8080", "--model", "fixture",
        "--identity-file", str(identity_file), "--evidence", str(evidence_file),
    ]) == 0
    output = json.loads(capsys.readouterr().out)
    assert set(output["passed"]) == {"chat", "structured", "cancellation"}
    assert output["synthetic"] is True and not output["identity_bound"]
    store = RecentCapabilityEvidence(evidence_file)
    assert store.load("openai-compatible", "fixture").identity == unrelated
    assert store.assess(
        "fixture", frozenset({BackendCapability.STRUCTURED}),
        backend="openai-compatible", identity=unrelated,
    ).state is EvidenceState.UNKNOWN


@pytest.mark.parametrize("endpoint", (
    "http://example.com:8080",
    "https://user:secret@example.com",
    "http://0.0.0.0:8080",
))
def test_cli_refuses_insecure_or_ambiguous_endpoints_even_with_cloud_flag(
    tmp_path, endpoint,
):
    with pytest.raises(SystemExit) as stopped:
        main([
            "--base-url", endpoint, "--model", "fixture", "--allow-cloud",
            "--dry-run", "--evidence", str(tmp_path / "evidence.json"),
        ])
    assert stopped.value.code == 2
    assert not (tmp_path / "evidence.json").exists()
