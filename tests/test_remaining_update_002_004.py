from __future__ import annotations

from hashlib import sha256

import pytest

from sonder_runtime.application.updates.release_evidence import (
    ActivationRecoveryError,
    ActivationRequest,
    AtomicReleaseActivator,
    SealedRuntimeContract,
    SignedReleaseManifest,
)


def _digest(value: bytes = b"evidence") -> str:
    return sha256(value).hexdigest()


def test_runtime_contract_rejects_missing_extra_and_tampered_entries():
    contract = SealedRuntimeContract.seal({"python": "3.12", "tuf": "3.0"})
    contract.verify_exact({"python": "3.12", "tuf": "3.0"})
    with pytest.raises(ValueError, match="mismatch"):
        contract.verify_exact({"python": "3.12"})
    with pytest.raises(ValueError, match="mismatch"):
        contract.verify_exact({"python": "3.12", "tuf": "3.0", "extra": "1"})
    tampered = SealedRuntimeContract(contract.dependencies, _digest(b"wrong"))
    with pytest.raises(ValueError, match="digest"):
        tampered.verify_exact({"python": "3.12", "tuf": "3.0"})


def test_manifest_contract_is_canonical_and_exact_before_activation():
    manifest = SignedReleaseManifest(
        "rel-2", "2.0.0", (("bundle", _digest()),), "release-key", "sig",
        (("python", "3.12"), ("tuf", "3.0")),
    )
    assert SealedRuntimeContract.from_manifest(manifest.runtime_contract).dependencies == (
        ("python", "3.12"), ("tuf", "3.0")
    )
    with pytest.raises(ValueError, match="mismatch"):
        SealedRuntimeContract.from_manifest(manifest.runtime_contract).verify_exact(
            {"python": "3.12", "tuf": "3.0", "openssl": "3.2"}
        )


def test_helper_process_request_is_platform_neutral_and_does_not_execute():
    request = ActivationRequest(
        "windows", "rel-1", "rel-2", _digest(), "nonce",
        ("sonder-update-helper", "activate", "rel-2"),
    )
    assert request.helper_argv == ("sonder-update-helper", "activate", "rel-2")
    with pytest.raises(ValueError, match="unsupported"):
        ActivationRequest("android", "rel-1", "rel-2", _digest(), "nonce")


class _Pointer:
    def __init__(self, value: str = "rel-1", fail_restore: bool = False) -> None:
        self.value = value
        self.fail_restore = fail_restore

    def current(self) -> str:
        return self.value

    def commit(self, target_release: str) -> None:
        if self.fail_restore and target_release == "rel-1":
            raise OSError("pointer unavailable")
        self.value = target_release


class _Helper:
    def __init__(self, fail_activate: bool = False, fail_rollback: bool = False) -> None:
        self.fail_activate = fail_activate
        self.fail_rollback = fail_rollback
        self.calls: list[str] = []

    def activate(self, request: ActivationRequest) -> None:
        self.calls.append("activate")
        if self.fail_activate:
            raise RuntimeError("activation failed")

    def rollback(self, request: ActivationRequest) -> None:
        self.calls.append("rollback")
        if self.fail_rollback:
            raise RuntimeError("rollback failed")


class _Sink:
    def __init__(self) -> None:
        self.rows = []

    def record(self, evidence) -> None:
        self.rows.append(evidence)


def _request() -> ActivationRequest:
    return ActivationRequest("linux", "rel-1", "rel-2", _digest(), "nonce")


def test_failed_activation_restores_known_good_release_and_records_standalone_evidence():
    pointer, helper, sink = _Pointer(), _Helper(fail_activate=True), _Sink()
    with pytest.raises(RuntimeError, match="activation failed"):
        AtomicReleaseActivator(pointer, helper, sink).activate(_request())
    assert pointer.value == "rel-1"
    assert helper.calls == ["activate", "rollback"]
    assert sink.rows[0].pointer_restored
    assert sink.rows[0].digest


def test_incomplete_recovery_is_explicit_and_not_hidden_by_failed_runtime():
    pointer, helper, sink = _Pointer(fail_restore=True), _Helper(fail_activate=True, fail_rollback=True), _Sink()
    with pytest.raises(ActivationRecoveryError) as caught:
        AtomicReleaseActivator(pointer, helper, sink).activate(_request())
    assert not caught.value.evidence.pointer_restored
    assert caught.value.evidence.error_types == ("RuntimeError", "OSError")
    assert sink.rows[0] is caught.value.evidence
