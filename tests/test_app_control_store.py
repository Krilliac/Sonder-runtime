import pytest
from sonder_runtime.adapters.persistence.app_control import SQLiteAppControlStore
from sonder_runtime.application.ports.app_control import AppControlLimits


def test_store_has_bounded_limits():
    assert AppControlLimits().command_cap == 4096


from dataclasses import asdict, replace
import json
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from sonder_runtime.adapters.persistence.fleet_store import _ensure_schema
from sonder_runtime.application.ports.app_control import (
    GrantSnapshot,
    ControlSessionRecord,
    BindingRecord,
    CommandKey,
    CommandConflict,
    CapacityExceeded,
    OutcomeUnknown,
    StoreUnavailable,
    NotFound,
)

OWNER = "account:" + "a" * 64
OTHER = "account:" + "b" * 64
REF = "account-session-v1:" + "c" * 64 + "." + "d" * 64
HASH = "1" * 64


@pytest.fixture
def state(tmp_path):
    path = tmp_path / "fleet.db"
    _ensure_schema(str(path))
    root = tmp_path / "workspace"
    root.mkdir()
    store = SQLiteAppControlStore(path, clock=lambda: 100)
    grant = GrantSnapshot(
        "grant1",
        1,
        "project1",
        (str(root),),
        ("read_file",),
        False,
        False,
        1000,
        HASH,
        "2" * 64,
        (1, 2, 3, 4),
    )
    session = ControlSessionRecord(
        "session1", OWNER, "runtime1", REF, grant, "3" * 64, "4" * 64, 950, 90, 900
    )
    return store, session


def enroll(store, session, command="enroll1", **kwargs):
    key = CommandKey(
        session.principal_id, "account:" + session.account_session_ref, command
    )
    return store.atomic(
        lambda tx: tx.commit_enrollment(
            key, argument_digest=HASH, session=session, **kwargs
        )
    )


def binding(session, bid="binding1"):
    return BindingRecord(
        bid,
        "app-session:" + bid,
        session.principal_id,
        session.runtime_id,
        session.grant,
        95,
        800,
    )


def create(store, session, bid="binding1", command="create1"):
    return store.atomic(
        lambda tx: tx.create_binding(
            CommandKey(OWNER, "control:" + session.control_session_id, command),
            argument_digest=HASH,
            control_session_id=session.control_session_id,
            binding=binding(session, bid),
        )
    )


def select(store, session, command="select1", epoch=0):
    return store.atomic(
        lambda tx: tx.select_binding(
            CommandKey(OWNER, "control:" + session.control_session_id, command),
            argument_digest=HASH,
            control_session_id=session.control_session_id,
            binding_id="binding1",
            expected_binding_revision=1,
            expected_epoch=epoch,
        )
    )


def test_complete_reopen_retry_and_clear(state):
    store, session = state
    first = enroll(store, session)
    assert enroll(store, session) == first
    create(store, session)
    selected = select(store, session)
    again = SQLiteAppControlStore(store.path, clock=lambda: 100)
    assert select(again, session) == selected
    values = again.atomic(
        lambda tx: tx.require_selection(
            principal_id=OWNER,
            control_session_id="session1",
            binding_id="binding1",
            binding_revision=1,
            selection_id=selected.entity_id,
            epoch=1,
        )
    )
    assert values[0] == session
    cleared = again.atomic(
        lambda tx: tx.clear_selection(
            CommandKey(OWNER, "control:session1", "clear1"),
            argument_digest=HASH,
            control_session_id="session1",
            expected_epoch=1,
        )
    )
    assert cleared.selection_epoch == 2
    with pytest.raises(CommandConflict):
        again.atomic(
            lambda tx: tx.require_selection(
                principal_id=OWNER,
                control_session_id="session1",
                binding_id="binding1",
                binding_revision=1,
                selection_id=selected.entity_id,
                epoch=1,
            )
        )
    assert (
        again.atomic(
            lambda tx: tx.command(
                CommandKey(OWNER, "account:" + REF, "enroll1"),
                action="enroll",
                argument_digest=HASH,
            )
        ).state
        == "committed"
    )
    assert REF not in json.dumps(asdict(first)) and session.verifier not in repr(
        session
    )


