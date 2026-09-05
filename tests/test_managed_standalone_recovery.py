from dataclasses import replace
import hashlib
import json
import time
from types import SimpleNamespace
import pytest

from tests.test_delegated_verification import lanes, _verifier
from tests.test_managed_standalone_session import setup, command
from tests.test_continuation_approval_bridge import bridge
from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
from sonder_runtime.application.ports.lane_continuation import (
    VerificationApprovalPending,
)


def pending_session(lanes):
    from sonder_runtime.adapters.agent_terminal_evidence import HostObservationLedger
    from sonder_runtime.interfaces.standalone_agent_lanes import HostTerminalDraft

    session, controller, host, current = setup(lanes)
    child = session.dispatch(
        command(
            session,
            controller,
            "spawn",
            dict(command_id="child", task="inspect", workspace_root=str(lanes[3])),
        )
    )["lane"]
    lanes[0].run_pending(child["id"], session.context)
    verifier, gateway, proofs = _verifier(lanes)
    execute = gateway.execute_check

    def checked(*args, **kwargs):
        execute(*args, **kwargs)
        for proof in proofs.values():
            proof["digest"] = hashlib.sha256(
                json.dumps(
                    {key: value for key, value in proof.items() if key != "digest"},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()

    gateway.execute_check = checked
    ledger = ApprovalLedger(lanes[3].parent / "recovery-approvals.db")
    gate = bridge(ledger)

    def approve(prepared, context):
        return gate.authorize(
            "workspace_run",
            prepared.approval_payload(),
            surface="agent",
            expires_at=time.time() + min(120, context.remaining_seconds),
        )

    session._approve = approve
    draft = HostTerminalDraft(
        HostObservationLedger(project_scope=str(lanes[3])).seal(),
        "exact original answer",
        "NORMAL",
        (),
    )
    assert (
        session.verify_delegated(draft, verifier_factory=lambda *args: verifier).valid
        is False
    )
    identity = session._bound.pending_verification()
    context = session.context
    session.close()
    return (
        session,
        host,
        current,
        verifier,
        gateway,
        ledger,
        approve,
        identity,
        context,
        draft,
    )


def recovery(lanes, host, context, approve):
    from sonder_runtime.bootstrap.managed_standalone import ManagedStandaloneRecovery
    from sonder_runtime.application.agents.lane_continuation import (
        LaneContinuationService,
    )
    from sonder_runtime.adapters.host_terminal_projection import TerminalProjectionCodec

    fresh_host = LaneContinuationService(
        lanes[0],
        authorize_host=host.authorize_host,
        projection_codec=TerminalProjectionCodec(),
        model_writable_roots=lambda: (lanes[3],),
    )
    return ManagedStandaloneRecovery(
        controller=SimpleNamespace(run_id="new-host"),
        application=object(),
        host=fresh_host,
        context=replace(
            context, correlation_id="fresh", deadline_monotonic=time.monotonic() + 300
        ),
        host_conversation_id="host-task",
        private_paths=lambda: (lanes[3].parent / "fleet.db",),
        model_writable_roots=lambda: (lanes[3],),
        approve_attachment=approve,
        approve_verification=approve,
    )


def test_real_ledger_pending_attachment_then_original_verification_resumes_once(lanes):
    old, host, current, verifier, gateway, ledger, approve, identity, context, draft = (
        pending_session(lanes)
    )
    coordinator = recovery(lanes, host, context, approve)
    prepared = coordinator.prepare(identity.continuation_id, command_id="reattach")
    with pytest.raises(VerificationApprovalPending) as pending:
        coordinator.execute(prepared)
    attachment_approval = ledger.issue(
        "workspace_run", pending.value.evidence.call_digest, approver="operator"
    )
    fresh = coordinator.execute(prepared)
    assert ledger.get(attachment_approval.nonce).spent
    assert fresh.original_terminal_draft() == draft
    view = fresh.recovery_verification(verifier_factory=lambda *args: verifier)
    assert view.identity == identity and view.phase == "approval_pending"
    assert view.prepared.bundle_digest == identity.bundle_digest
    assert (
        fresh.resume_pending_verification(
            identity, verifier_factory=lambda *args: verifier
        ).code
        == "APPROVAL_PENDING"
    )
    assert gateway.calls == 0
    pending_row = next(
        row
        for row in ledger.pending()
        if row.digest != pending.value.evidence.call_digest
    )
    verification_approval = ledger.issue(
        pending_row.tool, pending_row.digest, approver="operator"
    )
    verdict = fresh.resume_pending_verification(
        identity, verifier_factory=lambda *args: verifier
    )
    assert verdict.valid and ledger.get(verification_approval.nonce).spent
    assert fresh.published_terminal.output == draft.output
    assert fresh.resume_pending_verification(
        identity, verifier_factory=lambda *args: verifier
    ).valid
    assert gateway.calls == 1
    with pytest.raises(PermissionError):
        fresh.verify_delegated(
            replace(draft, output="replacement"),
            verifier_factory=lambda *args: verifier,
        )
    fresh.close()


def test_recovery_issuer_and_live_grant_revocation_refuse_before_attachment(lanes):
    old, host, current, verifier, gateway, ledger, approve, identity, context, draft = (
        pending_session(lanes)
    )
    coordinator = recovery(lanes, host, context, approve)
    prepared = coordinator.prepare(identity.continuation_id, command_id="reattach")
    with pytest.raises(PermissionError):
        coordinator.execute(replace(prepared, issuer=object()))
    current[0] = replace(current[0], revision=current[0].revision + 1)
    with pytest.raises(PermissionError):
        coordinator.execute(prepared)
    assert gateway.calls == 0


def attach(coordinator, identity, ledger):
    prepared = coordinator.prepare(identity.continuation_id, command_id="reattach")
    with pytest.raises(VerificationApprovalPending) as pending:
        coordinator.execute(prepared)
    ledger.issue(
        "workspace_run", pending.value.evidence.call_digest, approver="operator"
    )
    return coordinator.execute(prepared)


def test_consumed_verification_approval_lost_response_remains_unknown_without_replay(
    lanes,
):
    old, host, current, verifier, gateway, ledger, approve, identity, context, draft = (
        pending_session(lanes)
    )
    fresh = attach(recovery(lanes, host, context, approve), identity, ledger)
    pending_row = ledger.pending()[0]
    issued = ledger.issue(pending_row.tool, pending_row.digest, approver="operator")

    def uncertain(prepared, context):
        approve(prepared, context)
        raise OSError("simulated lost decision response")

    fresh._approve = uncertain
    result = fresh.resume_pending_verification(
        identity, verifier_factory=lambda *args: verifier
    )
    assert result.code == "APPROVAL_OUTCOME_UNKNOWN"
    assert ledger.get(issued.nonce).spent
    assert gateway.calls == 0
    fresh._approve = lambda *args: pytest.fail("unknown approval re-entered gate")
    assert (
        fresh.resume_pending_verification(
            identity, verifier_factory=lambda *args: verifier
        ).code
        == "APPROVAL_OUTCOME_UNKNOWN"
    )
    assert fresh.published_terminal is None
    assert fresh.original_terminal_draft() == draft
    fresh.close()


@pytest.mark.parametrize(
    "phase", ["admitted", "approval_deciding", "approved", "running", "incomplete"]
)
def test_nonresumable_phases_remain_observational(lanes, phase):
    old, host, current, verifier, gateway, ledger, approve, identity, context, draft = (
        pending_session(lanes)
    )
    fresh = attach(recovery(lanes, host, context, approve), identity, ledger)
    with lanes[1].transaction() as tx:
        value = tx.verification_row(identity.verification_id, context.principal_id)
        value.update(state=phase, code="")
        tx.save_verification(value)
    fresh._approve = lambda *args: pytest.fail("observational phase entered gate")
    assert (
        fresh.resume_pending_verification(
            identity, verifier_factory=lambda *args: verifier
        ).code
        == "RECOVERY_PHASE_NOT_RESUMABLE"
    )
    with lanes[1].transaction() as tx:
        assert (
            tx.verification_row(identity.verification_id, context.principal_id)["state"]
            == phase
        )
    assert gateway.calls == 0
    fresh.close()


def test_no_pending_projection_is_not_replaced_and_active_owner_is_not_stolen(
    lanes, monkeypatch
):
    from tests.test_lane_continuation import granted

    session, controller, host, current = setup(lanes)
    context = session.context
    continuation_id = session._bound.continuation_id
    coordinator = recovery(
        lanes, host, context, lambda *args: pytest.fail("active owner reached gate")
    )
    prepared = coordinator.prepare(continuation_id, command_id="live-owner")
    with pytest.raises(PermissionError):
        coordinator.execute(prepared)
    session.close()
    coordinator = recovery(lanes, host, context, granted)
    monkeypatch.setattr(
        lanes[0],
        "open_model_parent",
        lambda *args: pytest.fail("recovery minted parent"),
    )
    fresh = coordinator.execute(
        coordinator.prepare(continuation_id, command_id="closed-owner")
    )
    with pytest.raises(PermissionError, match="original terminal"):
        fresh.original_terminal_draft()
    assert fresh.report_metadata()["parent_session_id"] == session.parent_session_id
    current[0] = replace(current[0], workspace_roots=())
    with pytest.raises(PermissionError):
        fresh.report_metadata()
    fresh.close()
