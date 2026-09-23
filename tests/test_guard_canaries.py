from dataclasses import replace

import pytest

from sonder_runtime.application.compute_fabric.deployment_admission import DeploymentAdmissionService
from sonder_runtime.domain.common.errors import CapacityExceeded
from tests.test_model_deployment_admission import FakeCapacity, _deployment, _resources


def test_public_deployment_admission_enforces_active_plan_cap_before_reserving():
    capacity = FakeCapacity()
    service = DeploymentAdmissionService(capacity, max_active_admissions=1)
    first = _deployment(reservation_group="first")
    second = replace(_deployment(reservation_group="second"), deployment_id="second")

    service.admit(first, _resources(first))
    before_rejection = tuple(capacity.events)
    assert any(event[0] == "reserve" for event in before_rejection)
    with pytest.raises(CapacityExceeded, match="capacity is full"):
        service.admit(second, _resources(second))
    assert tuple(capacity.events) == before_rejection