def test_conflicting_command_rolls_back(state):
    store, session = state
    enroll(store, session)
    with pytest.raises(CommandConflict):
        store.atomic(
            lambda tx: tx.command(
                CommandKey(OWNER, "account:" + REF, "enroll1"),
                action="enroll",
                argument_digest="9" * 64,
            )
        )
    with pytest.raises(CommandConflict):
        store.atomic(
            lambda tx: tx.create_binding(
                CommandKey(OWNER, "account:" + REF, "enroll1"),
                argument_digest=HASH,
                control_session_id="session1",
                binding=binding(session),
            )
        )
    assert store.atomic(lambda tx: tx.list_bindings(principal_id=OWNER)).items == ()


def test_borrowed_connection_rollback_and_lifetime(state):
    store, session = state
    conn = sqlite3.connect(store.path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("BEGIN IMMEDIATE")
    captured = []

    def work(tx):
        captured.append(tx)
        return tx.commit_enrollment(
            CommandKey(OWNER, "account:" + REF, "enroll1"),
            argument_digest=HASH,
            session=session,
        )

    store.atomic(work, connection=conn)
    assert conn.in_transaction
    conn.rollback()
    assert (
        store.atomic(
            lambda tx: tx.read_session(
                principal_id=OWNER, control_session_id="session1"
            )
        )
        is None
    )
    with pytest.raises(StoreUnavailable):
        captured[0].read_session(principal_id=OWNER, control_session_id="session1")
    with pytest.raises(StoreUnavailable):
        store.atomic(work, connection=conn)
    conn.close()


def test_wrong_borrowed_database_refused(state, tmp_path):
    store, _ = state
    other = tmp_path / "other.db"
    _ensure_schema(str(other))
    SQLiteAppControlStore(other)
    conn = sqlite3.connect(other)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(StoreUnavailable):
        store.atomic(lambda tx: None, connection=conn)
    assert conn.in_transaction
    conn.rollback()
    conn.close()


def test_lost_commit_response_reconciles_exact_receipt(state, monkeypatch):
    store, session = state

    def fail_after_commit(conn):
        conn.commit()
        raise OSError("private unexpected detail")

    monkeypatch.setattr(store, "_commit", fail_after_commit)
    with pytest.raises(OutcomeUnknown, match="^control commit outcome unknown$"):
        enroll(store, session)
    reopened = SQLiteAppControlStore(store.path, clock=lambda: 100)
    receipt = enroll(reopened, session)
    assert receipt.entity_id == "session1"
    assert (
        reopened.atomic(
            lambda tx: tx.command(
                CommandKey(OWNER, "account:" + REF, "enroll1"),
                action="enroll",
                argument_digest=HASH,
            )
        ).public_receipt
        == receipt
    )
    with sqlite3.connect(store.path) as conn:
        assert (
            conn.execute("SELECT count(*) FROM app_control_sessions").fetchone()[0] == 1
        )


def test_quota_no_eviction_or_partial_mutation(state):
    store, session = state
    store.limits = AppControlLimits(account_session_cap=1, global_session_cap=1)
    enroll(store, session)
    with pytest.raises(CapacityExceeded):
        enroll(
            store,
            replace(session, control_session_id="session2"),
            "enroll2",
            replace_session_id="session1",
        )
    assert (
        store.atomic(
            lambda tx: tx.read_session(
                principal_id=OWNER, control_session_id="session1"
            )
        ).revoked_at
        is None
    )


def test_concurrent_selection_compare_and_swap(state):
    store, session = state
    enroll(store, session)
    create(store, session)

    def attempt(n):
        try:
            return select(store, session, command="select" + str(n))
        except CommandConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [1, 2]))
    assert sum(item is not None for item in results) == 1


def test_revoke_fences_all_selected_control_sessions(state):
    store, session = state
    enroll(store, session)
    create(store, session)
    select(store, session)
    second = replace(session, control_session_id="session2")
    enroll(store, second, "enroll2")
    select(store, second)
    receipt = store.atomic(
        lambda tx: tx.revoke_binding(
            CommandKey(OWNER, "control:session1", "revoke1"),
            argument_digest=HASH,
            control_session_id="session1",
            binding_id="binding1",
            expected_revision=1,
        )
    )
    assert receipt.entity_revision == 2
    for sid in ("session1", "session2"):
        slot = store.atomic(
            lambda tx: tx.read_selection(principal_id=OWNER, control_session_id=sid)
        )
        assert slot.epoch == 2 and slot.binding_id is None
    assert (
        store.atomic(
            lambda tx: tx.read_binding(principal_id=OTHER, binding_id="binding1")
        )
        is None
    )


