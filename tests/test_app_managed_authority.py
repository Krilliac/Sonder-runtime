"""Real app selection and ordered account/fleet authority regression cases."""

from dataclasses import replace
from pathlib import Path
import hashlib
import threading
import time
import pytest
from tests.test_app_control_http import control, enrolled, invoke
from sonder_runtime.application.context import OperationContext
from sonder_runtime.bootstrap.app_managed_authority import AppManagedAuthority
from sonder_runtime.adapters.persistence.agent_lanes import SQLiteAgentLaneStore
from sonder_runtime.adapters.persistence.session_repository import (
    SQLiteSessionRepository,
)
from sonder_runtime.application.agents.interactive_lanes import AgentLaneService
from sonder_runtime.application.ports.model_gateway import ModelResponse


class Cancel:
    def __init__(self):
        self.event = threading.Event()

    @property
    def cancelled(self):
        return self.event.is_set()

    def wait(self, timeout=None):
        return self.event.wait(timeout)


@pytest.fixture
def managed(control):
    binding, token, state, open_db, catalog, entry = control
    credential = enrolled(control)
    bid = invoke(
        binding, token, "create_binding", dict(command_id="create1"), credential
    )[1]["receipt"]["entity_id"]
    invoke(
        binding,
        token,
        "select_binding",
        dict(
            command_id="select1",
            binding_id=bid,
            expected_binding_revision=1,
            expected_epoch=0,
        ),
        credential,
    )
    context = OperationContext(
        "app-work",
        "account:" + hashlib.sha256(b"alice").hexdigest(),
        "admin",
        "http",
        time.monotonic() + 300,
        Cancel(),
        tuple(Path(p) for p in entry["roots"]),
    )
    sessions = SQLiteSessionRepository(
        Path(binding.store.path).with_name("sessions.db")
    )
    store = SQLiteAgentLaneStore(binding.store.path, sessions)

    class Model:
        calls = 0

        def generate(self, request, context):
            self.calls += 1
            return ModelResponse("Completed", "scripted", "code", tokens_out=1)

    model = Model()
    lanes = AgentLaneService(
        store, sessions, model, auto_start=False, allowed_tools=("read_file",)
    )
    authority = AppManagedAuthority(binding, lanes)
    selection = binding.issue_selection(
        account_token=token, control_token=credential, context=context
    )
    yield authority, selection, lanes, model, context, binding, token, credential
    lanes.close()


def test_private_selection_is_exact_and_cannot_be_replaced(managed):
    authority, selection, lanes, model, context, *_ = managed
    assert selection.context.principal_id == context.principal_id
    assert selection.context.source == "http"
    assert selection.context.cancellation is context.cancellation
    assert selection.context.deadline_monotonic <= context.deadline_monotonic
    with authority.admit(selection, selection.context) as admission:
        with lanes.store.transaction() as tx:
            grant = authority.authorize_host(
                admission,
                selection.context,
                selection.host_conversation_id,
                connection=tx.conn,
            )
            assert grant.principal_id == context.principal_id
    with pytest.raises(PermissionError):
        with authority.admit(replace(selection), selection.context):
            pass
    with pytest.raises(PermissionError):
        with authority.admit(
            selection, replace(selection.context, cancellation=Cancel())
        ):
            pass


def test_managed_host_registration_uses_private_selection_and_closes_registry(managed):
    authority, selection, lanes, model, context, *_ = managed
    host = authority.continuation_service(selection)
    parent = host.open_parent(selection.context)
    bound = host.register_parent(
        parent["parent_session_id"],
        parent["parent_token"],
        selection.host_conversation_id,
        context=selection.context,
        command_id="registered",
    )
    bound.require_current()
    assert authority._parents[parent["parent_session_id"]].bound is bound
    bound.close()
    assert parent["parent_session_id"] not in authority._parents
    with pytest.raises(PermissionError):
        bound.require_current()


def test_real_registered_app_lane_runs_only_admitted_context(managed):
    authority, selection, lanes, model, context, *_ = managed
    host = authority.continuation_service(selection)
    parent = host.open_parent(selection.context)
    bound = host.register_parent(
        parent["parent_session_id"],
        parent["parent_token"],
        selection.host_conversation_id,
        context=selection.context,
        command_id="registered",
    )
    try:
        with bound._scope() as current:
            receipt = lanes.spawn(
                command_id="spawn1",
                parent_session_id=parent["parent_session_id"],
                task="Inspect this disposable project.",
                workspace_root=str(current.workspace_roots[0]),
                context=current,
            )
        lane_id = receipt["lane"]["id"]
        with pytest.raises(PermissionError):
            lanes.run_pending(lane_id, replace(current))
        assert model.calls == 0
        lanes.run_pending(lane_id, current)
        assert model.calls == 1
        assert lanes.store.read_lane(lane_id)["status"] == "completed"
    finally:
        bound.close()


