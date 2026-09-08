from dataclasses import replace
import hashlib
import json
import time

import pytest

from tests.test_delegated_verification import lanes
from tests.test_lane_pending_verification import setup_pending, pending
from tests.test_lane_continuation import granted


def setup(lanes, output='original answer\r\n'):
    from sonder_runtime.adapters.agent_terminal_evidence import HostObservationLedger
    from sonder_runtime.adapters.host_terminal_projection import TerminalProjectionCodec
    from sonder_runtime.bootstrap.standalone_continuation import HostTerminalPublisher

    codec = TerminalProjectionCodec()
    def original(binding):
        return codec.capture(binding=binding,
            ledger=HostObservationLedger(project_scope=str(lanes[3])),
            output=output, terminal_class='NORMAL', blockers=(),
            terminal_receipt_id='original-receipt')
    host, bound, context, verifier, prepared, identity, gateway = setup_pending(
        lanes, original_factory=(codec, original))
    publisher = HostTerminalPublisher(bound=bound, verifier=verifier,
        original_codec=codec, require_current=bound.require_current)
    host.terminal_result_codec = publisher.codec
    return publisher, host, bound, verifier, prepared, identity, gateway


def test_current_certificate_and_durable_receipt_publish_original_once(lanes):
    publisher, host, bound, verifier, prepared, identity, gateway = setup(lanes)
    bound.execute_verification(verifier, prepared, approve=granted)
    result = publisher.publish()
    assert result.output == 'original answer\r\n'
    assert result.valid is True
    assert result.receipt.revision == 2
    again = publisher.publish()
    assert again == result
    assert gateway.calls == 1
    bound.close()


def test_pending_approval_cannot_publish_or_run_a_check(lanes):
    publisher, host, bound, verifier, prepared, identity, gateway = setup(lanes)
    bound.execute_verification(verifier, prepared, approve=pending)
    with pytest.raises(PermissionError):
        publisher.publish()
    assert gateway.calls == 0
    bound.close()


def test_original_failure_is_not_rewritten_by_passing_child_checks(lanes):
    publisher, host, bound, verifier, prepared, identity, gateway = setup(lanes, 'CANCELLED')
    bound.execute_verification(verifier, prepared, approve=granted)
    result = publisher.publish()
    assert result.output == 'CANCELLED'
    assert result.valid is False
    bound.close()


def test_failed_terminal_commit_does_not_return_publishable_output(lanes, monkeypatch):
    publisher, host, bound, verifier, prepared, identity, gateway = setup(lanes)
    bound.execute_verification(verifier, prepared, approve=granted)
    def fail(*args):
        raise OSError('commit response unavailable')
    monkeypatch.setattr(bound, '_commit_terminal_projection_with_codec', fail)
    with pytest.raises(OSError, match='commit response'):
        publisher.publish()
    bound.close()


def test_each_publisher_uses_its_own_codec_after_another_is_installed(lanes):
    from sonder_runtime.bootstrap.standalone_continuation import HostTerminalPublisher
    publisher, host, bound, verifier, prepared, identity, gateway = setup(lanes)
    try:
        bound.execute_verification(verifier, prepared, approve=granted)
        original = bound.terminal_projection(identity)
        retained = publisher.codec.capture(original)
        later = HostTerminalPublisher(bound=bound, verifier=verifier,
            original_codec=host.projection_codec, require_current=bound.require_current)
        host.terminal_result_codec = later.codec
        with pytest.raises(PermissionError):
            bound._commit_terminal_projection_with_codec(identity,
                identity.projection_revision, retained, later.codec)
        first = publisher.publish()
        assert publisher.publish().receipt == first.receipt
        assert later.publish().receipt == first.receipt
        assert gateway.calls == 1
    finally:
        bound.close()


def test_fenced_attachment_cannot_publish(lanes):
    publisher, host, bound, verifier, prepared, identity, gateway = setup(lanes)
    bound.execute_verification(verifier, prepared, approve=granted)
    bound.close()
    with pytest.raises(PermissionError):
        publisher.publish()


def test_reattached_pending_verification_publishes_original_with_fresh_codec(lanes):
    from sonder_runtime.adapters.host_terminal_projection import TerminalProjectionCodec
    from sonder_runtime.bootstrap.standalone_continuation import HostTerminalPublisher

    publisher, host, bound, verifier, prepared, identity, gateway = setup(lanes)
    bound.execute_verification(verifier, prepared, approve=pending)
    bound.close()
    fresh_context = replace(lanes[4], correlation_id='reopened',
        deadline_monotonic=time.monotonic() + 600)
    attachment = host.prepare_reattachment(host.select(identity.continuation_id, fresh_context),
        fresh_context, command_id='reattach')
    fresh = host.execute_reattachment(attachment, fresh_context, approve=granted)
    original_codec = TerminalProjectionCodec()
    host.projection_codec = original_codec
    reopened = HostTerminalPublisher(bound=fresh, verifier=verifier,
        original_codec=original_codec, require_current=fresh.require_current)
    host.terminal_result_codec = reopened.codec
    verifier.resume_pending_approval(fresh, identity, approve=granted)
    published = reopened.publish()
    assert published.output == 'original answer\r\n'
    assert published.valid is True
    assert gateway.calls == 1
    fresh.close()


def test_lost_result_commit_response_replays_same_receipt_without_execution(lanes, monkeypatch):
    publisher, host, bound, verifier, prepared, identity, gateway = setup(lanes)
    bound.execute_verification(verifier, prepared, approve=granted)
    commit = bound._commit_terminal_projection_with_codec
    receipts = []
    def lost(*args):
        receipts.append(commit(*args))
        raise OSError('lost response after commit')
    with monkeypatch.context() as patch:
        patch.setattr(bound, '_commit_terminal_projection_with_codec', lost)
        with pytest.raises(OSError):
            publisher.publish()
    result = publisher.publish()
    assert result.receipt == receipts[0]
    assert gateway.calls == 1
    bound.close()


@pytest.mark.parametrize('field,value', [
    ('job_id', 'other-job'), ('parent_session_id', 'other-parent'),
    ('principal_id', 'other-principal'), ('process_exited', False),
    ('containment_empty', 1), ('resources_released', False),
    ('status', 'failed'), ('exit_code', True), ('digest', 'broken'),
])
def test_malformed_stored_cleanup_proof_never_publishes(lanes, field, value):
    publisher, host, bound, verifier, prepared, identity, gateway = setup(lanes)
    bound.execute_verification(verifier, prepared, approve=granted)
    with lanes[1].transaction() as tx:
        row = tx.verification_row(prepared.verification_id, prepared.principal_id)
        row['certificate']['cleanup_proofs'][0][field] = value
        if field != 'digest':
            proof = row['certificate']['cleanup_proofs'][0]
            proof['digest'] = hashlib.sha256(json.dumps(
                {key: item for key, item in proof.items() if key != 'digest'},
                sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        tx.save_verification(row)
    with pytest.raises(PermissionError):
        publisher.publish()
    with lanes[1].transaction() as tx:
        assert tx.conn.execute('SELECT COUNT(*) FROM agent_lane_terminal_results').fetchone()[0] == 0
    bound.close()
