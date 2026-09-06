from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from sonder_runtime.application.compute_fabric.deployment_admission import (
    DeploymentAdmissionService,
    DeploymentResourceRequest,
)
from sonder_runtime.domain.common.errors import (
    CapacityExceeded,
    Conflict,
    DependencyUnavailable,
    NotFound,
)
from sonder_runtime.domain.model_deployment import ModelDeployment, ModelRank
from sonder_runtime.domain.worker_capacity import (
    CapacityReconciliation,
    CapacityReservation,
    CapacityReservationView,
    WorkerBudget,
)


def _deployment(*, revision: int = 1, reservation_group: str = "coding") -> ModelDeployment:
    return ModelDeployment(
        cluster_id="private-a",
        deployment_id="coding",
        revision=revision,
        backend="test-backend",
        backend_digest="a" * 64,
        model_bundle_digest="b" * 64,
        runtime_config_digest="c" * 64,
        context_tokens=8192,
        tensor_parallel=1,
        pipeline_parallel=2,
        reservation_group=reservation_group,
        ranks=(
            ModelRank(0, "host-1", "worker-1", "device-0"),
            ModelRank(1, "host-2", "worker-2", "device-0"),
        ),
    )


def _resources(deployment: ModelDeployment) -> tuple[DeploymentResourceRequest, ...]:
    return tuple(
        DeploymentResourceRequest(
            rank=rank,
            budget=WorkerBudget(rank.host_id, 2_000, max_jobs=2),
            memory_bytes=1_000,
        )
        for rank in deployment.ranks
    )


class FakeCapacity:
    def __init__(self, *, fail_rank: int | None = None, expired_job_ids: tuple[str, ...] = ()) -> None:
        self.fail_rank = fail_rank
        self.expired_job_ids = expired_job_ids
        self.events: list[tuple] = []
        self.reservations: dict[str, CapacityReservation] = {}

    def reserve_capacity(
        self,
        budget: WorkerBudget,
        job_id: str,
        request_sha256: str,
        memory_bytes: int | None,
        *,
        lease_seconds: int = 30,
    ) -> CapacityReservation:
        rank = int(job_id.rsplit("-", 1)[-1])
        self.events.append(("reserve", rank, budget, request_sha256, memory_bytes, lease_seconds))
        if self.fail_rank == rank:
            raise CapacityExceeded("test capacity exhausted")
        reservation = CapacityReservation(
            job_id,
            f"token-{rank}",
            memory_bytes or budget.memory_bytes,
            "2030-01-01T00:00:00+00:00",
        )
        self.reservations[job_id] = reservation
        return reservation

    def dispatch_capacity(self, job_id: str, token: str) -> None:
        self.events.append(("dispatch", job_id, token))

    def release_capacity(self, job_id: str) -> None:
        self.events.append(("release", job_id))

    def reconcile_capacity(self, *, now=None, limit: int = 1024) -> CapacityReconciliation:
        self.events.append(("reconcile", now, limit))
        observed = now or datetime(2030, 1, 1, tzinfo=timezone.utc)
        expired = tuple(
            CapacityReservationView(
                job_id,
                "host-1",
                "a" * 64,
                1_000,
                observed.isoformat(),
                "released",
                "f" * 64,
                "expired",
            )
            for job_id in self.expired_job_ids
        )
        return CapacityReconciliation(observed.isoformat(), expired, len(expired))


def test_admit_binds_immutable_manifest_to_every_rank_and_dispatches() -> None:
    deployment = _deployment()
    capacity = FakeCapacity()
    service = DeploymentAdmissionService(capacity, reservation_seconds=17)

    receipt = service.admit(deployment, _resources(deployment))

    assert receipt.deployment_digest == deployment.digest
    assert receipt.reservation_group == deployment.reservation_group
    assert len(receipt.reservations) == len(deployment.ranks)
    assert tuple(item.rank for item in receipt.reservations) == (0, 1)
    assert [event[0] for event in capacity.events] == [
        "reserve", "dispatch", "reserve", "dispatch"
    ]
    assert all(event[-1] == 17 for event in capacity.events if event[0] == "reserve")
    assert len({event[3] for event in capacity.events if event[0] == "reserve"}) == 2


def test_admit_routes_each_rank_to_its_configured_worker_capacity() -> None:
    deployment = _deployment()
    first = FakeCapacity()
    second = FakeCapacity()
    service = DeploymentAdmissionService({"host-1": first, "host-2": second})

    service.admit(deployment, _resources(deployment))

    assert [event[0] for event in first.events] == ["reserve", "dispatch"]
    assert [event[0] for event in second.events] == ["reserve", "dispatch"]