@pytest.mark.parametrize("operation", ["clear", "role", "key", "catalog", "cancel"])
def test_live_revocation_fences_bound_and_never_dispatches(
    managed, operation, monkeypatch
):
    authority, selection, lanes, model, context, binding, token, credential = managed
    host = authority.continuation_service(selection)
    parent = host.open_parent(selection.context)
    bound = host.register_parent(
        parent["parent_session_id"],
        parent["parent_token"],
        selection.host_conversation_id,
        context=selection.context,
        command_id="registered",
    )
    try:
        if operation == "clear":
            invoke(
                binding,
                token,
                "clear_selection",
                dict(command_id="clear1", expected_epoch=1),
                credential,
            )
        elif operation == "role":
            from sonder_runtime.adapters.security.account_auth import account_auth

            with binding._open() as conn:
                account_auth.set_account(conn, "alice", role="user")
        elif operation == "key":
            monkeypatch.setenv(
                "SONDER_AUTH_SECRET", "changed-signing-key-unique-01234567890123456789"
            )
        elif operation == "catalog":
            monkeypatch.setattr(
                binding,
                "_grant",
                lambda *a: (_ for _ in ()).throw(PermissionError("changed")),
            )
        else:
            context.cancellation.event.set()
        with pytest.raises(PermissionError):
            bound.require_current()
        assert model.calls == 0
    finally:
        bound.close()


def test_admission_binds_thread_connection_context_and_lifetime(managed):
    authority, selection, lanes, *_ = managed
    current = selection.context
    with authority.admit(selection, current) as admission:
        with lanes.store.transaction() as tx:
            assert authority.authorize_host(
                admission, current, selection.host_conversation_id, connection=tx.conn
            )
            with pytest.raises(PermissionError):
                authority.authorize_host(
                    admission,
                    replace(current),
                    selection.host_conversation_id,
                    connection=tx.conn,
                )
            errors = []

            def wrong_thread():
                try:
                    authority.authorize_host(
                        admission,
                        current,
                        selection.host_conversation_id,
                        connection=tx.conn,
                    )
                except PermissionError:
                    errors.append("denied")

            thread = threading.Thread(target=wrong_thread)
            thread.start()
            thread.join(2)
            assert not thread.is_alive() and errors == ["denied"]
        with lanes.store.transaction() as other:
            with pytest.raises(PermissionError):
                authority.authorize_host(
                    admission,
                    current,
                    selection.host_conversation_id,
                    connection=other.conn,
                )
    with lanes.store.transaction() as tx:
        with pytest.raises(PermissionError):
            authority.authorize_host(
                admission, current, selection.host_conversation_id, connection=tx.conn
            )


def test_exact_mint_is_revoked_after_registration_failure(managed, monkeypatch):
    authority, selection, lanes, *_ = managed
    host = authority.continuation_service(selection)
    parent = host.open_parent(selection.context)
    monkeypatch.setattr(
        authority,
        "register_parent",
        lambda *a: (_ for _ in ()).throw(PermissionError("failure")),
    )
    with pytest.raises(PermissionError):
        host.register_parent(
            parent["parent_session_id"],
            parent["parent_token"],
            selection.host_conversation_id,
            context=selection.context,
            command_id="register1",
        )
    host.discard_parent(parent, selection.context)
    with lanes.store.transaction() as tx:
        row = tx.conn.execute(
            "SELECT revoked FROM agent_lane_parent_grants WHERE session_id=?",
            (parent["parent_session_id"],),
        ).fetchone()
        assert row[0] == 1
    assert not authority._parents


