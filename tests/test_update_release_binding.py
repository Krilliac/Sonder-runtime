"""The authorized update target and the activated release must be one identity.

UpdateTarget carried release_id/version independently of its signed evidence,
and activation forwarded a separate ActivationRequest that was only compared
with the evidence.  An authority that approved release A could therefore end
up activating a validly signed release B.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest

from sonder_runtime.application.updates import UpdateTarget
from sonder_runtime.application.updates.release_evidence import (
    ActivationRequest, ReleaseEvidencePackage, RollbackCompatibility,
    SbomComponent, SignedReleaseManifest, TestEvidence,
)
from tests.test_update_application_service import (
    Authority, Backup, Ports, digest, request, service, target,
)

NOW = datetime(2026, 8, 21, tzinfo=timezone.utc)


def _other_release_evidence(artifact: bytes) -> ReleaseEvidencePackage:
    manifest = SignedReleaseManifest(
        "rel-3", "3.0.0", (("bundle", digest(artifact)),), "release-key", "valid",
        (("python", "3.12"),),
    )
    return ReleaseEvidencePackage.build(
        manifest=manifest, sbom=(SbomComponent("runtime", "3.0.0"),),
        tests=(TestEvidence("focused", 1),), migrations=(),
        rollback=RollbackCompatibility(("rel-1",), True, "restore-proof"),
    )


def test_target_release_id_must_match_signed_manifest():
    good, artifact = target()
    with pytest.raises(ValueError, match="release"):
        UpdateTarget(good.update_id, "rel-2", good.version, good.artifact_digest,
                     _other_release_evidence(artifact), good.metadata)


def test_target_version_must_match_signed_manifest():
    good, _artifact = target()
    with pytest.raises(ValueError, match="version"):
        dataclasses.replace(good, version="9.9.9")


def test_activation_request_must_name_the_authorized_target_release():
    update_target, artifact = target()
    backup = Backup()
    app, pointer, helper = service(Ports(artifact), backup, Authority())
    prepared = app.prepare(update_target, now=NOW)
    wrong = ActivationRequest("linux", "rel-1", "rel-3",
                              update_target.evidence.package_digest, "nonce")

    with pytest.raises(ValueError, match="target"):
        app.activate(prepared, activation_id="a1", request=wrong,
                     observed_dependencies={"python": "3.12"})

    assert pointer.value == "rel-1"
    assert helper.calls == []


def test_activation_request_must_carry_the_authorized_evidence_digest():
    update_target, artifact = target()
    app, pointer, helper = service(Ports(artifact), Backup(), Authority())
    prepared = app.prepare(update_target, now=NOW)
    other = _other_release_evidence(artifact)
    wrong = ActivationRequest("linux", "rel-1", "rel-2", other.package_digest, "nonce")

    with pytest.raises(ValueError, match="evidence"):
        app.activate(prepared, activation_id="a1", request=wrong,
                     observed_dependencies={"python": "3.12"})

    assert pointer.value == "rel-1"
    assert helper.calls == []


def test_matching_request_still_activates():
    update_target, artifact = target()
    app, pointer, _helper = service(Ports(artifact), Backup(), Authority())
    prepared = app.prepare(update_target, now=NOW)
    app.activate(prepared, activation_id="a1", request=request(update_target),
                 observed_dependencies={"python": "3.12"})
    assert pointer.value == "rel-2"


class _Pointer:
    def __init__(self): self.value = "rel-1"
    def current(self): return self.value
    def commit(self, value): self.value = value


class _Helper:
    def __init__(self): self.calls = []
    def activate(self, request): self.calls.append(request.target_release)
    def rollback(self, request): self.calls.append("rollback")


def _health_checked_state():
    from sonder_runtime.application.updates import BoundedUpdateState
    from tests.test_update_application_service import verifier

    update_target, artifact = target()
    ports = Ports(artifact)
    state = BoundedUpdateState(update_target)
    state.download(ports)
    state.verify(verifier, now=NOW)
    state.stage(ports, artifact)
    state.health_gate(ports)
    return state, update_target, artifact


@pytest.mark.parametrize("field", ["target_release", "release_evidence_digest"])
def test_state_machine_refuses_a_request_for_a_different_release(field):
    """Any activator (not only the durable coordinator) is bound to the target."""
    from sonder_runtime.application.updates.release_evidence import (
        AtomicReleaseActivator,
    )

    state, update_target, artifact = _health_checked_state()
    good = request(update_target)
    other = _other_release_evidence(artifact)
    wrong = dataclasses.replace(
        good,
        **{field: "rel-3" if field == "target_release" else other.package_digest},
    )
    pointer, helper = _Pointer(), _Helper()

    with pytest.raises(ValueError):
        state.activate(AtomicReleaseActivator(pointer, helper), wrong)

    assert pointer.value == "rel-1"
    assert helper.calls == []
    assert state.snapshot.phase.value == "health_checked"
