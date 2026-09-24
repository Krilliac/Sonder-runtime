from dataclasses import replace
from types import SimpleNamespace
import pytest
from tests.test_delegated_verification import lanes
from tests.test_managed_standalone_session import setup
from sonder_runtime.bootstrap.managed_conversation import ManagedConversationLifetime
from sonder_runtime.application.ports.host_final import HostFinalFacts
from sonder_runtime.interfaces.standalone_agent_lanes import HostTerminalDraft
from sonder_runtime.adapters.agent_terminal_evidence import HostObservationLedger
from sonder_runtime.application.ports.delegated_verification import PreparedCheck, PreparedVerification, digest
from sonder_runtime.application.ports.lane_continuation import PendingVerificationIdentity
from sonder_runtime.bootstrap.managed_terminal_eligibility import terminal_eligibility


@pytest.mark.parametrize(
    "delegated,terminal_class,code,eligible",
    [
        (False, "NORMAL", "", True),
        (None, "NORMAL", "", False),
        (True, "NORMAL", "", False),
        (True, "UNVERIFIED", "ORIGINAL_PARENT_EVIDENCE_FAILED", True),
        (True, "UNVERIFIED", "UNKNOWN_FUTURE_CODE", False),
    ],
)
def test_no_pending_requires_explicit_sealed_delegation_fact(
    lanes, delegated, terminal_class, code, eligible
):
    app = object()
    lifetime = ManagedConversationLifetime(
        application=app,
        session_factory=lambda c, a: setup(lanes, c)[0],
        require_current=lambda: None,
    )
    try:
        turn = lifetime.factory(SimpleNamespace(run_id="eligibility-turn"), app)
        link = turn.turn_link()
        turn.capture_terminal(
            HostTerminalDraft(
                HostObservationLedger(project_scope=str(lanes[3])).seal(),
                "original",
                "NORMAL",
                (),
            )
        )
        output = (
            "UNVERIFIED: original" if terminal_class == "UNVERIFIED" else "original"
        )
        turn.stage_final(
            HostFinalFacts(
                (),
                str(lanes[3]),
                False,
                False,
                False,
                terminal_class,
                certificate_code=code,
                delegated_work=delegated,
            )
        )
        turn.close()
        finalized = lifetime.finalize_result_with_receipt(output)

        def forbidden(*args):
            pytest.fail("no-pending eligibility must not construct a verifier")

        view = lifetime.terminal_eligibility(link, verifier_factory=forbidden)
        assert view.eligible is eligible
        assert view.evidence.result == finalized and view.published is None
        with pytest.raises(PermissionError):
            lifetime.terminal_eligibility(
                replace(link, parent_session_id="foreign"), verifier_factory=forbidden
            )
    finally:
        lifetime.close()


def test_current_host_attachment_accepts_only_the_durable_failed_check_receipt(
    monkeypatch,
):
    from tests.test_receipt_observation import _evidence

    evidence = _evidence(principal="owner", run_id="negative-live")
    root = evidence.facts.project_scope
    identity = PendingVerificationIdentity(
        "continuation-1", "verification-1", "parent-1", 1, 1,
        "b" * 64, "command-1", "a" * 64, 1,
    )
    check = PreparedCheck("unit", "catalog", "argv", root)
    prepared = PreparedVerification(
        "verification-1", "parent-1", "owner", 1, 1,
        (("lane-worker-a", 1, 1),), (root,), (check,), "context", "bundle",
    )
    prepared = replace(prepared, bundle_digest=digest(prepared.approval_payload()))
    proof = {
        "job_id": "lane-test-verification-1-0",
        "parent_session_id": "parent-1",
        "principal_id": "owner",
        "process_exited": True,
        "containment_empty": True,
        "resources_released": True,
        "status": "failed",
        "exit_code": 7,
        "digest": "proof",
    }
    failed_check = {
        "target": check.target, "catalog_digest": check.catalog_digest,
        "argv_digest": check.argv_digest, "workspace_root": check.workspace_root,
        "argv": [],
    }
    failure = {
        "schema": "delegated-verification-failure-v1",
        "verification_id": "verification-1", "generation": 1,
        "bundle": prepared.approval_payload(), "failed_check_index": 0,
        "failed_check": failed_check, "failed_job_id": proof["job_id"],
        "failed_proof": proof, "before_manifest_digest": "d" * 64,
        "after_manifest_digest": "d" * 64, "cleanup_proofs": [proof],
    }
    failure["receipt_digest"] = digest(failure)
    view = {"state": "failed", "code": "VERIFICATION_CHECK_FAILED",
            "job_ids": [proof["job_id"]], "failure_receipt": failure}

    class Snapshotter:
        def capture(self, roots):
            return SimpleNamespace(digest="d" * 64)

    class Verifier:
        snapshotter = Snapshotter()
        def _proof(self, *args):
            return proof
        def _require_current(self, *args, **kwargs):
            return None

    class Bound:
        def pending_verification(self): return identity
        def verification_view(self, *args, **kwargs): return view
        def prepared_verification(self, supplied):
            assert supplied == identity
            return prepared

    class Session:
        _bound = Bound()
        context = SimpleNamespace(principal_id="owner")
        def require_current(self): return None
        def final_evidence(self, expected): return evidence
        def _compose_verifier(self, factory): self._verifier = Verifier()

    monkeypatch.setattr(
        "sonder_runtime.bootstrap.managed_terminal_eligibility.require_host_pending_turn",
        lambda *args: None,
    )
    result = terminal_eligibility(Session(), evidence.result.receipt.turn, verifier_factory=object())
    assert result.eligible is False
    assert result.phase == "failed"
    assert result.authority is not None
    assert result.verified_failure_receipt["failed_check_index"] == 0
    view["failure_receipt"]["failed_proof"]["exit_code"] = 0
    spoofed = terminal_eligibility(Session(), evidence.result.receipt.turn, verifier_factory=object())
    assert spoofed.eligible is False
    assert spoofed.code == "FAILURE_RECEIPT_INVALID"
