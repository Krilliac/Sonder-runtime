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
from pathlib import Path
from typing import Mapping, Protocol, Sequence
from collections.abc import Callable

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
from ..execution.effect_journal import EffectJournalError, EffectState
from ..execution.worker_bindings import AuthenticatedWorkerBinding, journaled_effect


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

    def list_runs(self, limit: int = 20) -> Sequence[Mapping[str, object]]: ...
    def create_backup(self, run_id: str) -> Mapping[str, object]: ...
    def prepare_workspace(self, run_id: str) -> Mapping[str, object]: ...
    def record_reproducer_before(self, run_id: str, command: Sequence[str], timeout: int | None = None) -> Mapping[str, object]: ...
    def begin_testing(self, run_id: str) -> Mapping[str, object]: ...
    def record_test(self, run_id: str, kind: str, command: Sequence[str], **kwargs: object) -> Mapping[str, object]: ...
    def review(self, run_id: str, **kwargs: object) -> Mapping[str, object]: ...
    def approve(self, run_id: str, approver: str = "user") -> Mapping[str, object]: ...
    def deploy(self, run_id: str, **kwargs: object) -> Mapping[str, object]: ...
    def rollback(self, run_id: str, reason: str = "user requested rollback") -> Mapping[str, object]: ...


def _result_phase(result: Mapping[str, object]) -> str:
    if not isinstance(result, Mapping):
        # A malformed legacy receipt is ambiguous; raising here makes the
        # journal record the effect as uncertain instead of settled.
        raise TypeError("legacy selfmod stage returned a non-mapping receipt")
    return str(result.get("phase", ""))


def _result_passed(result: Mapping[str, object]) -> bool:
    if not isinstance(result, Mapping):
        raise TypeError("legacy selfmod stage returned a non-mapping receipt")
    return result.get("passed") is True


@dataclass(frozen=True, slots=True)
class _StageEffect:
    """Journal identity and success predicate for one legacy mutating stage.

    ``operation`` prefixes the journal operation id and ``receipt`` names the
    stage in its receipt/idempotency key.  A ``repeatable`` stage is one the
    legacy port legitimately allows to run again for the same run; each call
    gets a per-attempt identity derived from the durable journal.
    """

    operation: str
    receipt: str
    repeatable: bool
    success: Callable[[Mapping[str, object]], bool]