def test_account_guard_precedes_fleet_writer_without_abba(managed, monkeypatch):
    from sonder_runtime.adapters.security.account_admission import account_admission

    authority, selection, lanes, model, context, binding, *_ = managed
    host = authority.continuation_service(selection)
    parent = host.open_parent(selection.context)
    bound = host.register_parent(
        parent["parent_session_id"],
        parent["parent_token"],
        selection.host_conversation_id,
        context=selection.context,
        command_id="registered",
    )
    opened = threading.Event()
    failures = []
    original_open = binding._open

    def observed_open():
        result = original_open()
        if threading.current_thread() is not threading.main_thread():
            opened.set()
        return result

    monkeypatch.setattr(binding, "_open", observed_open)

    def read_current():
        try:
            bound.require_current()
        except BaseException as exc:
            failures.append(type(exc).__name__)

    conn = original_open()
    thread = threading.Thread(target=read_current)
    try:
        with account_admission(conn):
            thread.start()
            assert opened.wait(3)
            # The contender is blocked on account admission. This real writer
            # remains available, so it cannot be holding fleet while awaiting us.
            with lanes.store.transaction() as tx:
                assert tx.conn.execute("SELECT 1").fetchone()[0] == 1
        thread.join(5)
        assert not thread.is_alive() and failures == []
    finally:
        conn.close()
        thread.join(5)
        bound.close()


def test_queued_app_work_is_not_started_after_selection_clear(managed):
    authority, selection, lanes, model, context, binding, token, credential = managed
    host = authority.continuation_service(selection)
    parent = host.open_parent(selection.context)
    bound = host.register_parent(
        parent["parent_session_id"],
        parent["parent_token"],
        selection.host_conversation_id,
        context=selection.context,
        command_id="registered",
    )
    try:
        with bound._scope() as current:
            receipt = lanes.spawn(
                command_id="spawn1",
                parent_session_id=parent["parent_session_id"],
                task="Inspect the project",
                workspace_root=str(current.workspace_roots[0]),
                context=current,
            )
        invoke(
            binding,
            token,
            "clear_selection",
            dict(command_id="clear1", expected_epoch=1),
            credential,
        )
        with pytest.raises(PermissionError):
            lanes.run_pending(receipt["lane"]["id"], current)
        assert model.calls == 0
        assert lanes.store.read_lane(receipt["lane"]["id"])["status"] == "queued"
    finally:
        bound.close()


def test_host_authorization_borrows_exact_active_fleet_connection(managed, monkeypatch):
    authority, selection, lanes, *_ = managed
    host = authority.continuation_service(selection)
    observed = []
    original_atomic = authority.binding.store.atomic

    def spy(callback, *, connection=None):
        if connection is not None:
            observed.append(connection)
            assert connection.in_transaction
        return original_atomic(callback, connection=connection)

    monkeypatch.setattr(authority.binding.store, "atomic", spy)
    with host._transaction(selection.context) as tx:
        host._grant(selection.context, selection.host_conversation_id, tx=tx)
        assert observed == [tx.conn]
        tx.conn.execute("CREATE TABLE authority_rollback_probe(value INTEGER)")
        tx.conn.execute("INSERT INTO authority_rollback_probe VALUES (7)")
    assert observed


def test_explicit_fresh_reattachment_retains_original_ceiling(managed):
    from tests.test_lane_continuation import granted

    authority, selection, lanes, model, context, binding, token, credential = managed
    host = authority.continuation_service(selection)
    parent = host.open_parent(selection.context)
    bound = host.register_parent(
        parent["parent_session_id"],
        parent["parent_token"],
        selection.host_conversation_id,
        context=selection.context,
        command_id="registered",
    )
    original = bound.authority_ceiling()
    identity = bound.continuation_id
    bound.close()
    fresh_context = replace(context, correlation_id="explicit-fresh-host")
    fresh_selection = binding.issue_selection(
        account_token=token, control_token=credential, context=fresh_context
    )
    fresh = authority.continuation_service(fresh_selection)
    selected = fresh.select(identity, fresh_selection.context)
    prepared = fresh.prepare_reattachment(
        selected, fresh_selection.context, command_id="attach1"
    )
    resumed = fresh.execute_reattachment(
        prepared, fresh_selection.context, approve=granted
    )
    try:
        resumed.require_current()
        assert resumed.continuation_id == identity
        assert resumed._epoch == 2
        assert resumed.authority_ceiling().workspace_roots == original.workspace_roots
        assert (
            resumed.authority_ceiling().deadline_monotonic
            <= original.deadline_monotonic + 0.01
        )
        assert model.calls == 0
    finally:
        resumed.close()


