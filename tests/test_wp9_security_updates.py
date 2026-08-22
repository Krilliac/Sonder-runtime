import hashlib

import pytest

from sonder_runtime.application.security.secrets_and_fuzz import CredentialScanner, DecoderFuzzHarness
from sonder_runtime.application.updates.activation import SignedManifest, UpdateActivation


def test_secret_scanner_finds_common_credentials_and_redacts_values():
    findings = CredentialScanner().scan("api_key=super-secret-value\nAKIA1234567890ABCDEF")
    assert {item.kind for item in findings} == {"credential_assignment", "aws_access_key"}
    assert all("super-secret" not in item.redacted and "AKIA" not in item.redacted for item in findings)


def test_fuzz_harness_is_bounded_and_records_decoder_faults():
    def decoder(payload):
        if payload == b"bad":
            raise ValueError("malformed")
    report = DecoderFuzzHarness(decoder, max_cases=2, max_input_bytes=3).run([b"bad", b"ok", b"too-long"])
    assert report.cases == 2
    assert report.rejected == 0
    assert report.failures == ("ValueError",)


def test_signed_activation_health_gate_and_rollback():
    artifact_a, artifact_b = b"a", b"b"
    digest_a = hashlib.sha256(artifact_a).hexdigest()
    digest_b = hashlib.sha256(artifact_b).hexdigest()
    verifier = lambda payload, signature, signer: signature == payload.decode() + ":sig" and signer == "release"
    manager = UpdateActivation(verifier, lambda manifest: manifest.version != "bad")
    a = SignedManifest("1", digest_a, "release", "1\n" + digest_a + "\nrelease:sig")
    b = SignedManifest("2", digest_b, "release", "2\n" + digest_b + "\nrelease:sig")
    manager.activate(a, artifact_a)
    manager.activate(b, artifact_b)
    assert manager.rollback().manifest.version == "1"
    with pytest.raises(ValueError):
        manager.activate(SignedManifest("x", digest_a, "release", "bad"), artifact_b)