def test_admit_and_release_work_across_independent_sqlite_worker_authorities(tmp_path) -> None:
    from sonder_runtime.adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry

    deployment = _deployment()
    capacities = {
        host_id: SQLiteDurableJobRegistry(
            tmp_path / f"{host_id}.db",
            clock=lambda: "2030-01-01T00:00:00+00:00",
        )
        for host_id in ("host-1", "host-2")
    }
    service = DeploymentAdmissionService(capacities)

    receipt = service.admit(deployment, _resources(deployment))

    assert all(capacity.list_capacity()[0].state == "dispatched" for capacity in capacities.values())
    service.release(receipt)
    assert all(
        capacity.list_capacity(include_released=True)[0].state == "released"
        for capacity in capacities.values()
    )


def test_missing_host_capacity_is_refused_before_any_reservation() -> None:
    deployment = _deployment()
    capacity = FakeCapacity()
    service = DeploymentAdmissionService({"host-1": capacity})

    with pytest.raises(DependencyUnavailable, match="host-2"):
        service.admit(deployment, _resources(deployment))
    assert capacity.events == []


def test_admit_refuses_incomplete_or_mismatched_resources_before_reserving() -> None:
    deployment = _deployment()
    capacity = FakeCapacity()
    service = DeploymentAdmissionService(capacity)

    with pytest.raises(ValueError, match="exactly one resource request"):
        service.admit(deployment, _resources(deployment)[:1])
    assert capacity.events == []

    wrong_host = replace(
        _resources(deployment)[0],
        budget=WorkerBudget("host-other", 2_000, max_jobs=2),
    )
    with pytest.raises(ValueError, match="host"):
        service.admit(deployment, (wrong_host, _resources(deployment)[1]))
    assert capacity.events == []


def test_admit_rolls_back_active_reservations_when_dispatch_fails() -> None:
    deployment = _deployment()

    class DispatchFailure(FakeCapacity):
        def dispatch_capacity(self, job_id: str, token: str) -> None:
            self.events.append(("dispatch", job_id, token))
            raise CapacityExceeded("dispatch lost")

    capacity = DispatchFailure()
    service = DeploymentAdmissionService(capacity)

    with pytest.raises(CapacityExceeded, match="dispatch lost"):
        service.admit(deployment, _resources(deployment))

    # A failed admission never returns a usable receipt and asks the worker to
    # clean up every attempted reservation.  The real worker retains an
    # undispatched lease until bounded expiry, so no false release is claimed.
    assert [event[0] for event in capacity.events] == ["reserve", "dispatch", "release"]


def test_release_requires_the_exact_live_receipt_and_releases_all_ranks() -> None:
    deployment = _deployment()
    capacity = FakeCapacity()
    service = DeploymentAdmissionService(capacity)
    receipt = service.admit(deployment, _resources(deployment))

    released = service.release(receipt)

    assert released == receipt
    assert [event[0] for event in capacity.events if event[0] == "release"] == [
        "release", "release"
    ]
    with pytest.raises(NotFound, match="admission"):
        service.release(receipt)


def test_release_rejects_forged_or_cross_group_receipt() -> None:
    deployment = _deployment()
    capacity = FakeCapacity()
    service = DeploymentAdmissionService(capacity)
    receipt = service.admit(deployment, _resources(deployment))

    with pytest.raises(NotFound, match="admission"):
        service.release(replace(receipt, plan_digest="0" * 64))


def test_same_plan_is_idempotent_but_changed_plan_cannot_double_admit_group() -> None:
    deployment = _deployment()
    resources = _resources(deployment)
    capacity = FakeCapacity()
    service = DeploymentAdmissionService(capacity)

    first = service.admit(deployment, resources)
    assert service.admit(deployment, resources) == first
    assert len([event for event in capacity.events if event[0] == "reserve"]) == 2

    changed = replace(resources[0], memory_bytes=1_001)
    with pytest.raises(Conflict, match="reservation group"):
        service.admit(deployment, (changed, resources[1]))


def test_reconcile_delegates_bounded_worker_reconciliation() -> None:
    deployment = _deployment()
    capacity = FakeCapacity()
    service = DeploymentAdmissionService(capacity)
    service.admit(deployment, _resources(deployment))
    now = datetime(2030, 1, 2, tzinfo=timezone.utc)

    result = service.reconcile(now=now, limit=9)

    assert result.observed_at == now.isoformat()
    assert result.expired_job_ids == ()
    assert capacity.events[-1] == ("reconcile", now, 9)


def test_reconcile_marks_an_active_plan_when_worker_reports_an_expired_rank() -> None:
    deployment = _deployment()
    capacity = FakeCapacity()
    service = DeploymentAdmissionService(capacity)
    receipt = service.admit(deployment, _resources(deployment))
    capacity.expired_job_ids = (receipt.reservations[1].job_id,)

    result = service.reconcile()

    assert result.expired_job_ids == (receipt.reservations[1].job_id,)
    assert result.affected_plan_digests == (receipt.plan_digest,)