# Predicates mirror the legacy run-dict contract in ``selfmod.py``: each stage
# reports the phase it moved the run into, and command stages report
# ``passed``.  Deploy and rollback keep their original identity and semantics.
_STAGE_EFFECTS: Mapping[str, _StageEffect] = {
    "create_backup": _StageEffect(
        "selfmod-backup", "backup", False,
        lambda result: _result_phase(result) == "backed_up",
    ),
    "prepare_workspace": _StageEffect(
        "selfmod-prepare-workspace", "prepare-workspace", False,
        lambda result: _result_phase(result) == "editing",
    ),
    # Allowed repeatedly in the editing/testing phases.
    "record_reproducer_before": _StageEffect(
        "selfmod-reproducer-before", "reproducer-before", True, _result_passed,
    ),
    # Re-entered from ``interrupted`` after an interruption.
    "begin_testing": _StageEffect(
        "selfmod-begin-testing", "begin-testing", True,
        lambda result: _result_phase(result) == "testing",
    ),
    # One call per verification kind, and retried checks.
    "record_test": _StageEffect(
        "selfmod-record-test", "record-test", True, _result_passed,
    ),
    # A review can auto-approve an eligible run in the same legacy call.
    "review": _StageEffect(
        "selfmod-review", "review", False,
        lambda result: _result_phase(result) in {"reviewing", "approved"},
    ),
    "approve": _StageEffect(
        "selfmod-approve", "approve", False,
        lambda result: _result_phase(result) == "approved",
    ),
    "deploy": _StageEffect(
        "selfmod-deploy", "deploy", False,
        lambda result: str(result.get("phase", "")) == "deployed",
    ),
    "rollback": _StageEffect("selfmod-rollback", "rollback", False, lambda _result: True),
}
# Bound on journaled attempts of one repeatable stage per run.  Every retry
# requires all earlier attempts settled, so this also bounds the keyed journal
# lookups used to derive the next attempt.
MAX_SELFMOD_STAGE_ATTEMPTS = 256


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

    def __init__(
        self,
        legacy: LegacySelfmodPort,
        *,
        unrestricted: bool = False,
        effect_binding_factory: Callable[[str], AuthenticatedWorkerBinding] | None = None,
    ) -> None:
        self._legacy = legacy
        self._unrestricted = bool(unrestricted)
        if effect_binding_factory is not None and not callable(effect_binding_factory):
            raise TypeError("effect_binding_factory must be callable")
        self._effect_binding_factory = effect_binding_factory
        self._governance = SelfmodGovernance()
        self._lifecycle = VerificationLifecycle()
        self._repository_roots: dict[str, str] = {}

    def create_plan(self, objective: str, repository_root: object, **kwargs: object) -> SelfmodIntegrationState:
        run = self._legacy.create_plan(objective, repository_root, **kwargs)
        run_id = _required_run_value(run, "id")
        self._repository_roots[run_id] = str(repository_root)
        baseline = _required_run_value(run, "source_fingerprint")
        governance = self._governance.propose(
            run_id, str(run.get("objective") or objective), baseline,
            unrestricted=self._unrestricted,
        )
        lifecycle = self._lifecycle.propose(run_id, str(run.get("objective") or objective), baseline)
        return SelfmodIntegrationState(run, governance, lifecycle)

    def list_runs(self, *, limit: int = 64) -> tuple[Mapping[str, object], ...]:
        """Return bounded legacy run summaries for operator projections."""
        if type(limit) is not int or not 1 <= limit <= 256:
            raise InvalidInput("selfmod run limit must be between 1 and 256")
        rows = self._legacy.list_runs(limit=limit)
        if not isinstance(rows, (list, tuple)):
            raise InvalidInput("selfmod list_runs returned an invalid result")
        return tuple(row for row in rows[:limit] if isinstance(row, Mapping))

    def prepare(self, run_id: str) -> SelfmodIntegrationState:
        run = self._legacy.get_run(run_id)
        phase = str(run.get("phase", ""))
        if phase == "proposed":
            self._mutating_call(
                run_id, "create_backup", {}, lambda: self._legacy.create_backup(run_id),
            )
            run = self._mutating_call(
                run_id, "prepare_workspace", {},
                lambda: self._legacy.prepare_workspace(run_id),
            )
        elif phase == "backed_up":
            run = self._mutating_call(
                run_id, "prepare_workspace", {},
                lambda: self._legacy.prepare_workspace(run_id),
            )
        elif phase != "editing":
            raise InvalidInput(f"selfmod run {run_id!r} is not ready for preparation")
        if self._governance.get(run_id).phase.value == "proposed":
            repository_root = str(run.get("repository_root") or self._repository_roots.get(run_id) or "")
            if not repository_root.strip():
                raise InvalidInput("selfmod run did not provide a repository root for isolation")
            workspace_path = str(run.get("workspace_path") or "")
            isolated = bool(workspace_path) and _is_distinct_path(workspace_path, repository_root)
            metadata = WorktreeMetadata(
                workspace_path or "workspace-unavailable",
                str(run.get("branch_name") or f"selfmod/{run_id}"),
                str(run.get("starting_commit") or "no-git-commit"),
                isolated=isolated,
                clean=bool(run.get("workspace_clean", True)),
                managed=isolated,
            )
            governance = self._governance.attach_worktree(run_id, metadata)
        else:
            governance = self._governance.get(run_id)
        return SelfmodIntegrationState(run, governance, self._lifecycle.get(run_id))

    def record_reproducer(self, run_id: str, evidence: ReproducerEvidence) -> SelfmodIntegrationState:
        if not isinstance(evidence, FailureEvidence):
            raise InvalidInput("guarded legacy execution accepts FailureEvidence reproducers only")
        command = tuple(evidence.command_argv)
        result = self._mutating_call(
            run_id, "record_reproducer_before", {"command": list(command)},
            lambda: self._legacy.record_reproducer_before(run_id, command),
        )
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
            self._mutating_call(
                run_id, "begin_testing", {}, lambda: self._legacy.begin_testing(run_id),
            )
        root_kind = self._ROOT_KINDS[kind]
        result = self._mutating_call(
            run_id, "record_test",
            {"kind": root_kind, "command": list(command), "timeout": timeout},
            lambda: self._legacy.record_test(run_id, root_kind, command, timeout=timeout),
        )
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
            run = self._mutating_call(
                run_id, "review", {"require_kinds": None},
                lambda: self._legacy.review(run_id),
            )
            governance = self._governance.get(run_id)
            if not governance.verifications:
                governance = self._governance.mark_unrestricted_bypass(run_id, "verification")
            if not governance.reproducer_evidence:
                governance = self._governance.mark_unrestricted_bypass(run_id, "reproducer")
            governance = self._governance.mark_unrestricted_bypass(run_id, "review")
            return self._state(run_id, legacy_run=run)
        governance = self._governance.get(run_id)
        lifecycle = self._lifecycle.get(run_id)
        if lifecycle.phase is not LifecyclePhase.VERIFIED or governance.phase.value != "verified":
            raise Forbidden("typed verification evidence is incomplete")
        require_kinds = {"reproducer_before", "targeted", "architecture", "regression", "smoke"}
        result = self._mutating_call(
            run_id, "review", {"require_kinds": sorted(require_kinds)},
            lambda: self._legacy.review(run_id, require_kinds=require_kinds),
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
            run = self._mutating_call(
                run_id, "approve", {"approver": approver},
                lambda: self._legacy.approve(run_id, approver=approver),
            )
            return self._state(run_id, legacy_run=run)
        governance = self._governance.get(run_id)
        lifecycle = self._lifecycle.get(run_id)
        if governance.phase.value != "reviewed" or lifecycle.phase is not LifecyclePhase.REVIEWED:
            raise Forbidden("independent review is required before approval")
        run = self._mutating_call(
            run_id, "approve", {"approver": approver},
            lambda: self._legacy.approve(run_id, approver=approver),
        )
        governance = self._governance.approve(run_id)
        backup_digest = _receipt_digest({"manifest": run.get("backup_manifest"), "run_id": run_id})
        lifecycle = self._lifecycle.record_backup(
            run_id, BackupRecord(f"{run_id}:backup", True, backup_digest, "legacy hash-verified backup"),
        )
        return SelfmodIntegrationState(run, governance, lifecycle)

    def deploy(
        self,
        run_id: str,
        *,
        health_command: Sequence[str] | None = None,
        commit: bool = True,
        automatic_push: bool = False,
        remote_push: bool = False,
    ) -> SelfmodIntegrationState:
        if automatic_push or remote_push:
            raise Forbidden("automatic remote push is forbidden; deployment is local only")
        if self._unrestricted:
            run = self._mutating_call(
                run_id, "deploy", {"health_command": health_command, "commit": commit},
                lambda: self._legacy.deploy(run_id, health_command=health_command, commit=commit),
            )
            return self._state(run_id, legacy_run=run)
        if health_command is None:
            raise Forbidden("guarded integration requires an explicit post-deployment health command")
        governance = self._governance.get(run_id)
        lifecycle = self._lifecycle.get(run_id)
        if governance.phase.value != "approved" or lifecycle.phase is not LifecyclePhase.BACKED_UP:
            raise Forbidden("typed approval and backup evidence are required before deployment")
        governance = self._governance.deployment_intent(run_id)
        try:
            run = self._mutating_call(
                run_id, "deploy", {"health_command": health_command, "commit": commit},
                lambda: self._legacy.deploy(run_id, health_command=health_command, commit=commit),
            )
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
        if str(run.get("phase")) != "deployed":
            # A legacy adapter that returns a non-deployed receipt without
            # raising has not established activation.  Do not mint health or
            # completion evidence from that ambiguous result.  The adapter is
            # still asked to restore because it may have changed bytes before
            # producing its malformed receipt.
            try:
                rollback_run = self._mutating_call(
                    run_id, "rollback", {"reason": "ambiguous deployment receipt"},
                    lambda: self._legacy.rollback(
                        run_id, reason="ambiguous deployment receipt; fail-closed rollback"
                    ),
                )
            except Exception as exc:
                raise Forbidden("ambiguous deployment receipt and rollback failed") from exc
            run = rollback_run
            lifecycle = self._lifecycle.record_activation(
                run_id, ActivationRecord(f"{run_id}:activation", False, _receipt_digest(run), "legacy deployment returned no deployed phase"),
            )
            raise Forbidden("deployment did not return a deployed phase")
        lifecycle = self._lifecycle.record_activation(
            run_id, ActivationRecord(f"{run_id}:activation", True, _receipt_digest(run), "legacy deployment completed"),
        )
        lifecycle = self._lifecycle.record_health(
            run_id, HealthRecord(f"{run_id}:health", True, _receipt_digest(run), "explicit post-deployment health passed"),
        )
        return SelfmodIntegrationState(run, governance, lifecycle)

    def _mutating_call(
        self,
        run_id: str,
        stage: str,
        request: object,
        invoke: Callable[[], Mapping[str, object]],
    ) -> Mapping[str, object]:
        """Run one legacy mutating stage under the worker effect journal.

        Without a composed binding factory the legacy call runs directly, as
        before.  Otherwise the stage's own success predicate classifies the
        receipt: a receipt that misses it is a settled ``failed`` outcome, and
        an exception (or unpublishable receipt) leaves the intent uncertain.
        """
        effect = _STAGE_EFFECTS[stage]
        if self._effect_binding_factory is None:
            return invoke()
        binding = self._effect_binding_factory(run_id)
        if not isinstance(binding, AuthenticatedWorkerBinding):
            raise TypeError("effect_binding_factory returned an invalid binding")
        if effect.repeatable:
            attempt = self._next_stage_attempt(binding, run_id, effect)
            operation_id = f"{effect.operation}:{run_id}:attempt-{attempt}"
            receipt_key = f"selfmod:{run_id}:{effect.receipt}:attempt-{attempt}"
        else:
            # One-shot stages keep one identity per run; a second call is a
            # duplicate intent and refuses.  Deploy/rollback ids are unchanged.
            operation_id = f"{effect.operation}:{run_id}"
            receipt_key = f"selfmod:{run_id}:{effect.receipt}"
        return journaled_effect(
            binding,
            operation_id=operation_id,
            idempotency_key=receipt_key,
            request=request,
            invoke=invoke,
            receipt_key=receipt_key,
            reconciliation="manual",
            success=effect.success,
        )

    @staticmethod
    def _next_stage_attempt(
        binding: AuthenticatedWorkerBinding, run_id: str, effect: _StageEffect,
    ) -> int:
        """Derive a repeatable stage's next attempt from durable journal state.

        Attempts are admitted contiguously, so the first missing attempt is
        the next one.  Every earlier attempt must be settled: an ``intent`` or
        ``uncertain`` attempt may have mutated the run without a receipt, so a
        retry refuses until trusted reconciliation settles it.  Two callers
        that derive the same attempt collide on its journal identity.
        """
        lookup = getattr(binding.journal, "get", None)
        if not callable(lookup):
            raise EffectJournalError("selfmod stage attempts require a keyed effect journal")
        for attempt in range(1, MAX_SELFMOD_STAGE_ATTEMPTS + 1):
            operation_id = f"{effect.operation}:{run_id}:attempt-{attempt}"
            prior = lookup(f"{binding.run_id}:{operation_id}")
            if prior is None:
                return attempt
            if prior.run_id != binding.run_id or prior.operation_id != operation_id:
                raise EffectJournalError("selfmod stage attempt identity conflicts with the journal")
            if prior.state in {EffectState.INTENT, EffectState.UNCERTAIN}:
                raise EffectJournalError(
                    f"a prior {effect.receipt} attempt is unresolved; "
                    "it requires reconciliation before a retry"
                )
        raise EffectJournalError(f"selfmod {effect.receipt} attempts are exhausted")

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


def _is_distinct_path(candidate: str, repository: str) -> bool:
    try:
        return Path(candidate).resolve(strict=False) != Path(repository).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return False


__all__ = [
    "GuardedLegacySelfmodService",
    "LegacySelfmodPort",
    "SelfModificationService",
    "SelfmodIntegrationState",
]
