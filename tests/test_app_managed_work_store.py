from dataclasses import replace
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import pytest

from tests.test_app_control_store import state, enroll, create, select, OWNER, HASH
from sonder_runtime.adapters.persistence.app_control import SQLiteAppControlStore
from sonder_runtime.application.ports.app_control import (
    CommandKey,
    CommandConflict,
    StoreUnavailable,
)
from sonder_runtime.application.ports.app_managed_work import (
    WorkSpec,
    PreparedWorkbenchRun,
    PreparedAppWork,
)


def preparation(state):
    store, session = state
    enroll(store, session)
    create(store, session)
    select(store, session)
    binding = store.atomic(
        lambda tx: tx.read_binding(principal_id=OWNER, binding_id="binding1")
    )
    selection = store.atomic(
        lambda tx: tx.read_selection(principal_id=OWNER, control_session_id="session1")
    )
    plan = PreparedWorkbenchRun(
        WorkSpec("Fix parser", "code", "model:exact", 12, False),
        binding.grant.roots[0],
        ("model:exact",),
        HASH,
        False,
    )
    work = PreparedAppWork(
        "work1",
        CommandKey(OWNER, "control:session1", "prepare1"),
        binding,
        selection,
        session.account_session_ref,
        plan,
        HASH,
        100,
        700,
    )
    return store, work


def admit(store, **changes):
    args = dict(
        principal_id=OWNER,
        control_session_id="session1",
        work_id="work1",
        expected_revision=1,
        dispatch_id="dispatch1",
        process_incarnation="process1",
    )
    args.update(changes)
    return store.atomic(lambda tx: tx.admit_work(**args))


def test_preparation_reopen_and_admission_never_becomes_dispatchable_again(state):
    store, work = preparation(state)
    first = store.atomic(lambda tx: tx.prepare_work(work))
    assert first.state == "prepared"
    assert store.atomic(lambda tx: tx.prepare_work(work)) == first
    outcome = admit(store)
    assert outcome.newly_admitted
    admitted = outcome.record
    assert admitted.state == "admitted" and admitted.revision == 2
    reopened = SQLiteAppControlStore(store.path, clock=lambda: 100)
    retry = admit(reopened)
    assert retry.record == admitted and not retry.newly_admitted
    with pytest.raises(CommandConflict):
        admit(reopened, dispatch_id="different", process_incarnation="restarted")
    assert reopened.atomic(lambda tx: tx.prepare_work(work)) == admitted


def test_concurrent_dispatch_admission_has_one_owner(state):
    store, work = preparation(state)
    store.atomic(lambda tx: tx.prepare_work(work))

    def attempt(number):
        try:
            return admit(store, dispatch_id="dispatch" + str(number)).record.dispatch_id
        except CommandConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, (1, 2)))
    assert len([value for value in results if value]) == 1


def test_same_dispatch_retry_cannot_claim_a_second_launch(state):
    store, work = preparation(state)
    store.atomic(lambda tx: tx.prepare_work(work))
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: admit(store), range(2)))
    assert sum(value.newly_admitted for value in outcomes) == 1
    assert outcomes[0].record == outcomes[1].record


def test_changed_prepare_arguments_and_second_binding_work_refuse(state):
    store, work = preparation(state)
    store.atomic(lambda tx: tx.prepare_work(work))
    with pytest.raises(CommandConflict):
        store.atomic(lambda tx: tx.prepare_work(replace(work, context_digest="2" * 64)))
    with pytest.raises(CommandConflict):
        store.atomic(
            lambda tx: tx.prepare_work(
                replace(
                    work,
                    work_id="work2",
                    command=replace(work.command, command_id="prepare2"),
                )
            )
        )


def test_cleared_selection_prevents_dispatch(state):
    store, work = preparation(state)
    store.atomic(lambda tx: tx.prepare_work(work))
    store.atomic(
        lambda tx: tx.clear_selection(
            CommandKey(OWNER, "control:session1", "clear1"),
            argument_digest=HASH,
            control_session_id="session1",
            expected_epoch=1,
        )
    )
    with pytest.raises(CommandConflict):
        admit(store)


def test_borrowed_preparation_receipt_and_work_rollback_together(state):
    store, work = preparation(state)
    conn = store._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        store.atomic(lambda tx: tx.prepare_work(work), connection=conn)
        assert conn.in_transaction
        conn.rollback()
    finally:
        conn.close()
    assert (
        store.atomic(
            lambda tx: tx.read_work(
                principal_id=OWNER, control_session_id="session1", work_id="work1"
            )
        )
        is None
    )
    assert store.atomic(lambda tx: tx.prepare_work(work)).state == "prepared"


def test_work_table_triggers_are_refused_before_callback(state):
    store, _ = preparation(state)
    conn = store._connect()
    try:
        conn.execute(
            "CREATE TRIGGER work_observer AFTER INSERT ON app_managed_work BEGIN SELECT 1; END"
        )
    finally:
        conn.close()
    calls = []
    with pytest.raises(StoreUnavailable, match="trigger"):
        store.atomic(lambda tx: calls.append("called"))
    assert not calls


def test_missing_workspace_keeps_admitted_history_readable_but_fences_admission(state):
    store, work = preparation(state)
    store.atomic(lambda tx: tx.prepare_work(work))
    original = admit(store).record
    Path(work.plan.project_root).rmdir()
    retained = store.atomic(
        lambda tx: tx.read_work(
            principal_id=OWNER, control_session_id="session1", work_id="work1"
        )
    )
    assert retained == original
    with pytest.raises((CommandConflict, StoreUnavailable)):
        admit(store)


@pytest.mark.parametrize("already_admitted", [False, True])
def test_missing_preparation_receipt_cannot_admit_or_replay_work(
    state, already_admitted
):
    store, work = preparation(state)
    store.atomic(lambda tx: tx.prepare_work(work))
    if already_admitted:
        admit(store)
    conn = store._connect()
    try:
        conn.execute("DELETE FROM app_control_commands WHERE action='prepare_work'")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(StoreUnavailable, match="receipt"):
        admit(store)
    retained = store.atomic(
        lambda tx: tx.read_work(
            principal_id=OWNER, control_session_id="session1", work_id="work1"
        )
    )
    assert retained.state == ("admitted" if already_admitted else "prepared")


@pytest.mark.parametrize(
    "field,value",
    [("entity_id", "other-work"), ("entity_revision", 2), ("selection_epoch", 2)],
)
@pytest.mark.parametrize("replay", [False, True])
def test_mismatched_preparation_receipt_refuses_admission(state, field, value, replay):
    import json

    store, work = preparation(state)
    store.atomic(lambda tx: tx.prepare_work(work))
    conn = store._connect()
    try:
        receipt = json.loads(
            conn.execute(
                "SELECT receipt FROM app_control_commands WHERE action='prepare_work'"
            ).fetchone()[0]
        )
        receipt[field] = value
        encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
        conn.execute(
            "UPDATE app_control_commands SET receipt=? WHERE action='prepare_work'",
            (encoded,),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(StoreUnavailable, match="receipt"):
        if replay:
            store.atomic(lambda tx: tx.prepare_work(work))
        else:
            admit(store)


def test_prepared_scope_validation_does_not_resolve_live_filesystem(state, monkeypatch):
    _, work = preparation(state)

    def forbidden(*args, **kwargs):
        pytest.fail("historical validation accessed the filesystem")

    monkeypatch.setattr(Path, "resolve", forbidden)
    monkeypatch.setattr(Path, "is_dir", forbidden)
    work.__post_init__()
