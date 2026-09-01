from __future__ import annotations

from dataclasses import replace

import pytest

from sonder_runtime.application.compute_fabric.jobs import (
    ArgumentPolicy,
    JobCatalogEntry,
    RemoteArtifactReceipt,
    RemoteJobEnvelope,
)
from sonder_runtime.domain.compute_fabric import WorkloadKind


def _envelope(**changes) -> RemoteJobEnvelope:
    values = dict(
        controller_job_id="controller-job",
        idempotency_key="idem-1",
        workload=WorkloadKind.TEST,
        catalog_entry_id="pytest",
        workspace_mapping="sonder",
        relative_cwd="tests",
        arguments=("test_api.py",),
        environment=(("PYTEST_ADDOPTS", "-q"),),
        deadline_seconds=60,
        idempotent=True,
    )
    values.update(changes)
    return RemoteJobEnvelope.create(**values)


def test_envelope_digest_is_stable_and_every_material_field_is_bound() -> None:
    first = _envelope()
    assert first == _envelope()
    assert len(first.request_sha256) == 64
    assert first.request_sha256 != _envelope(arguments=("different.py",)).request_sha256
    with pytest.raises(ValueError, match="digest"):
        replace(first, request_sha256="0" * 64)


@pytest.mark.parametrize(
    "changes",
    (
        {"relative_cwd": "../outside"},
        {"relative_cwd": "C:\\outside"},
        {"arguments": ("../secret",)},
        {"environment": (("BAD-NAME", "x"),)},
        {"deadline_seconds": 0},
        {"arguments": tuple("x" for _ in range(65))},
    ),
)
def test_envelope_rejects_traversal_and_unbounded_fields(changes) -> None:
    with pytest.raises(ValueError):
        _envelope(**changes)


def test_catalog_is_worker_owned_and_bounded() -> None:
    entry = JobCatalogEntry(
        entry_id="pytest",
        workload=WorkloadKind.TEST,
        program="python",
        fixed_args=("-m", "pytest"),
        argument_policy=ArgumentPolicy.RELATIVE_PATHS_AND_TEST_SELECTORS,
        environment_allowlist=frozenset({"PYTEST_ADDOPTS"}),
        workspace_mappings=frozenset({"sonder"}),
    )
    assert entry.argv_for(("tests/test_api.py",)) == (
        "python", "-m", "pytest", "tests/test_api.py",
    )
    with pytest.raises(ValueError, match="environment"):
        entry.environment_for((("SECRET", "value"),))


def test_artifact_receipt_requires_content_digest_and_relative_name() -> None:
    receipt = RemoteArtifactReceipt("report.json", 12, "application/json", "a" * 64)
    assert receipt.size_bytes == 12
    with pytest.raises(ValueError):
        RemoteArtifactReceipt("../report", 12, "application/json", "a" * 64)
