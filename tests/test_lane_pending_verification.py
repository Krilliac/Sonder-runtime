"""Only proven no-effect pending approval can continue across host attachment."""

from dataclasses import replace
import time
import pytest
from tests.test_delegated_verification import lanes
from tests.test_lane_continuation import make_host, granted
from tests.test_lane_continuation_projection import Codec, HostProjection


def setup_pending(lanes):
    from sonder_runtime.application.agents.delegated_verification import (
        DelegatedVerificationService,
    )
    from sonder_runtime.application.ports.delegated_verification import PreparedCheck
    from sonder_runtime.application.ports.lane_continuation import ProjectionBinding
    from sonder_runtime.adapters.filesystem.workspace_manifest import (
        WorkspaceSnapshotter,
    )

    host, context, parent, current = make_host(lanes)
    child = lanes[0].spawn(
        command_id="child",
        parent_session_id=parent["parent_session_id"],
        task="Complete bounded task",
        workspace_root=str(lanes[3]),
        context=context,
    )["lane"]["id"]
    lanes[0].run_pending(child, context)
    bound = host.register_parent(
        parent["parent_session_id"],
        parent["parent_token"],
        "host-task",
        context=context,
        command_id="register",
    )
    proofs = {}

    class Gateway:
        calls = 0

        def prepare_checks(self, roots):
            return (PreparedCheck("unit", "catalog", "argv", roots[0]),)

        def require_current(self, checks):
            assert checks == self.prepare_checks((str(lanes[3]),))

        def execute_check(self, check, call_id, parent, context, *, permit):
            self.calls += 1
            job = "lane-test-" + call_id
            proofs[job] = dict(
                job_id=job,
                parent_session_id=parent,
                principal_id=context.principal_id,
                process_exited=True,
                containment_empty=True,
                resources_released=True,
                digest="proof",
                status="succeeded",
                exit_code=0,
            )

    gateway = Gateway()
    verifier = DelegatedVerificationService(
        lanes[0], gateway, lambda job: proofs.get(job), WorkspaceSnapshotter()
    )
    prepared = bound.prepare_verification(verifier, command_id="verify")
    codec = Codec()
    host.projection_codec = codec
    binding = ProjectionBinding(
        bound.continuation_id,
        context.principal_id,
        "run",
        "host-task",
        parent["parent_session_id"],
        1,
        prepared.verification_id,
        prepared.bundle_digest,
        prepared.roots,
        1,
    )
    projection = HostProjection(binding, True, "VALIDATION_FAILED", codec.issuer)
    identity = bound.link_pending_verification(verifier, prepared, projection)
    return host, bound, context, verifier, prepared, identity, gateway


def pending(*args):
    from sonder_runtime.application.ports.lane_continuation import (
        PendingApprovalEvidence,
        VerificationApprovalPending,
    )

    raise VerificationApprovalPending(
        PendingApprovalEvidence(
            "workspace_run", "a" * 64, "agent", "a" * 16, time.time() + 60
        )
    )


def test_pending_releases_barrier_and_explicit_resume_preserves_original_projection(
    lanes,
):
    host, bound, context, verifier, prepared, identity, gateway = setup_pending(lanes)
    first = bound.execute_verification(verifier, prepared, approve=pending)
    assert first["state"] == "approval_pending"
    assert first["job_ids"] == [] and gateway.calls == 0
    with lanes[1].transaction() as tx:
        assert not tx.verification_barrier(
            prepared.parent_session_id, context.principal_id
        )
    # Ordinary retry must not ask/spend again. Only the explicit bound resume may.
    assert (
        bound.execute_verification(verifier, prepared, approve=granted)["state"]
        == "approval_pending"
    )
    for _ in range(2):
        assert (
            verifier.resume_pending_approval(bound, identity, approve=pending)["state"]
            == "approval_pending"
        )
        with lanes[1].transaction() as tx:
            assert not tx.verification_barrier(
                prepared.parent_session_id, context.principal_id
            )
    bound.close()
    fresh_context = replace(
        context, correlation_id="reattached", deadline_monotonic=time.monotonic() + 600
    )
    attachment = host.prepare_reattachment(
        host.select(identity.continuation_id, fresh_context),
        fresh_context,
        command_id="reattach",
    )
    fresh = host.execute_reattachment(attachment, fresh_context, approve=granted)
    result = verifier.resume_pending_approval(fresh, identity, approve=granted)
    assert result["state"] == "certified" and gateway.calls == 1
    assert fresh.terminal_projection(identity).dirty is True
    assert fresh.terminal_projection(identity).terminal_class == "VALIDATION_FAILED"
    assert (
        verifier.resume_pending_approval(fresh, identity, approve=granted)["state"]
        == "certified"
    )
    assert gateway.calls == 1
    fresh.close()


def test_generic_approval_failure_is_unknown_and_cannot_reenter_gate(lanes):
    host, bound, context, verifier, prepared, identity, gateway = setup_pending(lanes)

    def lost(*args):
        raise OSError("lost approval response after possible spend")

    first = bound.execute_verification(verifier, prepared, approve=lost)
    assert first["code"] == "APPROVAL_OUTCOME_UNKNOWN"
    assert gateway.calls == 0
    assert (
        verifier.resume_pending_approval(bound, identity, approve=granted)["code"]
        == "APPROVAL_OUTCOME_UNKNOWN"
    )
    assert gateway.calls == 0
    bound.close()