def test_persistent_grant_rollback_equivocation_and_file_change(state):
    store, session = state
    enroll(store, session)
    for grant in (
        replace(session.grant, tools=("write_file",)),
        replace(session.grant, catalog_file_identity=(1, 2, 3, 5)),
    ):
        with pytest.raises(CommandConflict):
            enroll(
                store,
                replace(session, control_session_id="other", grant=grant),
                "other",
            )
    newer = replace(
        session, control_session_id="session2", grant=replace(session.grant, revision=2)
    )
    enroll(store, newer, "enroll2")
    reopened = SQLiteAppControlStore(store.path, clock=lambda: 100)
    with pytest.raises(CommandConflict):
        enroll(reopened, replace(session, control_session_id="session3"), "enroll3")
    with pytest.raises(CommandConflict):
        create(reopened, session)


def test_bounded_pages_and_command_quota(state):
    store, session = state
    enroll(store, session)
    create(store, session)
    create(store, session, "binding2", "create2")
    page = store.atomic(lambda tx: tx.list_bindings(principal_id=OWNER, limit=1))
    assert len(page.items) == 1 and page.next_position
    last = store.atomic(
        lambda tx: tx.list_bindings(
            principal_id=OWNER, after_position=page.next_position, limit=1
        )
    )
    assert last.next_position is None and last.items[0].binding_id == "binding2"
    with pytest.raises(CapacityExceeded):
        store.atomic(lambda tx: tx.list_bindings(principal_id=OWNER, max_bytes=1))
    store.limits = replace(store.limits, command_cap=3)
    with pytest.raises(CapacityExceeded):
        select(store, session)
    assert (
        store.atomic(
            lambda tx: tx.read_selection(
                principal_id=OWNER, control_session_id="session1"
            )
        )
        is None
    )


def test_records_refuse_raw_tokens_mutable_grants_and_noncanonical_scope(state):
    _, session = state
    with pytest.raises(ValueError):
        replace(session, account_session_ref="raw-account-token")
    with pytest.raises(ValueError):
        replace(session, salt="raw-control-token")
    with pytest.raises(ValueError):
        replace(session.grant, tools=["read_file"])
    with pytest.raises(ValueError):
        replace(session.grant, allow_cloud=1)
    with pytest.raises(ValueError):
        CommandKey("owner", "control:session1", "command1")


def test_binding_survives_control_expiry_without_renewal(state):
    store, original = state
    session = replace(original, expires_at=120)
    enroll(store, session)
    create(store, session)
    store.clock = lambda: 130
    renewed = replace(
        session, control_session_id="fresh-session", issued_at=130, expires_at=250
    )
    enroll(store, renewed, "fresh-enrollment")
    chosen = select(store, renewed)
    record = store.atomic(
        lambda tx: tx.read_binding(principal_id=OWNER, binding_id="binding1")
    )
    assert record.expires_at == 800 and chosen.entity_revision == 1
    with pytest.raises(CommandConflict):
        create(
            store, replace(renewed, control_session_id="session1"), "newbinding", "bad"
        )
    with pytest.raises(CommandConflict):
        store.atomic(
            lambda tx: tx.create_binding(
                CommandKey(OWNER, "control:fresh-session", "extend"),
                argument_digest=HASH,
                control_session_id="fresh-session",
                binding=replace(record, expires_at=900),
            )
        )
    assert (
        store.atomic(
            lambda tx: tx.read_binding(principal_id=OWNER, binding_id="binding1")
        ).expires_at
        == 800
    )


