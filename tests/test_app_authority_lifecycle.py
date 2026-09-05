"""Ordered app work transactions and exact request/work selection ownership."""

from dataclasses import replace
from sonder_runtime.application.ports.app_control import StoreUnavailable
import pytest
from tests.test_app_managed_authority import managed, control


def test_request_selection_release_is_exact_and_bounded(managed):
    authority, selection, lanes, model, context, binding, token, credential = managed
    with pytest.raises(PermissionError):
        authority.release_selection(replace(selection))
    authority.release_selection(selection)
    for _ in range(129):
        selected = authority.issue_selection(
            account_token=token, control_token=credential, context=context
        )
        authority.release_selection(selected)
    assert not authority._selections
    with pytest.raises(PermissionError):
        with authority.admit(selection, selection.context):
            pass


def test_retained_work_and_active_admission_block_request_release(managed):
    authority, selection, *_ = managed
    lease = authority.retain_selection(selection)
    with pytest.raises(PermissionError):
        authority.release_selection(selection)
    authority.release_retained(lease)
    with pytest.raises(PermissionError):
        authority.release_retained(lease)
    with authority.admit(selection, selection.context):
        with pytest.raises(PermissionError):
            authority.release_selection(selection)
    authority.release_selection(selection)


def test_work_atomic_borrows_and_rolls_back_on_failure(managed):
    authority, selection, lanes, *_ = managed
    captured = []

    def callback(tx):
        captured.append(tx)
        tx._conn.execute("CREATE TABLE lifecycle_probe(value INTEGER)")
        tx._conn.execute("INSERT INTO lifecycle_probe VALUES(1)")
        raise RuntimeError("callback failed")

    with pytest.raises(StoreUnavailable):
        authority.work_atomic(selection, selection.context, callback)
    conn = lanes.store.connect()
    try:
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE name='lifecycle_probe'"
            ).fetchone()
            is None
        )
    finally:
        conn.close()
    with pytest.raises(Exception):
        captured[0]._check()
    assert (
        authority.work_atomic(
            selection,
            selection.context,
            lambda tx: tx.read_selection(
                principal_id=selection.control.principal_id,
                control_session_id=selection.control.control_session_id,
            ),
        )
        == selection.slot
    )


def test_work_callback_cannot_enter_continuation_admission(managed):
    authority, selection, *_ = managed
    host = authority.continuation_service(selection)

    def callback(tx):
        with pytest.raises(PermissionError):
            host.open_parent(selection.context)
        with pytest.raises(PermissionError):
            authority.work_atomic(selection, selection.context, lambda tx: None)
        return "safe"

    assert authority.work_atomic(selection, selection.context, callback) == "safe"


def test_parent_retains_selection_until_exact_close(managed):
    authority, selection, *_ = managed
    host = authority.continuation_service(selection)
    parent = host.open_parent(selection.context)
    bound = host.register_parent(
        parent["parent_session_id"],
        parent["parent_token"],
        selection.host_conversation_id,
        context=selection.context,
        command_id="parent1",
    )
    try:
        with pytest.raises(PermissionError):
            authority.release_selection(selection)

        def callback(tx):
            with pytest.raises(PermissionError):
                bound.require_current()
            with pytest.raises(PermissionError):
                authority.release_parent(bound)

        authority.work_atomic(selection, selection.context, callback)
    finally:
        bound.close()
    authority.release_selection(selection)


def test_release_during_admission_setup_is_refused(managed, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    authority, selection, lanes, model, context, binding, *_ = managed
    entered, proceed = threading.Event(), threading.Event()
    original = binding._open

    def paused():
        entered.set()
        assert proceed.wait(10)
        return original()

    monkeypatch.setattr(binding, "_open", paused)

    def run():
        return authority.work_atomic(
            selection, selection.context, lambda tx: "committed"
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run)
        assert entered.wait(10)
        try:
            with pytest.raises(PermissionError):
                authority.release_selection(selection)
        finally:
            proceed.set()
        assert future.result(timeout=20) == "committed"
    authority.release_selection(selection)
    assert not authority._selection_uses


def test_rotation_after_callback_rolls_back_and_cleanup_remains_available(
    managed, monkeypatch
):
    authority, selection, lanes, *_ = managed
    lease = authority.retain_selection(selection)

    def callback(tx):
        tx._conn.execute("CREATE TABLE rotation_probe(value INTEGER)")
        monkeypatch.setenv(
            "SONDER_AUTH_SECRET",
            "rotated-at-commit-ABCDEFGHIJKLMNOPQRSTUVWXYZ-0123456789",
        )

    with pytest.raises(PermissionError):
        authority.work_atomic(selection, selection.context, callback)
    conn = lanes.store.connect()
    try:
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE name='rotation_probe'"
            ).fetchone()
            is None
        )
    finally:
        conn.close()
    with pytest.raises(PermissionError):
        authority.work_atomic(selection, selection.context, lambda tx: None)
    with pytest.raises(PermissionError):
        authority.release_retained(object())
    with pytest.raises(PermissionError):
        authority.release_selection(selection)
    authority.release_retained(lease)
    authority.release_selection(selection)


def test_expired_selection_can_only_be_cleaned_up(managed, monkeypatch):
    import time

    authority, selection, *_ = managed
    lease = authority.retain_selection(selection)
    monkeypatch.setattr(
        time, "monotonic", lambda: selection.context.deadline_monotonic + 1
    )
    with pytest.raises(PermissionError):
        authority.retain_selection(selection)
    with pytest.raises(PermissionError):
        authority.work_atomic(selection, selection.context, lambda tx: None)
    authority.release_retained(lease)
    authority.release_selection(selection)


def test_work_order_exact_connection_and_nested_admission_refusal(managed, monkeypatch):
    from contextlib import contextmanager
    import sonder_runtime.bootstrap.app_managed_authority as module

    authority, selection, lanes, *_ = managed
    account_held, fleet_held = [], []
    original_account, original_fleet = module.account_admission, lanes.store.transaction

    @contextmanager
    def account(conn):
        assert not fleet_held
        with original_account(conn):
            account_held.append(conn)
            try:
                yield
            finally:
                account_held.pop()

    @contextmanager
    def fleet():
        assert account_held and not fleet_held
        with original_fleet() as tx:
            fleet_held.append(tx.conn)
            try:
                yield tx
            finally:
                fleet_held.pop()

    monkeypatch.setattr(module, "account_admission", account)
    monkeypatch.setattr(lanes.store, "transaction", fleet)

    def callback(tx):
        assert account_held and tx._conn is fleet_held[0]
        return "exact"

    assert authority.work_atomic(selection, selection.context, callback) == "exact"
    assert not account_held and not fleet_held
    with authority.admit(selection, selection.context):
        with pytest.raises(PermissionError):
            authority.work_atomic(selection, selection.context, callback)
    assert not authority._admission_threads
