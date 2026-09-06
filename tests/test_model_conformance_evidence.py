from __future__ import annotations

from dataclasses import replace

import pytest

from sonder_runtime.domain.model_conformance import (
    BackendConformanceEvidence,
    ConformanceError,
    evaluate_conformance,
    verify_deployment_conformance,
)
from sonder_runtime.domain.model_deployment import ModelDeployment, ModelRank


OPS = (
    "generation", "streaming", "cancellation", "health",
    "transport_failure", "replacement",
)


def _evidence(*, verified=OPS, supported=OPS, deployment_digest="a" * 64):
    return BackendConformanceEvidence(
        backend_id="backend-1",
        deployment_digest=deployment_digest,
        supported_operations=tuple(supported),
        verified_operations=tuple(verified),
        evidence_ids=tuple("evidence-%d" % index for index in range(len(verified))),
        observed_at="2026-09-05T12:00:00+00:00",
    )


def _deployment(*, hosts=("host-1",), digest_override=None):
    ranks = tuple(
        ModelRank(index, host, "worker-%d" % index, "device-%d" % index)
        for index, host in enumerate(hosts)
    )
    deployment = ModelDeployment(
        cluster_id="cluster-1", deployment_id="deploy-1", revision=1,
        backend="backend-1", backend_digest="b" * 64,
        model_bundle_digest="c" * 64, runtime_config_digest="d" * 64,
        context_tokens=4096, tensor_parallel=len(ranks), pipeline_parallel=1,
        reservation_group="group-1", ranks=ranks,
    )
    return deployment, digest_override or deployment.digest


def test_evidence_is_bounded_unique_and_digest_stable():
    evidence = _evidence()
    assert len(evidence.digest) == 64
    assert evidence.digest == replace(evidence).digest
    with pytest.raises(ConformanceError, match="duplicate"):
        _evidence(supported=("generation", "generation"))
    with pytest.raises(ConformanceError, match="operation"):
        _evidence(verified=("unknown",))
    with pytest.raises(ConformanceError, match="timezone"):
        BackendConformanceEvidence(
            "backend-1", "a" * 64, OPS, OPS, ("proof",), "2026-09-05T12:00:00"
        )


def test_conformance_reports_missing_and_unsupported_operations_without_claiming_readiness():
    evidence = _evidence(verified=("generation",), supported=("generation", "health"))
    result = evaluate_conformance(evidence)
    assert result.accepted is False
    assert result.reason == "required_operations_unverified"
    assert "streaming" in result.missing_operations
    assert result.unsupported_operations == (
        "cancellation", "replacement", "streaming", "transport_failure",
    )


def test_single_host_backend_is_ready_only_after_all_required_checks():
    result = evaluate_conformance(_evidence())
    assert result.accepted is True
    assert result.reason == "required_operations_verified"
    assert result.missing_operations == ()


def test_multihost_deployment_requires_shard_loss_recovery_and_exact_manifest_binding():
    deployment, digest = _deployment(hosts=("host-1", "host-2"))
    no_recovery = _evidence(deployment_digest=digest)
    result = verify_deployment_conformance(deployment, no_recovery)
    assert result.accepted is False
    assert result.reason == "distributed_operations_unverified"
    assert result.missing_operations == ("shard_loss_recovery",)

    complete = _evidence(
        deployment_digest=digest,
        supported=OPS + ("shard_loss_recovery",),
        verified=OPS + ("shard_loss_recovery",),
    )
    assert verify_deployment_conformance(deployment, complete).accepted
    wrong = _evidence(deployment_digest="e" * 64)
    assert verify_deployment_conformance(deployment, wrong).reason == "deployment_digest_mismatch"


def test_single_host_does_not_infer_distribution_from_extra_capability():
    deployment, digest = _deployment()
    evidence = _evidence(
        deployment_digest=digest,
        supported=OPS + ("shard_loss_recovery",),
        verified=OPS + ("shard_loss_recovery",),
    )
    result = verify_deployment_conformance(deployment, evidence)
    assert result.accepted
    assert result.distributed is False
