"""Training domain models (SPEC-5 §20).

Immutable training lifecycle: attended start → dataset snapshot →
training → evaluation → deployment candidate. No autonomous start.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TrainingPhase(Enum):
    PLANNED = "planned"
    DATASET_LOCKED = "dataset_locked"
    TRAINING = "training"
    EVALUATING = "evaluating"
    CANDIDATE = "candidate"
    DEPLOYED = "deployed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


TERMINAL_PHASES = frozenset({
    TrainingPhase.DEPLOYED,
    TrainingPhase.FAILED,
    TrainingPhase.ROLLED_BACK,
})


@dataclass(frozen=True)
class DatasetIdentity:
    digest: str
    row_count: int
    created_at: str = ""


@dataclass(frozen=True)
class ModelIdentity:
    run_id: str
    base_model: str
    base_revision: str
    dataset_digest: str
    adapter_hash: str = ""
    created_at: str = ""


@dataclass
class TrainingRun:
    id: str
    phase: TrainingPhase
    base_model: str
    base_revision: str
    dataset: DatasetIdentity | None = None
    model_identity: ModelIdentity | None = None
    attended: bool = True
    trust_remote_code: bool = False
    checkpoints: list[str] = field(default_factory=list)
    evaluation_score: float | None = None

    @property
    def is_terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES

    @property
    def can_deploy(self) -> bool:
        return (
            self.phase == TrainingPhase.CANDIDATE
            and self.model_identity is not None
            and self.evaluation_score is not None
        )


@dataclass(frozen=True)
class Deployment:
    run_id: str
    model_identity: ModelIdentity
    policy_tier: str
    previous_model: str = ""
    created_at: str = ""
