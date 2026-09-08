"""Pending verification stays durable evidence, never a new dispatch permit."""

from dataclasses import replace
import pytest
from tests.test_app_control_store import state, OWNER
from tests.test_app_managed_work_store import preparation, admit
from sonder_runtime.application.ports.app_managed_work import WorkVerificationPending
from sonder_runtime.application.ports.lane_continuation import (
    PendingVerificationIdentity,
    PendingApprovalEvidence,
)
from sonder_runtime.application.ports.host_turn_links import (
    ManagedHostTurnLink,
    ManagedHostTerminalLink,
)


def running(state):
    store, work = preparation(state)
    store.atomic(lambda tx: tx.prepare_work(work))
    admit(store)
    scope = dict(
        principal_id=OWNER,
        control_session_id="session1",
        work_id="work1",
        dispatch_id="dispatch1",
        process_incarnation="process1",
    )
    store.atomic(
        lambda tx: tx.bind_work_run(**scope, expected_revision=2, run_id="run1")
    )
    turn = ManagedHostTurnLink(
        "continuation1", "parent1", work.binding.canonical_host_id, OWNER, "run1", 1
    )
    store.atomic(
        lambda tx: tx.bind_work_host(**scope, expected_revision=3, host_turn=turn)
    )
    terminal = ManagedHostTerminalLink(
        turn, "original1", "a" * 64, "final1", "b" * 64, "c" * 64, "d" * 64
    )
    identity = PendingVerificationIdentity(
        "continuation1",
        "verification1",
        "parent1",
        1,
        0,
        "e" * 64,
        "verify1",
        "f" * 64,
        1,
    )
    approval = PendingApprovalEvidence(
        "workspace_run", "1" * 64, "app-control", "1" * 16, 600
    )
    return store, scope, WorkVerificationPending(identity, approval, terminal)


def test_pending_reopen_exact_retry_is_non_dispatchable(state):
    from sonder_runtime.adapters.persistence.app_control import SQLiteAppControlStore

    store, scope, pending = running(state)
    result = store.atomic(
        lambda tx: tx.record_work_verification_pending(
            **scope, expected_revision=4, pending=pending
        )
    )
    assert result.state == "verification_pending" and result.revision == 5
    reopened = SQLiteAppControlStore(store.path, clock=lambda: 100)
    assert (
        reopened.atomic(
            lambda tx: tx.record_work_verification_pending(
                **scope, expected_revision=4, pending=pending
            )
        )
        == result
    )
    assert not admit(reopened).newly_admitted
    assert admit(reopened).record.verification_pending == pending


@pytest.mark.parametrize("change", ["identity", "approval", "expiry", "output"])
def test_pending_replay_rejects_changed_evidence(state, change):
    from sonder_runtime.application.ports.app_control import CommandConflict

    store, scope, pending = running(state)
    first = store.atomic(
        lambda tx: tx.record_work_verification_pending(
            **scope, expected_revision=4, pending=pending
        )
    )
    if change == "identity":
        changed = replace(
            pending,
            identity=replace(pending.identity, verification_id="other-verification"),
        )
    elif change == "approval":
        changed = replace(
            pending,
            approval=replace(pending.approval, call_digest="2" * 64, call_id="2" * 16),
        )
    elif change == "expiry":
        changed = replace(pending, approval=replace(pending.approval, expires_at=650))
    else:
        changed = replace(
            pending,
            original_terminal=replace(
                pending.original_terminal, output_digest="3" * 64
            ),
        )
    with pytest.raises(CommandConflict):
        store.atomic(
            lambda tx: tx.record_work_verification_pending(
                **scope, expected_revision=4, pending=changed
            )
        )
    assert admit(store).record == first


