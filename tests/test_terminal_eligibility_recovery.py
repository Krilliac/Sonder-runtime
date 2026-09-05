import hashlib
import json
import time
from types import SimpleNamespace

from sonder_runtime.adapters.agent_terminal_evidence import HostObservationLedger
from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
from sonder_runtime.application.ports.host_final import HostFinalFacts
from sonder_runtime.application.ports.lane_continuation import (
    VerificationApprovalPending,
)
from sonder_runtime.bootstrap.managed_conversation import ManagedConversationLifetime
from sonder_runtime.interfaces.standalone_agent_lanes import HostTerminalDraft
from tests.test_continuation_approval_bridge import bridge
from tests.test_delegated_verification import lanes, _verifier
from tests.test_managed_standalone_session import setup, command


def test_pending_then_certified_original_turn_becomes_terminally_eligible(lanes):
    app = object()
    ledger = ApprovalLedger(lanes[3].parent / "eligibility-recovery-approvals.db")
    gate = bridge(ledger)

    def create(controller, application):
        owner, _, _, _ = setup(lanes, controller)

        def approve(prepared, context):
            return gate.authorize(
                "workspace_run",
                prepared.approval_payload(),
                surface="agent",
                expires_at=time.time() + min(120, context.remaining_seconds),
            )

        owner._approve = approve
        return owner

    lifetime = ManagedConversationLifetime(
        application=app, session_factory=create, require_current=lambda: None
    )
    controller = SimpleNamespace(run_id="pending-recovery-turn")
    try:
        view = lifetime.factory(controller, app)
        child = view.dispatch(
            command(
                view,
                controller,
                "spawn",
                {
                    "command_id": "child",
                    "task": "inspect",
                    "workspace_root": str(lanes[3]),
                },
            )
        )["lane"]
        lanes[0].run_pending(child["id"], view.context)
        verifier, gateway, proofs = _verifier(lanes)
        execute = gateway.execute_check

        def checked(*args, **kwargs):
            execute(*args, **kwargs)
            for proof in proofs.values():
                contents = {
                    key: value for key, value in proof.items() if key != "digest"
                }
                proof["digest"] = hashlib.sha256(
                    json.dumps(contents, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()

        gateway.execute_check = checked
        draft = HostTerminalDraft(
            HostObservationLedger(project_scope=str(lanes[3])).seal(),
            "exact pending",
            "NORMAL",
            (),
        )
        view.capture_terminal(draft)
        assert not view.verify_delegated(
            draft, verifier_factory=lambda *args: verifier
        ).valid
        identity = view._session._bound.pending_verification()
        turn_link = view.turn_link()
        view.capture_final(
            "UNVERIFIED: exact pending",
            HostFinalFacts(
                (),
                str(lanes[3]),
                False,
                False,
                False,
                "UNVERIFIED",
                delegated_work=True,
            ),
        )
        view.close()
        before = lifetime.terminal_eligibility(
            turn_link, verifier_factory=lambda *args: verifier
        )
        assert before.phase == "approval_pending" and not before.eligible
        pending = ledger.resolve_call(before.pending_approval.call_digest)
        ledger.issue(pending.tool, pending.digest, approver="operator")
        verdict = lifetime._owner.resume_pending_verification(
            identity, verifier_factory=lambda *args: verifier
        )
        assert verdict.valid and gateway.calls == 1
        after = lifetime.terminal_eligibility(
            turn_link, verifier_factory=lambda *args: verifier
        )
        assert after.eligible and after.phase == "certified_after_return"
        assert after.code == "RECOVERED_CERTIFIED"
        assert after.evidence == before.evidence
        assert after.evidence.facts.validation_passed is False
        assert after.evidence.result.output == "UNVERIFIED: exact pending"
        assert after.published is not None and after.published.valid
    finally:
        lifetime.close()
