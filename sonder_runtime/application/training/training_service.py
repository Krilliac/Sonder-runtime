"""Training service (SPEC-5 §20).

Owns the training lifecycle. Key invariants:
- No autonomous training start (attended only).
- Resume uses same dataset digest.
- Candidate cannot activate itself.
- Immutable model identity after creation.
- trust_remote_code=False enforced.
- DeploymentService is sole runtime-policy mutator.
"""
from __future__ import annotations

from typing import Protocol

from ...domain.training.models import (
    DatasetIdentity,
    Deployment,
    ModelIdentity,
    TrainingPhase,
    TrainingRun,
)
from ...domain.common.errors import Forbidden, InvalidInput


class TrainingStore(Protocol):
    def create_run(
        self, run_id: str, base_model: str, base_revision: str,
    ) -> TrainingRun: ...
    def get_run(self, run_id: str) -> TrainingRun | None: ...
    def save_run(self, run: TrainingRun) -> None: ...


class DeploymentStore(Protocol):
    def record_deployment(self, deployment: Deployment) -> None: ...
    def get_active(self) -> Deployment | None: ...
    def rollback(self, run_id: str) -> Deployment | None: ...


class TrainingService:
    def __init__(self, store: TrainingStore):
        self._store = store

    def start(
        self,
        run_id: str,
        base_model: str,
        base_revision: str,
        *,
        attended: bool = True,
    ) -> TrainingRun:
        if not attended:
            raise Forbidden("autonomous training start is not allowed")

        run = self._store.create_run(run_id, base_model, base_revision)
        assert not run.trust_remote_code
        self._store.save_run(run)
        return run

    def lock_dataset(
        self, run_id: str, dataset: DatasetIdentity,
    ) -> TrainingRun:
        run = self._require(run_id)
        if run.phase != TrainingPhase.PLANNED:
            raise InvalidInput("can only lock dataset in PLANNED phase")
        run.dataset = dataset
        run.phase = TrainingPhase.DATASET_LOCKED
        self._store.save_run(run)
        return run

    def resume(self, run_id: str) -> TrainingRun:
        run = self._require(run_id)
        if run.dataset is None:
            raise InvalidInput("cannot resume without locked dataset")
        if run.phase not in (TrainingPhase.DATASET_LOCKED, TrainingPhase.TRAINING):
            raise InvalidInput("cannot resume from phase %s" % run.phase.value)
        run.phase = TrainingPhase.TRAINING
        self._store.save_run(run)
        return run

    def complete_training(
        self, run_id: str, adapter_hash: str,
    ) -> TrainingRun:
        run = self._require(run_id)
        if run.phase != TrainingPhase.TRAINING:
            raise InvalidInput("not in training phase")
        if run.dataset is None:
            raise InvalidInput("no dataset locked")
        run.model_identity = ModelIdentity(
            run_id=run_id,
            base_model=run.base_model,
            base_revision=run.base_revision,
            dataset_digest=run.dataset.digest,
            adapter_hash=adapter_hash,
        )
        run.phase = TrainingPhase.EVALUATING
        self._store.save_run(run)
        return run

    def record_evaluation(
        self, run_id: str, score: float,
    ) -> TrainingRun:
        run = self._require(run_id)
        if run.phase != TrainingPhase.EVALUATING:
            raise InvalidInput("not in evaluating phase")
        run.evaluation_score = score
        run.phase = TrainingPhase.CANDIDATE
        self._store.save_run(run)
        return run

    def _require(self, run_id: str) -> TrainingRun:
        run = self._store.get_run(run_id)
        if run is None:
            raise InvalidInput(f"training run {run_id!r} not found")
        return run


class DeploymentService:
    """Sole mutator of runtime policy for model deployment."""

    def __init__(
        self,
        training_store: TrainingStore,
        deployment_store: DeploymentStore,
    ):
        self._training = training_store
        self._deployment = deployment_store

    def deploy(self, run_id: str, tier: str = "code") -> Deployment:
        run = self._training.get_run(run_id)
        if run is None:
            raise InvalidInput(f"training run {run_id!r} not found")
        if not run.can_deploy:
            raise Forbidden("training run cannot be deployed")
        if run.model_identity is None:
            raise InvalidInput("no model identity")

        active = self._deployment.get_active()
        deployment = Deployment(
            run_id=run_id,
            model_identity=run.model_identity,
            policy_tier=tier,
            previous_model=active.model_identity.adapter_hash if active else "",
        )
        self._deployment.record_deployment(deployment)
        run.phase = TrainingPhase.DEPLOYED
        self._training.save_run(run)
        return deployment

    def rollback(self, run_id: str) -> Deployment | None:
        return self._deployment.rollback(run_id)
