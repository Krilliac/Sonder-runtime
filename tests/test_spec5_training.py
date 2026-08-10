"""SPEC-5 WP8 — Training and immutable deployment tests.

Covers:
- No autonomous training start
- Dataset locking lifecycle
- Resume uses same dataset digest
- Immutable model identity after creation
- Candidate cannot activate itself (only DeploymentService)
- trust_remote_code=False enforced
- Deployment records previous model
- Rollback via DeploymentService
"""
from __future__ import annotations

import pytest

from sonder_runtime.domain.training.models import (
    DatasetIdentity,
    Deployment,
    ModelIdentity,
    TrainingPhase,
    TrainingRun,
)
from sonder_runtime.domain.common.errors import Forbidden, InvalidInput
from sonder_runtime.application.training.training_service import (
    DeploymentService,
    TrainingService,
)


class InMemoryTrainingStore:
    def __init__(self):
        self._runs: dict[str, TrainingRun] = {}

    def create_run(self, run_id, base_model, base_revision):
        run = TrainingRun(
            id=run_id,
            phase=TrainingPhase.PLANNED,
            base_model=base_model,
            base_revision=base_revision,
        )
        self._runs[run_id] = run
        return run

    def get_run(self, run_id):
        return self._runs.get(run_id)

    def save_run(self, run):
        self._runs[run.id] = run


class InMemoryDeploymentStore:
    def __init__(self):
        self._deployments: list[Deployment] = []
        self._rolled_back: set[str] = set()

    def record_deployment(self, deployment):
        self._deployments.append(deployment)

    def get_active(self):
        for d in reversed(self._deployments):
            if d.run_id not in self._rolled_back:
                return d
        return None

    def rollback(self, run_id):
        self._rolled_back.add(run_id)
        return self.get_active()


DATASET_A = DatasetIdentity(digest="sha256:aaa", row_count=1000)
DATASET_B = DatasetIdentity(digest="sha256:bbb", row_count=500)


@pytest.fixture
def store():
    return InMemoryTrainingStore()


@pytest.fixture
def svc(store):
    return TrainingService(store)


def _full_lifecycle(svc, store, run_id="r1"):
    """Drive a run through the full lifecycle up to CANDIDATE."""
    run = svc.start(run_id, "qwen3:8b", "abc123")
    svc.lock_dataset(run_id, DATASET_A)
    svc.resume(run_id)
    svc.complete_training(run_id, "adapter_hash_1")
    svc.record_evaluation(run_id, 0.85)
    return store.get_run(run_id)


