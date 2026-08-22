from __future__ import annotations

from contextlib import contextmanager

import pytest

from sonder_runtime.application.training.attended_execution import (
    AttendedTrainingExecutionService,
    AttendedTrainingRequest,
)
from sonder_runtime.domain.common.errors import Conflict, Forbidden
from sonder_runtime.domain.training.reproducible import (
    BaseModelManifest,
    DatasetManifest,
    DependencyManifest,
    EvaluationManifest,
    Provenance,
    ReproducibleTrainingManifest,
)


def manifest():
    provenance = Provenance("test", "revision")
    dataset = DatasetManifest("d1", "dataset-digest", 1, "v1", provenance)
    return ReproducibleTrainingManifest(
        dataset=dataset,
        base_model=BaseModelManifest("model", "rev", "model-digest", "tokenizer", provenance),
        dependencies=(DependencyManifest("trainer", "1", "lock", "dep-digest"),),
        evaluation=EvaluationManifest.from_mapping(
            "smoke", "1", "dataset-digest", {"quality": 1.0}, provenance
        ),
    )


class Fakes:
    def __init__(self, *, exit_code=0, deployment_error=None, recovery_error=None):
        self.events = []
        self.exit_code = exit_code
        self.deployment_error = deployment_error
        self.recovery_error = recovery_error
        self.restored = False
        self.rolled_back = False

    class Process:
        def __init__(self, owner): self.owner = owner
        def launch(self, request):
            self.owner.events.append(("launch", request.run_id, request.manifest_digest))
            from sonder_runtime.application.ports.training import TrainingLaunchResult
            return TrainingLaunchResult(request.run_id, self.owner.exit_code, "adapter-digest", "failed")

    class Lock:
        def __init__(self, owner): self.owner = owner
        @contextmanager
        def acquire(self, run_id):
            self.owner.events.append(("lock", run_id, "acquired"))
            try:
                yield
            finally:
                self.owner.events.append(("lock", run_id, "released"))

    class Verifier:
        def verify(self, evidence): return evidence.signature == "valid"

    class Journal:
        def __init__(self, owner): self.owner = owner
        def append(self, event): self.owner.events.append(("journal", event.phase))

    class Policy:
        def __init__(self, owner): self.owner = owner
        def reserve(self, run_id, artifact_digest):
            self.owner.events.append(("reserve", artifact_digest))
            return "reservation"
        def commit(self, reservation): self.owner.events.append(("commit", reservation))
        def restore(self, reservation):
            self.owner.restored = True
            self.owner.events.append(("restore", reservation))
            if self.owner.recovery_error: raise self.owner.recovery_error

    class Deployment:
        def __init__(self, owner): self.owner = owner
        def activate(self, artifact_id, *, attended=False):
            assert attended
            self.owner.events.append(("activate", artifact_id))
            if self.owner.deployment_error: raise self.owner.deployment_error
            return "route"
        def rollback(self, *, attended=False, reason=""):
            assert attended
            self.owner.rolled_back = True
            self.owner.events.append(("rollback", reason))

    def service(self):
        return AttendedTrainingExecutionService(
            process=self.Process(self), lock=self.Lock(self), verifier=self.Verifier(),
            journal=self.Journal(self), policy=self.Policy(self), deployment=self.Deployment(self),
        )


def request(m, *, attended=True, signature="valid"):
    return AttendedTrainingRequest("run-1", ("python", "qlora_train.py"), m, signature, attended)


def test_boundary_requires_attendance_before_any_side_effect():
    fakes = Fakes()
    with pytest.raises(Forbidden):
        fakes.service().execute(request(manifest(), attended=False))
    assert fakes.events == []


def test_invalid_signed_manifest_fails_before_lock_or_launch():
    fakes = Fakes()
    with pytest.raises(Forbidden):
        fakes.service().execute(request(manifest(), signature="invalid"))
    assert fakes.events == []


def test_success_sequences_lock_launch_policy_health_gate_and_journal():
    fakes = Fakes()
    result = fakes.service().execute(request(manifest()))
    assert result.adapter_digest == "adapter-digest"
    assert [event[0] for event in fakes.events] == [
        "lock", "journal", "launch", "reserve", "journal", "activate", "commit", "journal", "lock"
    ]


def test_failed_process_is_journaled_and_never_mutates_policy():
    fakes = Fakes(exit_code=1)
    with pytest.raises(Conflict):
        fakes.service().execute(request(manifest()))
    assert not any(event[0] == "reserve" for event in fakes.events)
    assert ("journal", "failed") in fakes.events


def test_activation_failure_restores_policy_and_attempts_attended_rollback():
    fakes = Fakes(deployment_error=RuntimeError("health gate"))
    with pytest.raises(RuntimeError, match="health gate"):
        fakes.service().execute(request(manifest()))
    assert fakes.restored
    assert fakes.rolled_back
    assert ("journal", "rolled_back") in fakes.events


def test_recovery_failure_is_recorded_fail_closed():
    fakes = Fakes(
        deployment_error=RuntimeError("health gate"),
        recovery_error=RuntimeError("ollama unavailable"),
    )
    with pytest.raises(RuntimeError, match="health gate"):
        fakes.service().execute(request(manifest()))
    assert ("journal", "recovery_required") in fakes.events