def test_pending_unknown_and_completion_preserve_exact_original_evidence(state):
    from sonder_runtime.application.ports.app_managed_work import (
        WorkInterruption,
        WorkCompletionEvidence,
    )
    from sonder_runtime.application.ports.lane_continuation import (
        TerminalProjectionReceipt,
    )

    store, scope, pending = running(state)
    store.atomic(
        lambda tx: tx.record_work_verification_pending(
            **scope, expected_revision=4, pending=pending
        )
    )
    unknown = store.atomic(
        lambda tx: tx.mark_work_unknown(
            **scope,
            expected_revision=5,
            interruption=WorkInterruption(
                "verification_pending", "OWNER_INTERRUPTED", "4" * 64
            )
        )
    )
    assert unknown.state == "unknown" and unknown.verification_pending == pending
    for completion in (None, WorkCompletionEvidence("not_required")):
        with pytest.raises(ValueError):
            store.atomic(
                lambda tx: tx.record_work_terminal(
                    **scope,
                    expected_revision=6,
                    terminal=pending.original_terminal,
                    completion=completion
                )
            )
    publication = TerminalProjectionReceipt(
        "published", "5" * 64, pending.identity.projection_digest, "6" * 64, 2
    )
    completion = WorkCompletionEvidence(
        "certified_after_return", pending.identity, publication
    )
    result = store.atomic(
        lambda tx: tx.record_work_terminal(
            **scope,
            expected_revision=6,
            terminal=pending.original_terminal,
            completion=completion
        )
    )
    assert result.state == "terminal" and result.revision == 7
    assert result.verification_pending == pending and result.completion == completion
    assert result.terminal == pending.original_terminal
    assert (
        store.atomic(
            lambda tx: tx.record_work_verification_pending(
                **scope, expected_revision=4, pending=pending
            )
        )
        == result
    )
    assert not admit(store).newly_admitted


def test_historical_pending_free_encoding_and_corruption_refusal(state):
    import json
    from sonder_runtime.adapters.persistence.app_control import _encode, _decode
    from sonder_runtime.application.ports.app_managed_work import AppWorkRecord
    from sonder_runtime.application.ports.app_control import StoreUnavailable

    store, scope, pending = running(state)
    historical = admit(store).record
    raw = _encode(historical)
    assert "verification_pending" not in json.loads(raw)
    assert _decode(raw, AppWorkRecord) == historical
    result = store.atomic(
        lambda tx: tx.record_work_verification_pending(
            **scope, expected_revision=4, pending=pending
        )
    )
    assert _decode(_encode(result), AppWorkRecord) == result
    corrupted = json.loads(_encode(result))
    corrupted["verification_pending"]["original_terminal"]["turn"][
        "principal_id"
    ] = "account:foreign"
    with pytest.raises(StoreUnavailable):
        _decode(json.dumps(corrupted), AppWorkRecord)
    corrupted = json.loads(_encode(result))
    corrupted["verification_pending"]["approval"][
        "approval_nonce"
    ] = "not-a-pending-field"
    with pytest.raises(StoreUnavailable):
        _decode(json.dumps(corrupted), AppWorkRecord)


def test_pending_borrowed_write_rolls_back_with_caller(state):
    store, scope, pending = running(state)
    conn = store._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = store.atomic(
            lambda tx: tx.record_work_verification_pending(
                **scope, expected_revision=4, pending=pending
            ),
            connection=conn,
        )
        assert result.state == "verification_pending" and conn.in_transaction
        conn.rollback()
    finally:
        conn.close()
    assert admit(store).record.state == "running"


def test_lost_pending_commit_response_replays_exact_receipt(state, monkeypatch):
    from sonder_runtime.application.ports.app_control import OutcomeUnknown
    from sonder_runtime.adapters.persistence.app_control import SQLiteAppControlStore

    store, scope, pending = running(state)
    original = store._commit

    def lost(conn):
        original(conn)
        raise OSError("lost commit response")

    monkeypatch.setattr(store, "_commit", lost)
    with pytest.raises(OutcomeUnknown):
        store.atomic(
            lambda tx: tx.record_work_verification_pending(
                **scope, expected_revision=4, pending=pending
            )
        )
    reopened = SQLiteAppControlStore(store.path, clock=lambda: 100)
    result = reopened.atomic(
        lambda tx: tx.record_work_verification_pending(
            **scope, expected_revision=4, pending=pending
        )
    )
    assert result.revision == 5 and result.verification_pending == pending
    assert not admit(reopened).newly_admitted