class TestTrainingService:
    def test_autonomous_start_forbidden(self, svc):
        with pytest.raises(Forbidden, match="autonomous"):
            svc.start("r1", "model", "rev", attended=False)

    def test_attended_start_succeeds(self, svc):
        run = svc.start("r1", "qwen3:8b", "abc123")
        assert run.phase == TrainingPhase.PLANNED
        assert run.attended is True

    def test_trust_remote_code_false(self, svc):
        run = svc.start("r1", "qwen3:8b", "abc123")
        assert run.trust_remote_code is False

    def test_lock_dataset(self, svc):
        svc.start("r1", "m", "r")
        run = svc.lock_dataset("r1", DATASET_A)
        assert run.phase == TrainingPhase.DATASET_LOCKED
        assert run.dataset == DATASET_A

    def test_lock_dataset_wrong_phase(self, svc):
        svc.start("r1", "m", "r")
        svc.lock_dataset("r1", DATASET_A)
        with pytest.raises(InvalidInput, match="PLANNED"):
            svc.lock_dataset("r1", DATASET_B)

    def test_resume_without_dataset_fails(self, svc):
        svc.start("r1", "m", "r")
        with pytest.raises(InvalidInput, match="dataset"):
            svc.resume("r1")

    def test_resume_after_lock(self, svc):
        svc.start("r1", "m", "r")
        svc.lock_dataset("r1", DATASET_A)
        run = svc.resume("r1")
        assert run.phase == TrainingPhase.TRAINING

    def test_resume_idempotent_in_training(self, svc):
        svc.start("r1", "m", "r")
        svc.lock_dataset("r1", DATASET_A)
        svc.resume("r1")
        run = svc.resume("r1")
        assert run.phase == TrainingPhase.TRAINING

    def test_complete_training_creates_model_identity(self, svc):
        svc.start("r1", "qwen3:8b", "abc123")
        svc.lock_dataset("r1", DATASET_A)
        svc.resume("r1")
        run = svc.complete_training("r1", "adapter_hash_1")
        assert run.phase == TrainingPhase.EVALUATING
        assert run.model_identity is not None
        assert run.model_identity.adapter_hash == "adapter_hash_1"
        assert run.model_identity.dataset_digest == DATASET_A.digest

    def test_model_identity_immutable(self, svc):
        svc.start("r1", "qwen3:8b", "abc123")
        svc.lock_dataset("r1", DATASET_A)
        svc.resume("r1")
        run = svc.complete_training("r1", "adapter_hash_1")
        mi = run.model_identity
        with pytest.raises(AttributeError):
            mi.adapter_hash = "tampered"

    def test_evaluation_to_candidate(self, svc, store):
        run = _full_lifecycle(svc, store)
        assert run.phase == TrainingPhase.CANDIDATE
        assert run.evaluation_score == 0.85
        assert run.can_deploy is True

    def test_candidate_cannot_deploy_without_score(self, svc, store):
        svc.start("r1", "m", "r")
        svc.lock_dataset("r1", DATASET_A)
        svc.resume("r1")
        svc.complete_training("r1", "h")
        run = store.get_run("r1")
        assert run.can_deploy is False

    def test_not_found(self, svc):
        with pytest.raises(InvalidInput, match="not found"):
            svc.resume("nonexistent")


class TestDeploymentService:
    def test_deploy_candidate(self):
        ts = InMemoryTrainingStore()
        ds = InMemoryDeploymentStore()
        training = TrainingService(ts)
        deploy_svc = DeploymentService(ts, ds)

        _full_lifecycle(training, ts, "r1")
        deployment = deploy_svc.deploy("r1")

        assert isinstance(deployment, Deployment)
        assert deployment.run_id == "r1"
        assert deployment.policy_tier == "code"
        run = ts.get_run("r1")
        assert run.phase == TrainingPhase.DEPLOYED

    def test_deploy_non_candidate_forbidden(self):
        ts = InMemoryTrainingStore()
        ds = InMemoryDeploymentStore()
        training = TrainingService(ts)
        deploy_svc = DeploymentService(ts, ds)

        training.start("r1", "m", "r")
        with pytest.raises(Forbidden, match="cannot be deployed"):
            deploy_svc.deploy("r1")

    def test_deploy_records_previous_model(self):
        ts = InMemoryTrainingStore()
        ds = InMemoryDeploymentStore()
        training = TrainingService(ts)
        deploy_svc = DeploymentService(ts, ds)

        _full_lifecycle(training, ts, "r1")
        deploy_svc.deploy("r1")

        _full_lifecycle(training, ts, "r2")
        d2 = deploy_svc.deploy("r2")
        assert d2.previous_model == "adapter_hash_1"

    def test_rollback(self):
        ts = InMemoryTrainingStore()
        ds = InMemoryDeploymentStore()
        training = TrainingService(ts)
        deploy_svc = DeploymentService(ts, ds)

        _full_lifecycle(training, ts, "r1")
        deploy_svc.deploy("r1")
        _full_lifecycle(training, ts, "r2")
        deploy_svc.deploy("r2")

        prev = deploy_svc.rollback("r2")
        assert prev is not None
        assert prev.run_id == "r1"

    def test_deploy_not_found(self):
        ts = InMemoryTrainingStore()
        ds = InMemoryDeploymentStore()
        deploy_svc = DeploymentService(ts, ds)
        with pytest.raises(InvalidInput, match="not found"):
            deploy_svc.deploy("nonexistent")
