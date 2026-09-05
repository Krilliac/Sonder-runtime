"""Recovery distinguishes persisted phases without creating resume authority."""

from dataclasses import replace
import json
import time
import pytest
from tests.test_delegated_verification import lanes
from tests.test_lane_pending_verification import setup_pending
from tests.test_lane_continuation import granted


@pytest.mark.parametrize(
    "phase,code",
    [
        ("admitted", ""),
        ("approval_deciding", ""),
        ("approval_pending", "APPROVAL_PENDING"),
        ("approved", "APPROVAL_PENDING"),
        ("running", "APPROVAL_PENDING"),
        ("certified", ""),
        ("failed", "VERIFICATION_REFUSED"),
        ("failed", "RECOVERED_INCOMPLETE"),
        ("stale", "PENDING_BUNDLE_CHANGED"),
        ("incomplete", "CLEANUP_UNRESOLVED"),
        ("approval_unknown", "APPROVAL_OUTCOME_UNKNOWN"),
    ],
)
def test_recovery_exposes_exact_phase_identity_and_code_without_mutation(
    lanes, phase, code
):
    host, bound, context, verifier, prepared, identity, gateway = setup_pending(lanes)
    with lanes[1].transaction() as tx:
        value = tx.verification_row(identity.verification_id, context.principal_id)
        value.update(
            state=phase,
            code=code,
            pending_approval=dict(
                tool="workspace_run",
                call_digest="a" * 64,
                surface="agent",
                call_id="a" * 16,
                expires_at=time.time() + 60,
            ),
        )
        tx.save_verification(value)
    bound.close()

    with lanes[1].connect() as conn:
        before = [
            tuple(row) for row in conn.execute("SELECT * FROM agent_verifications")
        ]
        bindings_before = [
            tuple(row) for row in conn.execute("SELECT * FROM agent_lane_continuations")
        ]
    item = host.recovery_page(context, limit=1).items[0]
    assert item.verification_phase == phase
    assert item.verification_code == value["code"]
    assert item.pending_identity == identity
    assert item.pending_approval.call_digest == "a" * 64
    assert item.attachment_state == "active"
    with lanes[1].connect() as conn:
        assert [
            tuple(row) for row in conn.execute("SELECT * FROM agent_verifications")
        ] == before
        assert [
            tuple(row) for row in conn.execute("SELECT * FROM agent_lane_continuations")
        ] == bindings_before
    assert gateway.calls == 0


def test_reattached_host_reads_original_prepared_without_new_fingerprint(lanes):
    host, bound, context, verifier, prepared, identity, gateway = setup_pending(lanes)
    bound.close()
    fresh_context = replace(
        context, correlation_id="fresh-host", deadline_monotonic=time.monotonic() + 600
    )
    selection = host.select(identity.continuation_id, fresh_context)
    attachment = host.prepare_reattachment(
        selection, fresh_context, command_id="reattach"
    )
    fresh = host.execute_reattachment(attachment, fresh_context, approve=granted)
    with lanes[1].connect() as conn:
        before = [
            tuple(row) for row in conn.execute("SELECT * FROM agent_lane_continuations")
        ]
    assert fresh.prepared_verification(identity) == prepared
    assert (
        fresh.prepared_verification(identity).context_fingerprint
        == prepared.context_fingerprint
    )
    with lanes[1].connect() as conn:
        assert [
            tuple(row) for row in conn.execute("SELECT * FROM agent_lane_continuations")
        ] == before
    with pytest.raises(PermissionError):
        fresh.prepared_verification(replace(identity, verification_id="foreign"))
    fresh.close()
    with pytest.raises(PermissionError):
        fresh.prepared_verification(identity)
    assert gateway.calls == 0


def test_corrupt_prepared_link_is_unavailable_metadata_and_refuses_private_read(lanes):
    host, bound, context, verifier, prepared, identity, gateway = setup_pending(lanes)
    with lanes[1].transaction() as tx:
        value = tx.verification_row(identity.verification_id, context.principal_id)
        value["prepared"]["context_fingerprint"] = "tampered"
        tx.save_verification(value)
    with pytest.raises(ValueError):
        bound.prepared_verification(identity)
    item = host.recovery_page(context, limit=1).items[0]
    assert item.verification_phase == "unavailable"
    assert item.verification_code == "RECOVERY_METADATA_UNAVAILABLE"
    assert item.pending_identity is None
    assert gateway.calls == 0
    bound.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("host_conversation_id", "foreign-host"),
        ("parent_session_id", "foreign-parent"),
        ("parent_grant_revision", 999),
    ],
)
def test_corrupt_sealed_binding_refuses_original_prepared_and_recovery(
    lanes, field, value
):
    host, bound, context, verifier, prepared, identity, gateway = setup_pending(lanes)
    with lanes[1].transaction() as tx:
        row = tx.conn.execute(
            "SELECT binding FROM agent_lane_terminal_projections WHERE continuation_id=?",
            (identity.continuation_id,),
        ).fetchone()
        binding = json.loads(row["binding"])
        binding[field] = value
        tx.conn.execute(
            "UPDATE agent_lane_terminal_projections SET binding=? WHERE continuation_id=?",
            (json.dumps(binding), identity.continuation_id),
        )
    with pytest.raises(ValueError, match="projection link integrity"):
        bound.prepared_verification(identity)
    item = host.recovery_page(context, limit=1).items[0]
    assert item.verification_phase == "unavailable"
    assert item.verification_code == "RECOVERY_METADATA_UNAVAILABLE"
    assert item.pending_identity is None
    assert gateway.calls == 0
    bound.close()
