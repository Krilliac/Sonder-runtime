from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from sonder_runtime.application.selfmod.reproducer_contract import (
    AcceptanceCriterion,
    FailureEvidence,
)
from sonder_runtime.application.selfmod.selfmod_service import (
    GuardedLegacySelfmodService,
)
from sonder_runtime.application.selfmod.verification_lifecycle import (
    FailureState,
    LifecyclePhase,
    VerificationKind,
)
from sonder_runtime.domain.common.errors import Forbidden


DIGEST = "a" * 64


@dataclass
class LegacyDouble:
    phase: str = ""
    calls: list[tuple[str, object]] = field(default_factory=list)

    def create_plan(self, objective, repository_root, **kwargs):
        self.phase = "proposed"
        return self._run(objective=objective, source_fingerprint=DIGEST)

    def get_run(self, run_id):
        return self._run()

    def create_backup(self, run_id):
        self.calls.append(("backup", run_id))
        self.phase = "backed_up"
        return self._run(backup_manifest="manifest.json")

    def prepare_workspace(self, run_id):
        self.calls.append(("prepare", run_id))
        self.phase = "editing"
        return self._run(
            workspace_path="C:/workspaces/candidate",
            branch_name="selfmod/candidate",
            starting_commit="commit-1",
        )

    def record_reproducer_before(self, run_id, command, timeout=None):
        self.calls.append(("reproducer", tuple(command)))
        return {"passed": True, "output": "known failure"}

    def begin_testing(self, run_id):
        self.calls.append(("begin_testing", run_id))
        self.phase = "testing"
        return self._run()

    def record_test(self, run_id, kind, command, **kwargs):
        self.calls.append((kind, tuple(command)))
        return {"passed": True, "output": f"{kind} passed"}

    def review(self, run_id, **kwargs):
        self.calls.append(("review", kwargs))
        self.phase = "reviewing"
        return self._run(last_error="")

    def approve(self, run_id, approver="user"):
        self.calls.append(("approve", approver))
        self.phase = "approved"
        return self._run(backup_manifest="manifest.json")

    def deploy(self, run_id, **kwargs):
        self.calls.append(("deploy", kwargs))
        self.phase = "deployed"
        return self._run(deployed_commit="commit-2")

    def rollback(self, run_id, reason="user requested rollback"):
        self.phase = "restored"
        return self._run()

    def _run(self, **overrides):
        result = {
            "id": "selfmod-test-1",
            "objective": "bounded change",
            "source_fingerprint": DIGEST,
            "phase": self.phase,
            "workspace_path": "C:/workspaces/candidate" if self.phase != "proposed" else "",
            "branch_name": "selfmod/candidate" if self.phase != "proposed" else "",
            "starting_commit": "commit-1",
            "backup_manifest": "manifest.json",
            "last_error": "",
        }
        result.update(overrides)
        return result


def failure_evidence() -> FailureEvidence:
    return FailureEvidence(
        evidence_id="failure-1",
        command_argv=("python", "-m", "pytest", "tests/test_target.py", "-q"),
        expected_outcome="exit 1 with E-TIMEOUT",
        artifact_digest=DIGEST,
        acceptance_criteria=(AcceptanceCriterion("c1", "failure is repeatable", "E-TIMEOUT"),),
        failure_signature="E-TIMEOUT",
    )


def _prepared() -> tuple[GuardedLegacySelfmodService, LegacyDouble]:
    legacy = LegacyDouble()
    service = GuardedLegacySelfmodService(legacy)
    service.create_plan(
        "bounded change", "C:/repo", evidence=("concrete failure",),
        files=("target.py",), criteria=("targeted check passes",),
    )
    service.prepare("selfmod-test-1")
    service.record_reproducer("selfmod-test-1", failure_evidence())
    return service, legacy


def test_guarded_bridge_requires_typed_evidence_before_legacy_review() -> None:
    service, _ = _prepared()
    with pytest.raises(Forbidden, match="verification evidence"):
        service.review("selfmod-test-1")


def test_guarded_bridge_runs_legacy_path_and_completes_typed_lifecycle() -> None:
    service, legacy = _prepared()
    for kind in VerificationKind:
        service.record_verification("selfmod-test-1", kind, ("python", "-c", "pass"))
    reviewed = service.review("selfmod-test-1")
    assert reviewed.lifecycle.phase is LifecyclePhase.REVIEWED
    approved = service.approve("selfmod-test-1", approver="operator")
    assert approved.lifecycle.phase is LifecyclePhase.BACKED_UP
    deployed = service.deploy("selfmod-test-1", health_command=("python", "-c", "pass"), commit=False)
    assert deployed.legacy_run["phase"] == "deployed"
    assert deployed.lifecycle.phase is LifecyclePhase.COMPLETED
    assert {call[0] for call in legacy.calls} >= {"reproducer", "targeted", "architecture", "regression", "smoke", "review", "approve", "deploy"}


def test_guarded_bridge_requires_explicit_health_command() -> None:
    service, _ = _prepared()
    for kind in VerificationKind:
        service.record_verification("selfmod-test-1", kind, ("python", "-c", "pass"))
    service.review("selfmod-test-1")
    service.approve("selfmod-test-1")
    with pytest.raises(Forbidden, match="health command"):
        service.deploy("selfmod-test-1", commit=False)


def test_unrestricted_bridge_delegates_without_new_typed_gates() -> None:
    legacy = LegacyDouble()
    service = GuardedLegacySelfmodService(legacy, unrestricted=True)
    service.create_plan(
        "unrestricted change", "C:/repo", evidence=("operator supplied",),
        files=("target.py",), criteria=("operator decides",),
    )
    service.prepare("selfmod-test-1")
    reviewed = service.review("selfmod-test-1")
    assert reviewed.legacy_run["phase"] == "reviewing"
    approved = service.approve("selfmod-test-1", approver="operator")
    assert approved.legacy_run["phase"] == "approved"
    deployed = service.deploy("selfmod-test-1", commit=False)
    assert deployed.legacy_run["phase"] == "deployed"
    assert deployed.lifecycle.phase is LifecyclePhase.PROPOSED


def test_bootstrap_exposes_the_lazy_guarded_bridge_without_import_time_root_load() -> None:
    import sys

    from sonder_runtime.bootstrap.app import build_application

    sys.modules.pop("selfmod", None)
    application = build_application()
    assert "selfmod" not in sys.modules
    service = application.selfmod_service()
    assert isinstance(service, GuardedLegacySelfmodService)
    assert "selfmod" not in sys.modules