def test_removed_parent_registration_fences_existing_bound_handle(managed):
    authority, selection, lanes, *_ = managed
    host = authority.continuation_service(selection)
    parent = host.open_parent(selection.context)
    bound = host.register_parent(
        parent["parent_session_id"],
        parent["parent_token"],
        selection.host_conversation_id,
        context=selection.context,
        command_id="registered",
    )
    try:
        authority.release_parent(bound)
        with pytest.raises(PermissionError):
            bound.require_current()
    finally:
        bound.close()


def test_provider_runs_outside_guards_and_observes_selection_revocation(managed):
    authority, selection, lanes, model, context, binding, token, credential = managed
    host = authority.continuation_service(selection)
    parent = host.open_parent(selection.context)
    bound = host.register_parent(
        parent["parent_session_id"],
        parent["parent_token"],
        selection.host_conversation_id,
        context=selection.context,
        command_id="registered",
    )
    worker_contexts = []
    errors = []

    def generate(request, worker_context):
        model.calls += 1
        worker_contexts.append(worker_context)

        def revoke():
            try:
                invoke(
                    binding,
                    token,
                    "clear_selection",
                    dict(command_id="clear1", expected_epoch=1),
                    credential,
                )
            except BaseException as exc:
                errors.append(type(exc).__name__)

        thread = threading.Thread(target=revoke)
        thread.start()
        thread.join(5)
        assert not thread.is_alive() and not errors
        assert worker_context.cancellation.cancelled
        return ModelResponse(
            "Observed before revocation", "scripted", "code", tokens_out=1
        )

    model.generate = generate
    try:
        with bound._scope() as current:
            receipt = lanes.spawn(
                command_id="spawn1",
                parent_session_id=parent["parent_session_id"],
                task="Inspect the project",
                workspace_root=str(current.workspace_roots[0]),
                context=current,
            )
        lanes.run_pending(receipt["lane"]["id"], current)
        lane = lanes.store.read_lane(receipt["lane"]["id"])
        assert model.calls == 1 and lane["status"] == "failed"
        assert lane["pending_response"]["text"] == "Observed before revocation"
        assert not lane["pending_effect"]
        assert not authority._workers
    finally:
        bound.close()


def test_private_state_inside_broader_request_root_refuses_selection(managed):
    authority, selection, lanes, model, context, binding, token, credential = managed
    broad = Path(binding.store.path).parent.parent
    assert any(root.is_relative_to(broad) for root in context.workspace_roots)
    with pytest.raises(PermissionError):
        binding.issue_selection(
            account_token=token,
            control_token=credential,
            context=replace(context, workspace_roots=(broad,)),
        )
    assert model.calls == 0


def test_expired_admission_cannot_be_reactivated_by_mutating_token(managed):
    authority, selection, lanes, *_ = managed
    with authority.admit(selection, selection.context) as admission:
        pass
    admission.active = True
    with lanes.store.transaction() as tx:
        with pytest.raises(PermissionError):
            authority.authorize_host(
                admission,
                selection.context,
                selection.host_conversation_id,
                connection=tx.conn,
            )


def test_key_rotation_after_reference_read_refuses_before_fleet_mutation(
    managed, monkeypatch
):
    from sonder_runtime.adapters.security.account_auth import account_auth

    authority, selection, lanes, *_ = managed
    host = authority.continuation_service(selection)
    from contextlib import contextmanager

    original_transaction = lanes.store.transaction
    transactions = []

    @contextmanager
    def observed_transaction():
        transactions.append("opened")
        with original_transaction() as tx:
            yield tx

    monkeypatch.setattr(lanes.store, "transaction", observed_transaction)
    original_read = account_auth.read_session_reference
    reads = []

    def read_then_rotate(conn, reference):
        identity = original_read(conn, reference)
        assert identity == selection.account
        reads.append(identity)
        monkeypatch.setenv(
            "SONDER_AUTH_SECRET",
            "rotated-after-real-read-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        )
        return identity

    monkeypatch.setattr(account_auth, "read_session_reference", read_then_rotate)
    with lanes.store.connect() as conn:
        before = conn.execute(
            "SELECT COUNT(*) FROM agent_lane_parent_grants"
        ).fetchone()[0]
    with pytest.raises(PermissionError):
        host.open_parent(selection.context)
    assert len(reads) == 1
    assert transactions == []
    assert not authority._admissions
    with lanes.store.connect() as conn:
        after = conn.execute(
            "SELECT COUNT(*) FROM agent_lane_parent_grants"
        ).fetchone()[0]
    assert after == before
    assert not host._minted_parents
