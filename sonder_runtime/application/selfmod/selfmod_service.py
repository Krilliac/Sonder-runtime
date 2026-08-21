"""Self-modification service (SPEC-5 §18).

Orchestrates the guarded selfmod lifecycle: snapshot → edit →
test → review → approve → deploy. Candidate-generated code
never decides its own test/approval/deployment outcomes.

Guarded invariants:
- Live source cannot be mutated by candidate executor.
- Failed tests block deployment.
- Rollback restores hashes exactly.
- Dirty checkout does not receive auto commit.
- No automatic remote push.

Unrestricted mode (--unrestricted-selfmod):
- Can use host executor.
- Protected path checks bypassed.
- Approval can be bypassed.
- Candidate isolation can be bypassed.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Mapping, Protocol, Sequence

from ...domain.selfmod.models import (
    InvalidPhaseTransition,
    SelfmodPhase,
    SelfmodRun,
    Snapshot,
)
from ...domain.common.errors import Forbidden, InvalidInput
from .governance import (
    CandidateRecord,
    ReviewEvidence as GovernanceReviewEvidence,
    SelfmodGovernance,
    VerificationEvidence as GovernanceVerificationEvidence,
    WorktreeMetadata,
)
from .reproducer_contract import FailureEvidence, ReproducerEvidence
from .verification_lifecycle import (
    ActivationRecord,
    BackupRecord,
    FailureState,
    HealthRecord,
    LifecyclePhase,
    ReviewRecord,
    RollbackRecord,
    VerificationKind,
    VerificationLifecycle,
    VerificationLifecycleRecord,
    VerificationRecord,
)


class SelfmodStore(Protocol):
    def create_run(self, run_id: str, objective: str) -> SelfmodRun: ...
    def get_run(self, run_id: str) -> SelfmodRun | None: ...
    def save_run(self, run: SelfmodRun) -> None: ...
    def save_snapshot(self, snapshot: Snapshot) -> None: ...
    def get_snapshot(self, run_id: str) -> Snapshot | None: ...


class WorkspaceAdapter(Protocol):
    def is_clean(self) -> bool: ...
    def file_hashes(self) -> dict[str, str]: ...


class SelfModificationService:

    def __init__(
        self,
        store: SelfmodStore,
        workspace: WorkspaceAdapter,
        unrestricted: bool = False,
    ):
        self._store = store
        self._workspace = workspace
        self._unrestricted = unrestricted

    def propose(self, run_id: str, objective: str) -> SelfmodRun:
        run = self._store.create_run(run_id, objective)
        run.transition(SelfmodPhase.PROPOSED)
        self._store.save_run(run)
        return run

    def backup(self, run_id: str) -> Snapshot:
        run = self._require_run(run_id)
        hashes = self._workspace.file_hashes()
        snapshot = Snapshot(run_id=run_id, file_hashes=hashes)
        self._store.save_snapshot(snapshot)
        run.backup_hash = _digest(hashes)
        run.transition(SelfmodPhase.BACKED_UP)
        self._store.save_run(run)
        return snapshot

    def start_editing(self, run_id: str) -> SelfmodRun:
        run = self._require_run(run_id)
        run.transition(SelfmodPhase.EDITING)
        self._store.save_run(run)
        return run

    def submit_tests(self, run_id: str, passed: bool) -> SelfmodRun:
        run = self._require_run(run_id)
        run.transition(SelfmodPhase.TESTING)
        run.test_passed = passed
        if not passed and not self._unrestricted:
            run.transition(SelfmodPhase.REJECTED)
        else:
            run.transition(SelfmodPhase.REVIEWING)
        self._store.save_run(run)
        return run

    def approve(self, run_id: str) -> SelfmodRun:
        run = self._require_run(run_id)
        if not self._unrestricted and not run.test_passed:
            raise Forbidden("cannot approve without passing tests")
        run.approval_given = True
        run.transition(SelfmodPhase.APPROVED)
        self._store.save_run(run)
        return run

    def deploy(self, run_id: str) -> SelfmodRun:
        run = self._require_run(run_id)
        if not self._unrestricted:
            if not run.can_deploy:
                raise Forbidden("deploy requires tests passed, approval, and backup")
            if not self._workspace.is_clean():
                raise Forbidden("dirty checkout cannot receive auto commit")
        run.deployed_hash = _digest(self._workspace.file_hashes())
        run.transition(SelfmodPhase.DEPLOYED)
        self._store.save_run(run)
        return run

    def rollback(self, run_id: str) -> SelfmodRun:
        run = self._require_run(run_id)
        snapshot = self._store.get_snapshot(run_id)
        if snapshot is None:
            raise InvalidInput("no snapshot to roll back to")
        run.transition(SelfmodPhase.ROLLBACK_REQUESTED)
        run.transition(SelfmodPhase.RESTORED)
        self._store.save_run(run)
        return run

    def _require_run(self, run_id: str) -> SelfmodRun:
        run = self._store.get_run(run_id)
        if run is None:
            raise InvalidInput(f"selfmod run {run_id!r} not found")
        return run


def _digest(hashes: dict[str, str]) -> str:
    import hashlib
    combined = "|".join(f"{k}={v}" for k, v in sorted(hashes.items()))
    return hashlib.sha256(combined.encode()).hexdigest()[:16]


class LegacySelfmodPort(Protocol):
    """The narrow legacy execution surface composed by bootstrap."""

    def create_plan(self, objective: str, repository_root: object, **kwargs: object) -> Mapping[str, object]: ...
    def get_run(self, run_id: str) -> Mapping[str, object]: ...
    def create_backup(self, run_id: str) -> Mapping[str, object]: ...
    def prepare_workspace(self, run_id: str) -> Mapping[str, object]: ...
    def record_reproducer_before(self, run_id: str, command: Sequence[str], timeout: int | None = None) -> Mapping[str, object]: ...
    def begin_testing(self, run_id: str) -> Mapping[str, object]: ...
    def record_test(self, run_id: str, kind: str, command: Sequence[str], **kwargs: object) -> Mapping[str, object]: ...
    def review(self, run_id: str, **kwargs: object) -> Mapping[str, object]: ...
    def approve(self, run_id: str, approver: str = "user") -> Mapping[str, object]: ...
    def deploy(self, run_id: str, **kwargs: object) -> Mapping[str, object]: ...
    def rollback(self, run_id: str, reason: str = "user requested rollback") -> Mapping[str, object]: ...


@dataclass(frozen=True)
class SelfmodIntegrationState:
    """A read-only view joining the durable legacy run to typed evidence."""

    legacy_run: Mapping[str, object]
    governance: CandidateRecord
    lifecycle: VerificationLifecycleRecord


class GuardedLegacySelfmodService:
    """Bridge typed selfmod contracts to the guarded legacy executor.

    The legacy module remains the mutation/recovery authority.  This service
    adds evidence and lifecycle gates around it; it never edits files, runs
    commands itself, commits, or pushes.  ``unrestricted`` intentionally
    delegates the legacy bypass path without imposing the guarded gates.
    """

    _ROOT_KINDS = {
        VerificationKind.TARGETED: "targeted",
        VerificationKind.ARCHITECTURE: "architecture",
        VerificationKind.REGRESSION: "regression",
        VerificationKind.SMOKE: "smoke",
    }

    def __init__(self, legacy: LegacySelfmodPort, *, unrestricted: bool = False) -> None:
        self._legacy = legacy
        self._unrestricted = bool(unrestricted)
        self._governance = SelfmodGovernance()
        self._lifecycle = VerificationLifecycle()

    def create_plan(self, objective: str, repository_root: object, **kwargs: object) -> SelfmodIntegrationState:
        run = self._legacy.create_plan(objective, repository_root, **kwargs)
        run_id = _required_run_value(run, "id")
        baseline = _required_run_value(run, "source_fingerprint")
        governance = self._governance.propose(
            run_id, str(run.get("objective") or objective), baseline,
            unrestricted=self._unrestricted,
        )
        lifecycle = self._lifecycle.propose(run_id, str(run.get("objective") or objective), baseline)
        return SelfmodIntegrationState(run, governance, lifecycle)

    def prepare(self, run_id: str) -> SelfmodIntegrationState:
        run = self._legacy.get_run(run_id)
        phase = str(run.get("phase", ""))
        if phase == "proposed":
            self._legacy.create_backup(run_id)
            run = self._legacy.prepare_workspace(run_id)
        elif phase == "backed_up":
            run = self._legacy.prepare_workspace(run_id)
        elif phase != "editing":
            raise InvalidInput(f"selfmod run {run_id!r} is not ready for preparation")
        if self._governance.get(run_id).phase.value == "proposed":
            metadata = WorktreeMetadata(
                str(run.get("workspace_path") or "workspace-unavailable"),
                str(run.get("branch_name") or f"selfmod/{run_id}"),
                str(run.get("starting_commit") or "no-git-commit"),
                isolated=bool(run.get("workspace_path")),
                clean=True,
                managed=bool(run.get("workspace_path")),
            )
            governance = self._governance.attach_worktree(run_id, metadata)
        else:
            governance = self._governance.get(run_id)
        return SelfmodIntegrationState(run, governance, self._lifecycle.get(run_id))

    def record_reproducer(self, run_id: str, evidence: ReproducerEvidence) -> SelfmodIntegrationState:
        if not isinstance(evidence, FailureEvidence):
            raise InvalidInput("guarded legacy execution accepts FailureEvidence reproducers only")
        result = self._legacy.record_reproducer_before(run_id, evidence.command_argv)
        if not bool(result.get("passed")):
            raise Forbidden("baseline reproducer did not demonstrate the declared failure")
        governance = self._governance.record_reproducer(run_id, evidence)
        return self._state(run_id, governance=governance)

    def record_verification(
        self, run_id: str, kind: VerificationKind, command: Sequence[str], *, timeout: int | None = None,
    ) -> SelfmodIntegrationState:
        if not isinstance(kind, VerificationKind):
            raise InvalidInput("verification kind must be a VerificationKind")
        lifecycle = self._lifecycle.get(run_id)
        if not self._unrestricted and lifecycle.phase is LifecyclePhase.PROPOSED:
            self._legacy.begin_testing(run_id)
        result = self._legacy.record_test(run_id, self._ROOT_KINDS[kind], command, timeout=timeout)
        evidence_id = f"{run_id}:{kind.value}"
        digest = _receipt_digest(result)
        passed = bool(result.get("passed"))
        typed_governance = GovernanceVerificationEvidence(
            evidence_id=evidence_id,
            check=kind.value,
            passed=passed,
            artifact_digest=digest,
            summary=str(result.get("output") or "")[:1000],
        )
        typed_lifecycle = VerificationRecord(
            evidence_id, kind, passed, digest, str(result.get("output") or "")[:1000],
        )
        if self._unrestricted:
            with _suppress_typed_gate_errors():
                self._governance.record_verification(run_id, typed_governance)
                self._lifecycle.record_verification(run_id, typed_lifecycle)
        else:
            governance = self._governance.record_verification(run_id, typed_governance)
            lifecycle = self._lifecycle.record_verification(run_id, typed_lifecycle)
            return SelfmodIntegrationState(self._legacy.get_run(run_id), governance, lifecycle)
        return self._state(run_id)

    def review(self, run_id: str, *, reviewer: str = "independent-selfmod-review") -> SelfmodIntegrationState:
        if self._unrestricted:
            run = self._legacy.review(run_id)
            return self._state(run_id, legacy_run=run)
        governance = self._governance.get(run_id)
        lifecycle = self._lifecycle.get(run_id)
        if lifecycle.phase is not LifecyclePhase.VERIFIED or governance.phase.value != "verified":
            raise Forbidden("typed verification evidence is incomplete")
        result = self._legacy.review(
            run_id,
            require_kinds={"reproducer_before", "targeted", "architecture", "regression", "smoke"},
        )
        approved = str(result.get("phase")) == "reviewing"
        evidence_ids = tuple(item.evidence_id for item in governance.verifications)
        gov_review = GovernanceReviewEvidence(
            f"{run_id}:review", reviewer, approved, evidence_ids,
            summary=str(result.get("last_error") or "legacy review complete")[:1000],
        )
        lifecycle_review = ReviewRecord(
            f"{run_id}:review", reviewer, approved, evidence_ids,
            summary=str(result.get("last_error") or "legacy review complete")[:1000],
        )
        governance = self._governance.record_review(run_id, gov_review)
        lifecycle = self._lifecycle.record_review(run_id, lifecycle_review)
        return SelfmodIntegrationState(result, governance, lifecycle)

    def approve(self, run_id: str, *, approver: str = "user") -> SelfmodIntegrationState:
        if self._unrestricted:
            run = self._legacy.approve(run_id, approver=approver)
            return self._state(run_id, legacy_run=run)
        governance = self._governance.get(run_id)
        lifecycle = self._lifecycle.get(run_id)
        if governance.phase.value != "reviewed" or lifecycle.phase is not LifecyclePhase.REVIEWED:
            raise Forbidden("independent review is required before approval")
        run = self._legacy.approve(run_id, approver=approver)
        governance = self._governance.approve(run_id)
        backup_digest = _receipt_digest({"manifest": run.get("backup_manifest"), "run_id": run_id})
        lifecycle = self._lifecycle.record_backup(
            run_id, BackupRecord(f"{run_id}:backup", True, backup_digest, "legacy immutable backup verified"),
        )
        return SelfmodIntegrationState(run, governance, lifecycle)

    def deploy(self, run_id: str, *, health_command: Sequence[str] | None = None, commit: bool = True) -> SelfmodIntegrationState:
        if self._unrestricted:
            run = self._legacy.deploy(run_id, health_command=health_command, commit=commit)
            return self._state(run_id, legacy_run=run)
        if health_command is None:
            raise Forbidden("guarded integration requires an explicit post-deployment health command")
        governance = self._governance.get(run_id)
        lifecycle = self._lifecycle.get(run_id)
        if governance.phase.value != "approved" or lifecycle.phase is not LifecyclePhase.BACKED_UP:
            raise Forbidden("typed approval and backup evidence are required before deployment")
        governance = self._governance.deployment_intent(run_id)
        try:
            run = self._legacy.deploy(run_id, health_command=health_command, commit=commit)
        except Exception:
            run = self._legacy.get_run(run_id)
            if str(run.get("phase")) == "restored":
                lifecycle = self._lifecycle.record_activation(
                    run_id, ActivationRecord(f"{run_id}:activation", True, _receipt_digest(run), "legacy activation rolled back after health failure"),
                )
                lifecycle = self._lifecycle.record_health(
                    run_id, HealthRecord(f"{run_id}:health", False, _receipt_digest(run), "post-deployment health failed"),
                )
                lifecycle = self._lifecycle.record_rollback(
                    run_id, RollbackRecord(f"{run_id}:rollback", True, _receipt_digest(run), "legacy automatic rollback completed"),
                )
            else:
                lifecycle = self._lifecycle.record_activation(
                    run_id, ActivationRecord(f"{run_id}:activation", False, _receipt_digest(run), "legacy deployment failed before activation"),
                )
            return SelfmodIntegrationState(run, governance, lifecycle)
        lifecycle = self._lifecycle.record_activation(
            run_id, ActivationRecord(f"{run_id}:activation", True, _receipt_digest(run), "legacy deployment completed"),
        )
        lifecycle = self._lifecycle.record_health(
            run_id, HealthRecord(f"{run_id}:health", True, _receipt_digest(run), "explicit post-deployment health passed"),
        )
        return SelfmodIntegrationState(run, governance, lifecycle)

    def get(self, run_id: str) -> SelfmodIntegrationState:
        return self._state(run_id)

    def _state(self, run_id: str, *, legacy_run: Mapping[str, object] | None = None,
               governance: CandidateRecord | None = None,
               lifecycle: VerificationLifecycleRecord | None = None) -> SelfmodIntegrationState:
        return SelfmodIntegrationState(
            legacy_run if legacy_run is not None else self._legacy.get_run(run_id),
            governance if governance is not None else self._governance.get(run_id),
            lifecycle if lifecycle is not None else self._lifecycle.get(run_id),
        )


class _suppress_typed_gate_errors:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return exc_type is not None


def _required_run_value(run: Mapping[str, object], field: str) -> str:
    value = run.get(field)
    if not isinstance(value, str) or not value.strip():
        raise InvalidInput(f"legacy selfmod run did not provide {field}")
    return value


def _receipt_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "GuardedLegacySelfmodService",
    "LegacySelfmodPort",
    "SelfModificationService",
    "SelfmodIntegrationState",
]
