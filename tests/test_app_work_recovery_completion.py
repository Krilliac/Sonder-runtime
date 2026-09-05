"""Current-authority recovery completion preserves original work and exact receipt."""

from dataclasses import replace
import pytest
from tests.test_app_control_store import state, OWNER
from tests.test_app_work_recovery_history import recovered_selection
from tests.test_app_managed_work_store import admit
from sonder_runtime.adapters.persistence.app_control import SQLiteAppControlStore
from sonder_runtime.application.ports.app_control import CommandKey, CommandConflict
from sonder_runtime.application.ports.app_managed_work import WorkCompletionEvidence
from sonder_runtime.application.ports.lane_continuation import TerminalProjectionReceipt


def completion(original):
    pending = original.verification_pending
    return WorkCompletionEvidence(
        "certified_after_return",
        pending.identity,
        TerminalProjectionReceipt(
            "publication1", "5" * 64, pending.identity.projection_digest, "6" * 64, 2
        ),
    )


def finish(store, original, scope, evidence, **changes):
    args = dict(
        scope,
        expected_revision=original.revision,
        terminal=original.verification_pending.original_terminal,
        completion=evidence,
    )
    args.update(changes)
    return store.atomic(
        lambda tx: tx.complete_recovery_work(
            CommandKey(OWNER, "control:session2", "recover1"), **args
        )
    )


def test_recovery_completion_has_new_receipt_without_reviving_original(state):
    store, original, scope = recovered_selection(state)
    later = SQLiteAppControlStore(store.path, clock=lambda: 750)
    evidence = completion(original)
    result = finish(later, original, scope, evidence)
    assert result.state == "terminal" and result.revision == 6
    assert result.prepared == original.prepared
    assert result.dispatch_id == original.dispatch_id
    assert result.process_incarnation == original.process_incarnation
    assert result.verification_pending == original.verification_pending
    assert result.completion == evidence
    assert finish(later, original, scope, evidence) == result
    with pytest.raises(CommandConflict):
        admit(later)
    row = later.atomic(
        lambda tx: tx._conn.execute(
            "SELECT action,receipt FROM app_control_commands WHERE id='recover1'"
        ).fetchone()
    )
    assert row[0] == "complete_work_recovery"
    assert '"entity_revision":6' in row[1]


@pytest.mark.parametrize("change", ["phase", "receipt", "revision"])
def test_changed_completion_retry_conflicts(state, change):
    store, original, scope = recovered_selection(state)
    evidence = completion(original)
    finish(store, original, scope, evidence)
    kwargs = {}
    if change == "phase":
        evidence = replace(evidence, phase="certified")
    elif change == "receipt":
        evidence = replace(
            evidence,
            publication_receipt=replace(
                evidence.publication_receipt, receipt_id="different"
            ),
        )
    else:
        kwargs["expected_revision"] = original.revision + 1
    with pytest.raises(CommandConflict):
        finish(store, original, scope, evidence, **kwargs)


def test_uncertified_recovery_cannot_free_binding(state):
    store, original, scope = recovered_selection(state)
    with pytest.raises((ValueError, CommandConflict)):
        finish(store, original, scope, WorkCompletionEvidence("not_required"))
    assert store.atomic(lambda tx: tx.read_recovery_work(**scope)) == original


def test_receipt_failure_rolls_back_terminal_mutation(state, monkeypatch):
    from sonder_runtime.application.ports.app_control import StoreUnavailable

    store, original, scope = recovered_selection(state)

    def fail(tx):
        monkeypatch.setattr(
            tx, "_finish", lambda *args: (_ for _ in ()).throw(RuntimeError("lost"))
        )
        return tx.complete_recovery_work(
            CommandKey(OWNER, "control:session2", "recover1"),
            **scope,
            expected_revision=original.revision,
            terminal=original.verification_pending.original_terminal,
            completion=completion(original)
        )

    with pytest.raises(StoreUnavailable):
        store.atomic(fail)
    assert store.atomic(lambda tx: tx.read_recovery_work(**scope)) == original


def test_lost_commit_response_replays_exact_completion(state, monkeypatch):
    from sonder_runtime.application.ports.app_control import OutcomeUnknown

    store, original, scope = recovered_selection(state)
    commit = store._commit

    def lost(conn):
        commit(conn)
        raise RuntimeError("response lost")

    monkeypatch.setattr(store, "_commit", lost)
    with pytest.raises(OutcomeUnknown):
        finish(store, original, scope, completion(original))
    reopened = SQLiteAppControlStore(store.path, clock=lambda: 100)
    observed = reopened.atomic(lambda tx: tx.read_recovery_work(**scope))
    assert observed.state == "terminal"
    assert finish(reopened, original, scope, completion(original)) == observed


def test_expired_current_selection_cannot_replay_committed_receipt(state):
    from sonder_runtime.application.ports.app_control import NotFound

    store, original, scope = recovered_selection(state)
    finish(store, original, scope, completion(original))
    later = SQLiteAppControlStore(store.path, clock=lambda: 901)
    with pytest.raises((CommandConflict, NotFound)):
        finish(later, original, scope, completion(original))


def test_pending_derived_unknown_can_complete_with_original_evidence(state):
    from sonder_runtime.application.ports.app_managed_work import WorkInterruption

    store, original, scope = recovered_selection(state)
    unknown = store.atomic(
        lambda tx: tx.mark_work_unknown(
            principal_id=OWNER,
            control_session_id="session1",
            work_id="work1",
            dispatch_id=original.dispatch_id,
            process_incarnation=original.process_incarnation,
            expected_revision=5,
            interruption=WorkInterruption(
                "verification_pending", "OWNER_INTERRUPTED", "4" * 64
            ),
        )
    )
    result = finish(store, unknown, scope, completion(unknown))
    assert result.state == "terminal" and result.revision == 7
    assert result.interruption == unknown.interruption
    assert result.prepared == original.prepared


def test_other_command_cannot_claim_existing_completion(state):
    store, original, scope = recovered_selection(state)
    finish(store, original, scope, completion(original))
    with pytest.raises(CommandConflict):
        store.atomic(
            lambda tx: tx.complete_recovery_work(
                CommandKey(OWNER, "control:session2", "different-command"),
                **scope,
                expected_revision=5,
                terminal=original.verification_pending.original_terminal,
                completion=completion(original)
            )
        )
