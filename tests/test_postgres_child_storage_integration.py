"""Opt-in tests against a disposable PostgreSQL pair; never reset its schema.

The workspace conformance harness supplies an ACL-protected binding and owns
both database processes. These tests do not start services or accept a DSN.
"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
import os
import time
import uuid

import pytest

from sonder_runtime.application.ports.continuation_mutations import prepare_call
from sonder_runtime.application.ports.continuation_records import (
    DurableChildSession,
    ChildSessionLineage,
)
from sonder_runtime.application.ports.subagents import SubagentRequest, SubagentBudget
from sonder_runtime.application.subagents.continuable import ContinuableCheckpoint
from sonder_runtime.platform.child_storage_config import ChildStorageConfig

# Supplied only by the in-process disposable harness; never read from a DSN,
# request payload, or production configuration.
PAIR_CONTROL = None


@pytest.fixture
def storage_config():
    path = os.environ.get("SONDER_TEST_CHILD_PG_BINDING")
    if not path or os.environ.get("SONDER_TEST_DISPOSABLE_PG") != "1":
        pytest.skip("requires explicit disposable PostgreSQL conformance binding")
    return ChildStorageConfig(
        backend="postgresql",
        binding_file=path,
        owner_id="lab-owner",
        durability="sync-pair",
        required_standby="lab_standby",
        operation_timeout_seconds=2,
    )


@pytest.fixture
def repository(storage_config):
    from sonder_runtime.adapters.persistence.postgres_binding import (
        PostgresPrivateBinding,
    )
    from sonder_runtime.adapters.persistence.postgres_continuation import (
        PostgreSQLDurableContinuationRepository,
    )

    config = storage_config
    path = config.binding_file
    binding = PostgresPrivateBinding(
        Path(path), writable_roots=lambda: (Path(__file__).resolve().parents[1],)
    )
    repo = PostgreSQLDurableContinuationRepository(config, binding)
    try:
        yield repo
    finally:
        assert repo.close(runners_stopped=True, timeout=5)


def new_record():
    return DurableChildSession(
        SubagentRequest(
            "pg-conformance-parent",
            "bounded fixture",
            SubagentBudget(max_steps=3),
            "pg-test-" + uuid.uuid4().hex,
        ),
        ChildSessionLineage("pg-conformance-parent"),
    )


def test_actual_pair_keeps_original_logical_receipt(repository):
    from dataclasses import replace
    from sonder_runtime.application.ports.subagents import InvalidSubagentRequest

    record = new_record()
    command = prepare_call("create", record)
    first = repository.mutate(command)
    repository.save_checkpoint(
        ContinuableCheckpoint(record.request.child_id, 0, {"step": 1}),
        expected_sequence=-1,
    )
    replay = repository.mutate(command)
    assert replay.replayed and replay.result_bytes == first.result_bytes
    assert (
        first.storage_acknowledgement
        == replay.storage_acknowledgement
        == "pair_committed"
    )
    assert repository.reconcile(command).storage_acknowledgement == "local_committed"
    assert repository.get(record.request.child_id).checkpoint.sequence == 0
    changed = prepare_call(
        "create",
        replace(record, request=replace(record.request, prompt="changed fixture")),
        operation_id=command.operation_id,
    )
    with pytest.raises(InvalidSubagentRequest):
        repository.mutate(changed)
    assert repository.reconcile(command).result_bytes == first.result_bytes


def test_actual_gate_rejects_before_third_database_callback(repository):
    from sonder_runtime.adapters.persistence.postgres_continuation_transport import (
        PostgresAdmissionUnavailable,
    )

    barrier = Barrier(3)
    entered = []

    def occupy(connection):
        entered.append(connection.info.backend_pid)
        barrier.wait(timeout=3)
        connection.execute("SELECT pg_sleep(0.5)")
        connection.rollback()

    with ThreadPoolExecutor(2) as executor:
        futures = [executor.submit(repository._transport.run, occupy) for _ in range(2)]
        barrier.wait(timeout=3)
        began = time.monotonic()
        with pytest.raises(PostgresAdmissionUnavailable):
            repository._transport.run(
                lambda connection: pytest.fail("third callback admitted")
            )
        assert time.monotonic() - began < 0.25
        for future in futures:
            future.result(timeout=4)
    assert len(set(entered)) == 2
    assert repository._transport.quiescent()
    record = new_record()
    repository.create(record)
    assert repository.get(record.request.child_id) == record


def test_canceled_intent_commit_cannot_start_state_transaction(repository):
    if PAIR_CONTROL is None:
        pytest.skip("requires disposable harness standby controls")
    from sonder_runtime.application.ports.continuation_mutations import (
        ContinuationCommitAmbiguous,
    )

    command = prepare_call("create", new_record())
    stop, start = PAIR_CONTROL
    try:
        stop()
        began = time.monotonic()
        with pytest.raises(ContinuationCommitAmbiguous) as failure:
            repository.mutate(command)
        assert failure.value.prepared == command
        assert time.monotonic() - began < 5
        deadline = time.monotonic() + 3
        while not repository._transport.quiescent() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (
            repository._transport.quiescent()
        ), "cancelled intent started another blocking transaction"
        assert repository.read_mutation(command.operation_id) == command
        assert repository.get(command.child_id) is None
        assert repository.reconcile(command) is None
    finally:
        start()
    assert repository.mutate(command).storage_acknowledgement == "pair_committed"


def test_actual_application_provider_lineage_and_denied_workspace(
    storage_config, tmp_path, monkeypatch
):
    from dataclasses import replace
    from sonder_runtime.bootstrap.app import build_application
    from sonder_runtime.platform.config import SonderConfig
    from sonder_runtime.adapters import conversational_subagents
    from sonder_runtime.adapters.persistence.postgres_continuation import (
        PostgreSQLDurableContinuationRepository,
    )
    from sonder_runtime.application.agents.lineage_delegation import (
        DelegationRequest,
        LineageRecord,
        WorkspaceAssignment,
        IntegrationError,
    )
    from sonder_runtime.application.agents.presets import resolve_preset
    from sonder_runtime.application.context import local_owner_context
    from sonder_runtime.application.ports.subagents import SubagentStatus

    effects = []

    def factory(*dependencies):
        def for_request(request, context):
            def run(state, save, control):
                effects.append(request.child_id)
                save({"step": 1})
                return "bounded fixture result"

            return run

        return for_request

    monkeypatch.setattr(
        conversational_subagents, "conversational_runner_factory", factory
    )
    config = SonderConfig()
    config = replace(
        config,
        child_storage=storage_config,
        state=replace(
            config.state, workspace_roots=(str(tmp_path),), home=str(tmp_path)
        ),
    )
    app = build_application(config=config)
    child = "composed-" + uuid.uuid4().hex
    root = "root-" + uuid.uuid4().hex
    try:
        delegation = app.delegation_service()
        query = app.lineage_query()
        assert isinstance(query._children, PostgreSQLDurableContinuationRepository)
        delegation._provider.register_root(
            root,
            SubagentBudget(
                max_steps=100, max_output_tokens=10000, max_wall_seconds=300
            ),
        )
        preset = resolve_preset("researcher")
        workspace = WorkspaceAssignment((str(tmp_path),), ())
        request = DelegationRequest(
            "delegation-" + uuid.uuid4().hex,
            LineageRecord(
                "lineage-" + uuid.uuid4().hex,
                root,
                root,
                child,
                1,
                preset.name,
                preset.role,
                workspace,
            ),
            "bounded fixture",
            preset,
            workspace,
        )
        context = local_owner_context(
            correlation_id="pg-composition", workspace_roots=(tmp_path,)
        )
        outside = WorkspaceAssignment((str(tmp_path.parent / "outside"),), ())
        denied = replace(
            request,
            workspace=outside,
            lineage=replace(request.lineage, workspace=outside),
        )
        with pytest.raises(IntegrationError, match="outside the parent"):
            delegation.dispatch(denied, context)
        assert effects == [] and query._children.get(child) is None
        result = delegation.dispatch(request, context).result(6)
        assert result.status is SubagentStatus.SUCCEEDED and effects == [child]
        assert any(
            node.node_id == child and node.parent_id == root
            for node in query.snapshot()
        )
        assert query._children.get(child).checkpoint.sequence == 0
    finally:
        app.close_providers(timeout=5)
    reopened = build_application(config=config)
    try:
        assert (
            reopened.lineage_query()._children.get(child).status
            is SubagentStatus.SUCCEEDED
        )
    finally:
        reopened.close_providers(timeout=5)


def test_pair_receipt_replay_requires_a_new_acknowledged_barrier(
    repository, monkeypatch
):
    if PAIR_CONTROL is None:
        pytest.skip("requires disposable harness standby controls")
    from sonder_runtime.application.ports.continuation_mutations import (
        ContinuationCommitAmbiguous,
    )

    command = prepare_call("create", new_record())
    first = repository.mutate(command)
    read_barrier = lambda: repository._read(
        lambda connection: connection.execute(
            "SELECT barrier FROM sonder_child.meta WHERE id=1"
        ).fetchone()[0]
    )
    before = read_barrier()
    stop, start = PAIR_CONTROL
    original = repository._begin
    begins = []

    def lose_standby_after_intent_ack(connection):
        begins.append(True)
        if len(begins) == 2:
            stop()
        original(connection)

    try:
        monkeypatch.setattr(repository, "_begin", lose_standby_after_intent_ack)
        with pytest.raises(ContinuationCommitAmbiguous):
            repository.mutate(command)
        monkeypatch.setattr(repository, "_begin", original)
        deadline = time.monotonic() + 3
        while not repository._transport.quiescent() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert repository._transport.quiescent()
        local = repository.reconcile(command)
        assert local.result_bytes == first.result_bytes
        assert local.storage_acknowledgement == "local_committed"
        ambiguous_barrier = read_barrier()
        assert ambiguous_barrier == before + 1
    finally:
        monkeypatch.setattr(repository, "_begin", original)
        start()
    replay = repository.mutate(command)
    assert replay.storage_acknowledgement == "pair_committed"
    assert replay.result_bytes == first.result_bytes
    assert read_barrier() == ambiguous_barrier + 1


def test_earliest_retained_intent_orders_real_connection_mutations(
    repository, monkeypatch
):
    from threading import Event
    from sonder_runtime.application.ports.continuation_mutations import (
        ContinuationCommitAmbiguous,
        ContinuationStorageFailure,
    )

    record = new_record()
    repository.create(record)
    earlier = prepare_call(
        "save_checkpoint",
        ContinuableCheckpoint(record.request.child_id, 0, {"step": 1}),
        expected_sequence=-1,
    )
    later = prepare_call(
        "request_cancel", record.request.child_id, reason="bounded fixture cancellation"
    )
    entered, release = Event(), Event()
    original = repository._begin
    calls = {}
    first_pid = []

    def interrupt_before_state_transaction(connection):
        pid = connection.info.backend_pid
        if not first_pid:
            first_pid.append(pid)
        calls[pid] = calls.get(pid, 0) + 1
        if pid == first_pid[0] and calls[pid] == 2:
            entered.set()
            assert release.wait(2)
            raise ContinuationStorageFailure(
                "fixture interrupted between durable intent and state transaction"
            )
        original(connection)

    monkeypatch.setattr(repository, "_begin", interrupt_before_state_transaction)
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(repository.mutate, earlier)
        try:
            assert entered.wait(2)
            with pytest.raises(ContinuationCommitAmbiguous) as blocked:
                repository.mutate(later)
            assert blocked.value.prepared == earlier
        finally:
            release.set()
        with pytest.raises(ContinuationCommitAmbiguous):
            future.result(timeout=3)
    monkeypatch.setattr(repository, "_begin", original)
    assert repository.reconcile(earlier) is repository.reconcile(later) is None
    row = repository.get(record.request.child_id)
    assert row.checkpoint is None and not row.cancellation_requested
    assert repository.mutate(earlier).value.checkpoint.sequence == 0
    assert repository.mutate(later).value is True
    row = repository.get(record.request.child_id)
    assert row.checkpoint.sequence == 0 and row.cancellation_requested


def test_slow_owned_pool_worker_prevents_clean_owner_marker(repository, monkeypatch):
    from threading import Event

    entered, release = Event(), Event()
    original = repository.binding.connection_kwargs
    # Ensure a live pooled connection exists, then force its owned replacement
    # worker to pause before creating another connection.
    repository.get("missing-slow-worker-fixture")

    def slow_binding(config):
        entered.set()
        assert release.wait(5)
        return original(config)

    monkeypatch.setattr(repository.binding, "connection_kwargs", slow_binding)
    repository._transport.pool.drain()
    try:
        assert entered.wait(3)
        began = time.monotonic()
        assert not repository.close(runners_stopped=True, timeout=0.1)
        assert time.monotonic() - began < 0.5
        assert any(thread.is_alive() for thread in repository._transport._pool_threads)
        assert not repository._owner_connection.execute(
            "SELECT clean FROM sonder_child.owner WHERE id=1"
        ).fetchone()[0]
        repository._owner_connection.rollback()
    finally:
        release.set()
    assert repository.close(runners_stopped=True, timeout=5)
    assert all(not thread.is_alive() for thread in repository._transport._pool_threads)
    assert repository._owner_connection.closed


def test_driver_upgrade_requires_review_before_pool_creation(
    storage_config, monkeypatch
):
    import psycopg_pool
    from sonder_runtime.adapters.persistence.postgres_continuation_transport import (
        PostgresContinuationTransport,
    )
    from sonder_runtime.application.ports.continuation_mutations import (
        ContinuationStorageFailure,
    )

    monkeypatch.setattr(psycopg_pool, "__version__", "unreviewed")
    monkeypatch.setattr(
        psycopg_pool,
        "ConnectionPool",
        lambda **kw: pytest.fail("unreviewed pool created"),
    )
    with pytest.raises(ContinuationStorageFailure, match="versions require reviewed"):
        PostgresContinuationTransport(storage_config, None)


def test_unknown_pool_ownership_shape_fails_closed(storage_config, monkeypatch):
    import psycopg_pool
    from sonder_runtime.adapters.persistence.postgres_continuation_transport import (
        PostgresContinuationTransport,
    )
    from sonder_runtime.application.ports.continuation_mutations import (
        ContinuationStorageFailure,
    )

    closed = []

    class UnknownPool:
        def __init__(self, **kwargs):
            pass

        def open(self):
            pass

        def close(self, timeout):
            closed.append(timeout)

    monkeypatch.setattr(psycopg_pool, "ConnectionPool", UnknownPool)
    with pytest.raises(ContinuationStorageFailure, match="ownership structure"):
        PostgresContinuationTransport(storage_config, None)
    assert closed == [0]


@pytest.mark.parametrize("sqlstate", ["01000", "01001", "P0001"])
def test_localized_warning_stops_success_and_next_transaction(repository, sqlstate):
    from sonder_runtime.application.ports.continuation_mutations import (
        ContinuationStorageFailure,
    )

    after = []

    def warned(connection):
        repository._begin(connection)
        connection.execute(
            "DO $$ BEGIN RAISE WARNING USING ERRCODE = '"
            + sqlstate
            + "', MESSAGE = 'réplication interrompue'; END $$"
        )
        connection.commit()
        repository._begin(connection)
        after.append(True)
        connection.rollback()

    with pytest.raises(ContinuationStorageFailure):
        repository._transport.run(warned)
    assert after == []