def test_pending_expiry_is_not_renewed_by_retry(state):
    from sonder_runtime.application.ports.app_control import CommandConflict

    store, scope, pending = running(state)
    with pytest.raises(CommandConflict):
        store.atomic(
            lambda tx: tx.record_work_verification_pending(
                **scope,
                expected_revision=4,
                pending=replace(
                    pending, approval=replace(pending.approval, expires_at=90)
                )
            )
        )
    result = store.atomic(
        lambda tx: tx.record_work_verification_pending(
            **scope, expected_revision=4, pending=pending
        )
    )
    store.clock = lambda: 601
    assert (
        store.atomic(
            lambda tx: tx.record_work_verification_pending(
                **scope, expected_revision=4, pending=pending
            )
        )
        == result
    )
    assert result.verification_pending.approval.expires_at == 600


def test_concurrent_pending_observations_have_one_exact_winner(state):
    from concurrent.futures import ThreadPoolExecutor
    from sonder_runtime.application.ports.app_control import CommandConflict

    store, scope, pending = running(state)
    choices = (
        pending,
        replace(
            pending,
            approval=replace(pending.approval, call_digest="7" * 64, call_id="7" * 16),
        ),
    )

    def attempt(value):
        try:
            return store.atomic(
                lambda tx: tx.record_work_verification_pending(
                    **scope, expected_revision=4, pending=value
                )
            )
        except CommandConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, choices))
    assert len([r for r in results if r is not None]) == 1
    assert admit(store).record.verification_pending in choices


def test_pending_blocks_new_work_until_matching_certified_completion(state):
    from sonder_runtime.application.ports.app_managed_work import WorkCompletionEvidence
    from sonder_runtime.application.ports.lane_continuation import (
        TerminalProjectionReceipt,
    )
    from sonder_runtime.application.ports.app_control import CommandConflict

    store, scope, pending = running(state)
    work = admit(store).record.prepared
    store.atomic(
        lambda tx: tx.record_work_verification_pending(
            **scope, expected_revision=4, pending=pending
        )
    )
    next_work = replace(
        work, work_id="work2", command=replace(work.command, command_id="prepare2")
    )
    with pytest.raises(CommandConflict):
        store.atomic(lambda tx: tx.prepare_work(next_work))
    receipt = TerminalProjectionReceipt(
        "published1", "8" * 64, pending.identity.projection_digest, "9" * 64, 2
    )
    completion = WorkCompletionEvidence(
        "certified_after_return", pending.identity, receipt
    )
    with pytest.raises(ValueError):
        store.atomic(
            lambda tx: tx.record_work_terminal(
                **scope,
                expected_revision=5,
                terminal=replace(pending.original_terminal, output_digest="0" * 64),
                completion=completion
            )
        )
    terminal = store.atomic(
        lambda tx: tx.record_work_terminal(
            **scope,
            expected_revision=5,
            terminal=pending.original_terminal,
            completion=completion
        )
    )
    assert terminal.revision == 6 and terminal.verification_pending == pending
    assert store.atomic(lambda tx: tx.prepare_work(next_work)).state == "prepared"


@pytest.mark.parametrize(
    "damage", ["foreign_parent", "generation_bool", "surface_control", "wrong_tool"]
)
def test_pending_port_rejects_invalid_evidence(state, damage):
    store, scope, pending = running(state)
    with pytest.raises(ValueError):
        if damage == "foreign_parent":
            replace(
                pending, identity=replace(pending.identity, parent_session_id="other")
            )
        elif damage == "generation_bool":
            replace(pending, identity=replace(pending.identity, generation=True))
        elif damage == "surface_control":
            replace(
                pending, approval=replace(pending.approval, surface="app\x00control")
            )
        else:
            replace(pending, approval=replace(pending.approval, tool="unrelated_tool"))
