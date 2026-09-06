from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sonder_runtime.domain.model_conformance import (
    BackendConformanceEvidence,
    evaluate_conformance,
    verify_deployment_conformance,
)
from sonder_runtime.domain.model_deployment import ModelDeployment, ModelRank
from sonder_runtime.domain.model_shard_recovery import (
    RecoveryState,
    ShardRecoveryCandidate,
    ShardRecoveryError,
    plan_shard_replacement,
)


OPS = (
    "cancellation", "generation", "health", "replacement", "streaming",
    "transport_failure", "shard_loss_recovery",
)


def _deployment() -> ModelDeployment:
    return ModelDeployment(
        cluster_id="cluster-1", deployment_id="deploy-1", revision=1,
        backend="backend-1", backend_digest="b" * 64,
        model_bundle_digest="c" * 64, runtime_config_digest="d" * 64,
        context_tokens=4096, tensor_parallel=2, pipeline_parallel=1,
        reservation_group="group-1",
        ranks=(
            ModelRank(0, "host-1", "worker-1", "device-1"),
            ModelRank(1, "host-2", "worker-2", "device-2"),
        ),
    )


def _conformance(deployment: ModelDeployment):
    evidence = BackendConformanceEvidence(
        backend_id="backend-1", deployment_digest=deployment.digest,
        supported_operations=OPS, verified_operations=OPS,
        evidence_ids=tuple("e-%d" % i for i in range(len(OPS))),
        observed_at=datetime.now(timezone.utc).isoformat(),
    )
    return verify_deployment_conformance(deployment, evidence)


def _candidate(deployment: ModelDeployment, *, rank=1, host="host-3", device="device-3", **changes):
    values = dict(
        rank=rank, host_id=host, worker_id="worker-3", device_id=device,
        backend_id=deployment.backend, backend_digest=deployment.backend_digest,
        model_bundle_digest=deployment.model_bundle_digest,
        runtime_config_digest=deployment.runtime_config_digest,
        reservation_group=deployment.reservation_group,
        capacity_tokens=deployment.context_tokens, healthy=True,
    )
    values.update(changes)
    return ShardRecoveryCandidate(**values)


def test_healthy_exact_candidate_is_admissible_but_does_not_claim_kv_cache_recovery():
    deployment = _deployment()
    decision = plan_shard_replacement(
        deployment, lost_ranks=(1,), candidates=(_candidate(deployment),),
        conformance=_conformance(deployment),
    )
    assert decision.state is RecoveryState.READY
    assert decision.replacement_ranks[0].rank == 1
    assert decision.replacement_ranks[0].host_id == "host-3"
    assert decision.kv_cache_recovery_verified is False
    assert decision.application_replay_required is True


def test_missing_or_non_distributed_conformance_pauses_recovery():
    deployment = _deployment()
    paused = plan_shard_replacement(deployment, lost_ranks=(1,), candidates=(_candidate(deployment),), conformance=None)
    assert paused.state is RecoveryState.PAUSED
    assert paused.reason == "distributed_backend_conformance_required"

    single = _conformance(deployment)
    single = single.__class__(single.accepted, single.reason, single.required_operations, single.missing_operations, single.unsupported_operations, distributed=False)
    paused = plan_shard_replacement(deployment, lost_ranks=(1,), candidates=(_candidate(deployment),), conformance=single)
    assert paused.state is RecoveryState.PAUSED
    assert paused.reason == "distributed_backend_conformance_required"


@pytest.mark.parametrize(
    "changes, reason",
    [
        ({"backend_id": "other"}, "candidate_backend_mismatch"),
        ({"model_bundle_digest": "e" * 64}, "candidate_artifact_mismatch"),
        ({"reservation_group": "other"}, "candidate_reservation_group_mismatch"),
        ({"capacity_tokens": 1024}, "candidate_capacity_insufficient"),
        ({"healthy": False}, "candidate_unhealthy"),
    ],
)
def test_candidate_mismatch_pauses_without_provider_side_effect(changes, reason):
    deployment = _deployment()
    decision = plan_shard_replacement(
        deployment, lost_ranks=(1,), candidates=(_candidate(deployment, **changes),),
        conformance=_conformance(deployment),
    )
    assert decision.state is RecoveryState.PAUSED
    assert decision.reason == reason
    assert decision.replacement_ranks == ()


def test_loss_and_manifest_identity_are_bounded_and_fail_closed():
    deployment = _deployment()
    with pytest.raises(ShardRecoveryError, match="lost_ranks"):
        plan_shard_replacement(deployment, lost_ranks=(), candidates=(), conformance=_conformance(deployment))
    with pytest.raises(ShardRecoveryError, match="deployment_digest"):
        plan_shard_replacement(
            deployment, lost_ranks=(1,), candidates=(_candidate(deployment),),
            conformance=_conformance(deployment), deployment_digest="a" * 64,
        )


def test_candidate_cannot_reuse_a_surviving_physical_device_or_replace_the_wrong_rank():
    deployment = _deployment()
    same_device = plan_shard_replacement(
        deployment, lost_ranks=(1,), candidates=(_candidate(deployment, host="host-1", device="device-1"),),
        conformance=_conformance(deployment),
    )
    assert same_device.state is RecoveryState.PAUSED
    assert same_device.reason == "candidate_device_conflicts_with_survivor"

    wrong_rank = plan_shard_replacement(
        deployment, lost_ranks=(1,), candidates=(_candidate(deployment, rank=0),),
        conformance=_conformance(deployment),
    )
    assert wrong_rank.state is RecoveryState.PAUSED
    assert wrong_rank.reason == "candidate_rank_mismatch"