@pytest.mark.parametrize("phase", ["approval_deciding", "approved"])
def test_kernel_proven_dead_no_intent_owner_releases_barrier_without_replay(
    lanes, phase
):
    host, bound, context, verifier, prepared, identity, gateway = setup_pending(lanes)

    class SimulatedProcessExit(BaseException):
        pass

    def die(*args):
        raise SimulatedProcessExit()

    if phase == "approved":
        verifier.snapshotter.capture = die
    with pytest.raises(SimulatedProcessExit):
        bound.execute_verification(
            verifier, prepared, approve=die if phase == "approval_deciding" else granted
        )
    with lanes[1].transaction() as tx:
        value = tx.verification_row(prepared.verification_id, context.principal_id)
        assert value["state"] == phase and value["job_ids"] == []
        assert lanes[1].owner_definitely_stopped(value["owner"]) is True
    result = bound.verification_view(
        verifier, prepared.verification_id, action="reconcile"
    )
    assert result["code"] == "APPROVAL_OUTCOME_UNKNOWN"
    with lanes[1].transaction() as tx:
        assert not tx.verification_barrier(
            prepared.parent_session_id, context.principal_id
        )
    assert (
        verifier.resume_pending_approval(bound, identity, approve=granted)["state"]
        == "approval_unknown"
    )
    assert gateway.calls == 0
    bound.close()


def test_actual_ledger_exact_pending_issue_and_consume_receipt(lanes):
    from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
    from sonder_runtime.application.ports.lane_continuation import (
        PendingApprovalEvidence,
        GrantedApprovalEvidence,
        VerificationApprovalPending,
    )
    from sonder_runtime.application.ports.delegated_verification import digest

    host, bound, context, verifier, prepared, identity, gateway = setup_pending(lanes)
    ledger = ApprovalLedger(lanes[3].parent / "approvals.db")
    call_digest = digest(prepared.approval_payload())

    def gate(bundle, ctx):
        assert digest(bundle.approval_payload()) == call_digest
        spent = ledger.consume("workspace_run", call_digest, surface="agent")
        if spent:
            return GrantedApprovalEvidence(
                spent.tool,
                spent.digest,
                "agent",
                spent.nonce,
                spent.nonce,
                spent.expires_ts,
                "approval",
            )
        row = ledger.record_pending("workspace_run", call_digest, surface="agent")
        assert ledger.resolve_call(call_digest) == row
        raise VerificationApprovalPending(
            PendingApprovalEvidence(
                row.tool,
                row.digest,
                row.surface,
                row.call_id,
                min(row.first_ts + 600, time.time() + 120),
            )
        )

    assert (
        bound.execute_verification(verifier, prepared, approve=gate)["state"]
        == "approval_pending"
    )
    issued = ledger.issue(
        "workspace_run", call_digest, approver="test-host", surface="agent"
    )
    assert (
        verifier.resume_pending_approval(bound, identity, approve=gate)["state"]
        == "certified"
    )
    with lanes[1].transaction() as tx:
        value = tx.verification_row(prepared.verification_id, context.principal_id)
        assert value["approval_evidence"]["approval_nonce"] == issued.nonce
    assert ledger.consume("workspace_run", call_digest, surface="agent") is None
    assert gateway.calls == 1
    bound.close()


def test_accepted_steering_invalidates_pending_before_gate(lanes):
    host, bound, context, verifier, prepared, identity, gateway = setup_pending(lanes)
    assert (
        bound.execute_verification(verifier, prepared, approve=pending)["state"]
        == "approval_pending"
    )
    command = object()

    class Commands:
        def decode_command(self, value):
            assert value is command
            return "send_message", dict(
                lane_id=prepared.children[0][0],
                command_id="steer",
                content="Address new edge case",
            )

    host.command_codec = Commands()
    bound.dispatch(command)
    calls = []
    assert (
        verifier.resume_pending_approval(
            bound, identity, approve=lambda *a: calls.append("gate")
        )["state"]
        == "stale"
    )
    assert calls == [] and gateway.calls == 0
    bound.close()


def test_terminal_projection_result_is_once_only_and_keeps_original_failure(lanes):
    from sonder_runtime.application.ports.delegated_verification import digest

    host, bound, context, verifier, prepared, identity, gateway = setup_pending(lanes)
    bound.execute_verification(verifier, prepared, approve=pending)
    certificate = verifier.resume_pending_approval(bound, identity, approve=granted)[
        "certificate"
    ]
    original = bound.terminal_projection(identity)
    codec = Codec()
    result = HostProjection(
        replace(original.binding, revision=2), True, "VALIDATION_FAILED", codec.issuer
    )
    codec.certificate_digest = lambda value: (
        digest(certificate) if value is result else "invalid"
    )
    host.terminal_result_codec = codec
    receipt = bound.commit_terminal_projection(identity, 1, result)
    assert bound.commit_terminal_projection(identity, 1, result) == receipt
    with pytest.raises(ValueError):
        bound.commit_terminal_projection(identity, 2, result)
    assert bound.terminal_projection(identity).terminal_class == "VALIDATION_FAILED"
    bound.close()
