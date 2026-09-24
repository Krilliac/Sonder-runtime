from __future__ import annotations

import json

import pytest

from scripts.backend_attest import _read_host_identity, attest, main
from sonder_runtime.adapters.inference.openai_compat_gateway import (
    OpenAICompatibleConfig,
    OpenAICompatibleGateway,
)
from sonder_runtime.domain.routing.backend_conformance import BackendIdentity


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