def test_ttl_and_account_ceiling_are_not_renewed(state):
    store, session = state
    with pytest.raises(ValueError):
        replace(session, account_expires_at=200)
    store.limits = replace(store.limits, session_ttl_seconds=30)
    with pytest.raises(CommandConflict):
        enroll(store, session)
    assert (
        store.atomic(
            lambda tx: tx.read_session(
                principal_id=OWNER, control_session_id="session1"
            )
        )
        is None
    )
    short = replace(session, expires_at=115)
    enroll(store, short)
    store.limits = replace(store.limits, binding_ttl_seconds=30)
    with pytest.raises(CommandConflict):
        create(store, short)
    assert store.atomic(lambda tx: tx.list_bindings(principal_id=OWNER)).items == ()


def test_owned_callback_failure_rolls_back_and_borrowed_does_not_commit(state):
    store, session = state
    key = CommandKey(OWNER, "account:" + REF, "enroll1")

    def failed(tx):
        tx.commit_enrollment(key, argument_digest=HASH, session=session)
        raise CommandConflict("later operation refused")

    with pytest.raises(CommandConflict):
        store.atomic(failed)
    assert (
        store.atomic(
            lambda tx: tx.read_session(
                principal_id=OWNER, control_session_id="session1"
            )
        )
        is None
    )
    conn = sqlite3.connect(store.path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(CommandConflict):
        store.atomic(failed, connection=conn)
    assert conn.in_transaction
    assert conn.execute("SELECT count(*) FROM app_control_sessions").fetchone()[0] == 1
    conn.rollback()
    conn.close()
    assert (
        store.atomic(
            lambda tx: tx.read_session(
                principal_id=OWNER, control_session_id="session1"
            )
        )
        is None
    )


def test_schema_init_rejects_active_transaction(state):
    from sonder_runtime.adapters.persistence.app_control import initialize_schema

    store, _ = state
    conn = sqlite3.connect(store.path)
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(StoreUnavailable):
        initialize_schema(conn)
    assert conn.in_transaction
    conn.rollback()
    conn.close()


def test_record_scope_tamper_fails_closed(state):
    store, session = state
    enroll(store, session)
    with sqlite3.connect(store.path) as conn:
        raw = conn.execute("SELECT record FROM app_control_sessions").fetchone()[0]
        value = json.loads(raw)
        value["principal_id"] = OTHER
        conn.execute(
            "UPDATE app_control_sessions SET record=?",
            (json.dumps(value, sort_keys=True, separators=(",", ":")),),
        )
    with pytest.raises(StoreUnavailable):
        store.atomic(
            lambda tx: tx.read_session(
                principal_id=OWNER, control_session_id="session1"
            )
        )


def test_enrollment_replacement_fences_selection_and_replays_once(state):
    store, session = state
    enroll(store, session)
    create(store, session)
    select(store, session)
    replacement = replace(session, control_session_id="replacement")
    receipt = enroll(store, replacement, "replace", replace_session_id="session1")
    assert (
        enroll(store, replacement, "replace", replace_session_id="session1") == receipt
    )
    old, slot = store.atomic(
        lambda tx: (
            tx.read_session(principal_id=OWNER, control_session_id="session1"),
            tx.read_selection(principal_id=OWNER, control_session_id="session1"),
        )
    )
    assert old.revoked_at == 100 and slot.epoch == 2 and slot.binding_id is None


def test_binding_limit_refusal_does_not_leave_receipt(state):
    store, session = state
    enroll(store, session)
    store.limits = replace(store.limits, account_binding_cap=1)
    create(store, session)
    with pytest.raises(CapacityExceeded):
        create(store, session, "binding2", "create2")
    assert (
        store.atomic(
            lambda tx: tx.command(
                CommandKey(OWNER, "control:session1", "create2"),
                action="create_binding",
                argument_digest=HASH,
            )
        )
        is None
    )


def test_borrowed_connection_cannot_shadow_control_tables(state):
    store, session = state
    enroll(store, session)
    conn = store._connect()
    try:
        conn.execute(
            "CREATE TEMP TABLE app_control_sessions AS SELECT * FROM main.app_control_sessions WHERE 0"
        )
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(StoreUnavailable):
            store.atomic(
                lambda tx: tx.read_session(
                    principal_id=OWNER, control_session_id=session.control_session_id
                ),
                connection=conn,
            )
        assert conn.in_transaction
    finally:
        conn.rollback()
        conn.close()
