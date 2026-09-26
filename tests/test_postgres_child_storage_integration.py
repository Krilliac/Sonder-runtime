"""Opt-in tests against a disposable PostgreSQL pair; never reset its schema.

The workspace conformance harness supplies an ACL-protected binding and owns
both database processes. These tests do not start services or accept a DSN.
"""

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from pathlib import Path
from threading import Barrier

import pytest

from sonder_runtime.application.ports.continuation_mutations import prepare_call
from sonder_runtime.application.ports.continuation_records import (
    ChildSessionLineage,
    DurableChildSession,
)
from sonder_runtime.application.ports.subagents import SubagentBudget, SubagentRequest
from sonder_runtime.application.subagents.continuable import ContinuableCheckpoint
from sonder_runtime.platform.child_storage_config import ChildStorageConfig

# Supplied only by the in-process disposable harness; never read from a DSN,
# request payload, or deployment environment. The root pytest isolation hook
# intentionally clears all ambient SONDER_* variables before test setup.
PAIR_BINDING = None
PAIR_CONTROL = None


@pytest.fixture
def storage_config():
    path = PAIR_BINDING
    if path is None or PAIR_CONTROL is None:
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


def _competing_owner(config, response):
    from sonder_runtime.adapters.persistence.postgres_binding import (
        PostgresPrivateBinding,
    )
    from sonder_runtime.adapters.persistence.postgres_continuation import (
        PostgreSQLDurableContinuationRepository,
    )
    from sonder_runtime.application.ports.continuation_mutations import (
        ContinuationStorageFailure,
    )

    binding = PostgresPrivateBinding(
        Path(config.binding_file), writable_roots=lambda: (Path(__file__).resolve().parents[1],)
    )
    try:
        try:
            contender = PostgreSQLDurableContinuationRepository(config, binding)
        except ContinuationStorageFailure:
            response.put("fenced")
        else:
            contender.close(runners_stopped=True, timeout=5)
            response.put("unexpected second owner")
    finally:
        binding.close()


def test_actual_pair_rejects_second_execution_process(repository, storage_config):
    multiprocessing = get_context("spawn")
    response = multiprocessing.Queue()
    contender = multiprocessing.Process(target=_competing_owner, args=(storage_config, response))
    contender.start()
    try:
        contender.join(timeout=12)
        assert contender.exitcode == 0
        assert response.get(timeout=2) == "fenced"
        assert repository.create(new_record()).status.value == "created"
    finally:
        if contender.is_alive():
            contender.terminate()
            contender.join(timeout=2)


def test_actual_pair_serializes_distinct_child_admissions_for_one_root(repository):
    from sonder_runtime.application.ports.subagents import InvalidSubagentRequest
    from sonder_runtime.application.subagents.durable_continuation import (
        DurableContinuationService,
    )

    root_id = "pg-root-" + uuid.uuid4().hex
    DurableContinuationService(repository).register_root(
        root_id, SubagentBudget(max_steps=10, max_children=2, max_depth=2, max_concurrency=1),
    )
    barrier = Barrier(2)

    def reserve(index):
        record = DurableChildSession(
            SubagentRequest(root_id, "raced child", SubagentBudget(
                max_steps=5, max_children=2, max_depth=2, max_concurrency=1,
            ), "pg-raced-" + uuid.uuid4().hex),
            ChildSessionLineage(root_id),
        )
        barrier.wait(timeout=3)
        try:
            repository.create(record)
        except InvalidSubagentRequest as error:
            return "rejected", str(error)
        return "admitted", record.request.child_id

    with ThreadPoolExecutor(2) as executor:
        outcomes = list(executor.map(reserve, range(2)))
    assert sorted(status for status, _ in outcomes) == ["admitted", "rejected"]
    assert "concurrency" in next(value for status, value in outcomes if status == "rejected")


def test_actual_pair_recovers_scoped_keys_and_rejects_parallel_duplicates(repository):
    from sonder_runtime.application.ports.subagents import (
        InvalidSubagentRequest,
        SubagentResult,
        SubagentStatus,
    )
    from sonder_runtime.application.subagents.durable_continuation import (
        DurableContinuationService,
    )

    root = "pg-key-root-" + uuid.uuid4().hex
    key = "pg-key-" + uuid.uuid4().hex
    DurableContinuationService(repository).register_root(
        root, SubagentBudget(max_steps=20, max_children=3,
                             max_depth=2, max_concurrency=3),
    )
    candidates = tuple(DurableChildSession(
        SubagentRequest(root, "same scoped work", SubagentBudget(
            max_steps=4, max_children=3, max_depth=2, max_concurrency=3,
        ),
                        "pg-key-child-" + uuid.uuid4().hex, resume_key=key,
                        idempotency_key=key),
        ChildSessionLineage(root),
    ) for _ in range(2))
    barrier = Barrier(2)

    def reserve(record):
        barrier.wait(timeout=3)
        try:
            repository.create(record)
        except InvalidSubagentRequest as error:
            return "rejected", str(error)
        return "admitted", record.request.child_id

    with ThreadPoolExecutor(2) as executor:
        outcomes = list(executor.map(reserve, candidates))
    assert sorted(status for status, _ in outcomes) == ["admitted", "rejected"], outcomes
    assert "key already exists" in next(detail for status, detail in outcomes if status == "rejected")
    winner = next(value for status, value in outcomes if status == "admitted")
    for namespace in ("resume", "idempotency"):
        assert repository.get_active_by_key(root, key, namespace).request.child_id == winner
        assert repository.get_by_key(root, key, namespace).request.child_id == winner
    assert repository.update(winner, status=SubagentStatus.SUCCEEDED, expected_revision=0,
                             result=SubagentResult(winner, root, SubagentStatus.SUCCEEDED,
                                                   output="done")) is not None
    for namespace in ("resume", "idempotency"):
        assert repository.get_active_by_key(root, key, namespace) is None
        assert repository.get_by_key(root, key, namespace).request.child_id == winner


def test_actual_pair_serializes_owner_width_across_operation_roots(repository):
    from sonder_runtime.application.ports.subagents import InvalidSubagentRequest
    from sonder_runtime.application.subagents.durable_continuation import (
        DurableContinuationService,
    )

    roots = ("pg-operation-" + uuid.uuid4().hex, "pg-operation-" + uuid.uuid4().hex)
    owner = "pg-owner-" + uuid.uuid4().hex
    service = DurableContinuationService(repository)
    for root in roots:
        service.register_root(
            root, SubagentBudget(max_steps=20, max_children=2,
                                 max_depth=2, max_concurrency=1), owner_id=owner,
        )
    children = tuple(DurableChildSession(
        SubagentRequest(root, "competing host operation", SubagentBudget(
            max_steps=5, max_children=2, max_depth=2, max_concurrency=1,
        ), "pg-owner-child-" + uuid.uuid4().hex), ChildSessionLineage(root),
    ) for root in roots)
    barrier = Barrier(2)

    def reserve(child):
        barrier.wait(timeout=3)
        try:
            repository.create(child)
        except InvalidSubagentRequest as error:
            return "rejected", str(error)
        return "admitted", child.request.child_id

    with ThreadPoolExecutor(2) as executor:
        outcomes = list(executor.map(reserve, children))
    assert sorted(status for status, _ in outcomes) == ["admitted", "rejected"]
    assert "host concurrency" in next(value for status, value in outcomes if status == "rejected")
    admitted = children[next(index for index, (status, _value) in enumerate(outcomes)
                             if status == "admitted")]
    rejected = children[next(index for index, (status, _value) in enumerate(outcomes)
                             if status == "rejected")]
    assert repository.request_cancel(
        admitted.request.child_id, reason="release owner reservation",
        expected_revision=0, unstarted_only=True,
    )
    assert repository.create(rejected).status.value == "created"


def test_actual_pair_serializes_owned_scope_and_cancellation(repository):
    from sonder_runtime.application.ports.subagents import InvalidSubagentRequest
    from sonder_runtime.application.ports.worker_registry import (
        WorkerExecutionContract,
        WorkerLaunch,
    )
    from sonder_runtime.application.subagents.durable_continuation import (
        DurableContinuationService,
    )
    from sonder_runtime.application.worker_registry.continuation import _request_for

    root_id = "pg-owned-root-" + uuid.uuid4().hex
    DurableContinuationService(repository).register_root(
        root_id, SubagentBudget(max_steps=10, max_children=3, max_depth=2, max_concurrency=2),
    )
    owned_file = str(Path(__file__).resolve())

    def record(index):
        key = "pg-owned-key-" + uuid.uuid4().hex
        launch = WorkerLaunch(
            "pg-owned-" + uuid.uuid4().hex, root_id, "researcher", "local", "provider", "default",
            (str(Path(__file__).resolve().parents[1]),), ("research",),
            {"max_steps": 5, "max_children": 2, "max_depth": 2, "max_concurrency": 2},
            {"max_attempts": 1}, key, key, "owned PG race", "owner",
            execution_contract=WorkerExecutionContract(owned_files=(owned_file,)),
        )
        return DurableChildSession(_request_for(launch), ChildSessionLineage(root_id))

    left, right = record(0), record(1)
    barrier = Barrier(2)

    def reserve(item):
        barrier.wait(timeout=3)
        try:
            return repository.create(item)
        except InvalidSubagentRequest:
            return None

    with ThreadPoolExecutor(2) as executor:
        rows = list(executor.map(reserve, (left, right)))
    assert sum(row is not None for row in rows) == 1
    winner, loser = (left, right) if rows[0] is not None else (right, left)
    barrier = Barrier(2)

    def cancel():
        barrier.wait(timeout=3)
        return repository.request_cancel(
            winner.request.child_id, reason="release owned slot",
            expected_revision=0, unstarted_only=True,
        )

    def retry():
        barrier.wait(timeout=3)
        try:
            return repository.create(loser)
        except InvalidSubagentRequest:
            return None

    with ThreadPoolExecutor(2) as executor:
        cancelled, admitted = executor.submit(cancel), executor.submit(retry)
        assert cancelled.result() is True
        pending = admitted.result()
    assert repository.get(winner.request.child_id).status.value == "cancelled"
    assert repository.get(winner.request.child_id).result.error.code == "cancelled_before_start"
    if pending is None:
        assert repository.create(loser).request.child_id == loser.request.child_id
    assert repository.get(loser.request.child_id).status.value == "created"


def test_actual_pair_reserves_speculative_hypothesis_once(repository):
    from sonder_runtime.application.ports.subagents import InvalidSubagentRequest
    from sonder_runtime.application.subagents.durable_continuation import (
        DurableContinuationService,
    )

    root_id = "pg-speculative-root-" + uuid.uuid4().hex
    DurableContinuationService(repository).register_root(
        root_id, SubagentBudget(max_steps=10, max_children=3, max_depth=2, max_concurrency=2),
    )
    barrier = Barrier(2)

    def reserve(index):
        child = DurableChildSession(
            SubagentRequest(
                root_id, "raced hypothesis", SubagentBudget(
                    max_steps=5, max_children=2, max_depth=2, max_concurrency=2,
                ), "pg-speculative-" + uuid.uuid4().hex,
                (("execution_task_scope", "question"), ("execution_speculative_lane", "true"),
                 ("speculative_lane_id", f"lane-{index}"),
                 ("hypothesis_digest", "a" * 64)),
            ),
            ChildSessionLineage(root_id),
        )
        barrier.wait(timeout=3)
        try:
            repository.create(child)
        except InvalidSubagentRequest as error:
            return "rejected", str(error)
        return "admitted", child.request.child_id

    with ThreadPoolExecutor(2) as executor:
        outcomes = list(executor.map(reserve, range(2)))
    assert sorted(status for status, _ in outcomes) == ["admitted", "rejected"]
    assert "speculative hypothesis" in next(value for status, value in outcomes if status == "rejected")


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

    from sonder_runtime.adapters import conversational_subagents
    from sonder_runtime.adapters.persistence.postgres_continuation import (
        PostgreSQLDurableContinuationRepository,
    )
    from sonder_runtime.application.agents.lineage_delegation import (
        DelegationRequest,
        IntegrationError,
        LineageRecord,
        WorkspaceAssignment,
    )
    from sonder_runtime.application.agents.presets import resolve_preset
    from sonder_runtime.application.context import local_owner_context
    from sonder_runtime.application.ports.subagents import SubagentStatus
    from sonder_runtime.bootstrap.app import build_application
    from sonder_runtime.platform.config import SonderConfig

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
    context = local_owner_context(
        correlation_id="pg-composition", workspace_roots=(tmp_path,)
    )
    try:
        delegation = app.delegation_service()
        root = delegation.root_id_for_context(context)
        query = app.lineage_query()
        assert isinstance(query._children, PostgreSQLDurableContinuationRepository)
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


def test_owner_loss_during_commit_never_publishes_success(repository, monkeypatch):
    from sonder_runtime.application.ports.continuation_mutations import (
        ContinuationCommitAmbiguous,
        ContinuationStorageFailure,
    )

    command = prepare_call("create", new_record())
    capacity = repository._capacity
    calls = []

    def lose_owner_after_state_authorization(connection, extra=0):
        capacity(connection, extra)
        calls.append(True)
        if len(calls) == 3:
            repository._owner_connection.close()

    monkeypatch.setattr(repository, "_capacity", lose_owner_after_state_authorization)
    try:
        with pytest.raises(ContinuationCommitAmbiguous) as failure:
            repository.mutate(command)
        assert failure.value.prepared == command
    finally:
        with pytest.raises(ContinuationStorageFailure):
            repository.get(command.child_id)
    assert repository._owner_fenced
    # A separate test-owned read connection observes the committed logical
    # receipt. It cannot authorize a runner or clear the durable owner marker.
    connection = repository._transport.connection_class.connect(
        **repository.binding.connection_kwargs(repository.config)
    )
    try:
        assert (
            connection.execute(
                "SELECT count(*) FROM sonder_child.receipt WHERE operation_id=%s",
                (command.operation_id,),
            ).fetchone()[0]
            == 1
        )
        assert not connection.execute(
            "SELECT clean FROM sonder_child.owner WHERE id=1"
        ).fetchone()[0]
    finally:
        connection.close()


@pytest.mark.parametrize("surface", ["repl", "legacy-mcp"])
def test_toml_cli_uses_actual_postgres_graph(
    storage_config, tmp_path, monkeypatch, surface
):
    import json
    from types import SimpleNamespace

    import server
    from sonder_runtime import __main__ as cli
    from sonder_runtime.adapters.persistence.postgres_continuation import (
        PostgreSQLDurableContinuationRepository,
    )
    from sonder_runtime.bootstrap import app as composition
    from sonder_runtime.bootstrap import legacy_root
    from sonder_runtime.interfaces.repl import repl
    from sonder_runtime.platform import logging as runtime_logging

    composition.reset_for_tests()
    monkeypatch.setattr(runtime_logging, "configure_logging", lambda **kwargs: None)
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setattr(server, "_APP_GRAPH", None)
    monkeypatch.setattr(legacy_root, "_owned_application", None)
    path = tmp_path / "explicit-postgres.toml"
    path.write_text(
        "[state]\nhome = "
        + json.dumps(str(tmp_path))
        + "\nworkspace_roots = ["
        + json.dumps(str(tmp_path))
        + "]\n[child_storage]\nbackend = 'postgresql'\nowner_id = 'lab-owner'\ndurability = 'sync-pair'\nrequired_standby = 'lab_standby'\nbinding_file = "
        + json.dumps(storage_config.binding_file)
        + "\n",
        encoding="utf-8",
    )
    seen = []

    def run(*args, **kwargs):
        application = server._application()
        assert application.config.child_storage.backend == "postgresql"
        assert isinstance(
            application.lineage_query()._children,
            PostgreSQLDurableContinuationRepository,
        )
        assert str(path.resolve()) in application.private_source_paths
        assert (
            str(Path(storage_config.binding_file).resolve())
            in application.private_source_paths
        )
        root = "cli-root-" + uuid.uuid4().hex
        application.delegation_service()._provider.register_root(
            root, SubagentBudget(max_steps=3)
        )
        assert application.lineage_query()._children.get(root).request.child_id == root
        assert "SONDER_CHILD_STORAGE_BINDING_FILE" not in os.environ
        seen.append(application)

    arguments = SimpleNamespace(
        config=str(path), secrets=None, set=None, json=True, native=False
    )
    if surface == "repl":
        monkeypatch.setattr(repl, "run_jsonl", run)
        assert cli.cmd_repl(arguments) == 0
    else:
        monkeypatch.setattr(server, "require_mcp_startup_safety", lambda: None)
        monkeypatch.setattr(server, "run_mcp", run)
        assert cli.cmd_mcp(arguments) == 0
    assert len(seen) == 1
    assert seen[0].lineage_query()._children._closed


# --- Journal-stamped checkpoints and child resume (#515) ----------------------
#
# Production wraps whichever child repository it composes in
# DurableContinuationService with a JournalProvenanceStamp over the SQLite
# worker-effects journal.  These cases run that path against the live pair.
# A killed interpreter would leave this pair's single durable owner marker
# unclean (only the owner-loss canary may do that), so each cut is a failure
# raised at the same point instead of os._exit.  For the database the effect
# is the same: a transaction that never reached COMMIT is rolled back.

_PROVENANCE_RUN = "subagent:pg-provenance"
_PROVENANCE_WORKER = "subagent:pg-worker"


class _Cut(RuntimeError):
    """Stands in for the host dying at one cut point."""


class _CutBeforeReceipt:
    """Connection proxy: fail after the snapshot UPDATE, before its receipt."""

    def __init__(self, connection, child_id, sequence, fired):
        self._connection, self._child_id = connection, child_id
        self._sequence, self._fired = sequence, fired

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def execute(self, query, *args, **kwargs):
        if isinstance(query, str) and query.startswith("INSERT INTO sonder_child.receipt"):
            from sonder_runtime.adapters.persistence.postgres_continuation import (
                decode_child_snapshot,
            )

            # The provenance-carrying snapshot is written inside this open
            # transaction; the cut lands before the receipt and COMMIT.
            snapshot = self._connection.execute(
                "SELECT snapshot FROM sonder_child.child WHERE child_id=%s",
                (self._child_id,),
            ).fetchone()[0]
            written = decode_child_snapshot(bytes(snapshot)).checkpoint
            assert written.sequence == self._sequence
            assert written.provenance is not None
            self._fired.append(written)
            raise _Cut("host stopped inside the checkpoint compare-and-set")
        return self._connection.execute(query, *args, **kwargs)


def _cut_checkpoint_cas(repository, monkeypatch, child_id, sequence):
    from sonder_runtime.application.subagents.continuation_codec import decode_call

    transport, fired = repository._transport, []
    original = transport.run

    def run(function, *, prepared=None, **kwargs):
        if prepared is not None and prepared.kind == "save_checkpoint" and not fired:
            args, _kwargs = decode_call(prepared)
            if args[0].child_id == child_id and args[0].sequence == sequence:
                return original(
                    lambda connection: function(
                        _CutBeforeReceipt(connection, child_id, sequence, fired)
                    ),
                    prepared=prepared, **kwargs,
                )
        return original(function, prepared=prepared, **kwargs)

    monkeypatch.setattr(transport, "run", run)
    return fired


def _provenance_journal(root):
    from sonder_runtime.adapters.persistence.durable_continuation import (
        SQLiteJournalProvenanceSource,
    )
    from sonder_runtime.adapters.persistence.sqlite.effect_journal import (
        SQLiteEffectJournal,
    )
    from sonder_runtime.application.execution.worker_bindings import (
        AuthenticatedWorkerBinding,
    )
    from sonder_runtime.application.subagents.checkpoint_provenance import (
        JournalProvenanceStamp,
        ProvenanceBinding,
    )

    journal = SQLiteEffectJournal(root / "worker-effects.db")
    source = SQLiteJournalProvenanceSource(journal, create_identity=True)
    journal.claim_owner(_PROVENANCE_RUN, _PROVENANCE_WORKER, 1)
    binding = AuthenticatedWorkerBinding(
        journal, _PROVENANCE_RUN, _PROVENANCE_WORKER, 1, "local-subagents",
    )
    stamp = JournalProvenanceStamp(
        source, lambda _subject: ProvenanceBinding(_PROVENANCE_RUN, _PROVENANCE_WORKER, 1),
    )
    return journal, source, binding, stamp


def _validate_resume(checkpoint, source, *, epoch):
    from sonder_runtime.application.subagents.checkpoint_provenance import (
        validate_checkpoint_resume,
    )

    return validate_checkpoint_resume(
        checkpoint, source, run_id=_PROVENANCE_RUN, worker_id=_PROVENANCE_WORKER,
        resumer_owner_epoch=epoch,
    )


@pytest.mark.parametrize("cut", ["after_receipt", "in_cas", "after_cas"])
def test_actual_pair_checkpoint_cuts_never_pass_the_settled_journal(
    repository, tmp_path, monkeypatch, cut
):
    from sonder_runtime.application.context import local_owner_context
    from sonder_runtime.application.execution.effect_journal import EffectState
    from sonder_runtime.application.execution.worker_bindings import journaled_effect
    from sonder_runtime.application.ports.subagents import SubagentStatus
    from sonder_runtime.application.subagents.continuable import (
        checkpoint_state_digest,
    )
    from sonder_runtime.application.subagents.durable_continuation import (
        DurableContinuationService,
    )

    journal, source, binding, stamp = _provenance_journal(tmp_path)
    root_id = "pg-provenance-root-" + uuid.uuid4().hex
    child_id = "pg-provenance-" + uuid.uuid4().hex
    service = DurableContinuationService(repository, checkpoint_provenance=stamp)
    service.register_root(root_id, SubagentBudget(max_steps=8))
    fired = (
        _cut_checkpoint_cas(repository, monkeypatch, child_id, 1)
        if cut == "in_cas" else []
    )

    def runner(_state, save, _control):
        save({"phase": "before"}, "before")
        journaled_effect(  # The journal receipt commits before the next save.
            binding, operation_id="op-write-1", idempotency_key="write-1",
            request={"key": "write-1"}, invoke=lambda: "done",
            receipt_key="receipt:write-1",
        )
        if cut == "after_receipt":
            raise _Cut("host stopped after the journal receipt")
        save({"phase": "after"}, "after")
        if cut == "after_cas":
            raise _Cut("host stopped after the checkpoint compare-and-set")
        return "unreachable"

    handle = service.spawn(
        SubagentRequest(root_id, "bounded provenance fixture", SubagentBudget(max_steps=8), child_id),
        local_owner_context(correlation_id="pg-provenance-" + cut),
        runner,
    )
    try:
        handle.result(10)
    except Exception:
        pass  # A storage cut surfaces as the service's storage failure.
    assert service.close(5)
    if cut == "in_cas":
        assert len(fired) == 1  # The cut really ran inside the open transaction.
        monkeypatch.undo()

    record = repository.get(child_id)
    checkpoint = record.checkpoint
    provenance = checkpoint.provenance
    receipts = journal.effects_since(_PROVENANCE_RUN, 0).records
    assert [(item.idempotency_key, item.state) for item in receipts] == [
        ("write-1", EffectState.COMPLETED),
    ]
    assert provenance is not None and provenance.digest_valid
    assert provenance.child_id == child_id
    assert provenance.settled_position <= journal.settled_high_water(_PROVENANCE_RUN)
    assert provenance.state_digest == checkpoint_state_digest(checkpoint.state)
    if cut == "after_cas":
        assert (checkpoint.sequence, checkpoint.state, provenance.settled_position) == (
            1, {"phase": "after"}, 1,
        )
    else:
        assert (checkpoint.sequence, checkpoint.state, provenance.settled_position) == (
            0, {"phase": "before"}, 0,
        )
    if cut == "in_cas":
        # The rolled-back save leaves its admitted intent without a receipt:
        # the child stays fenced as an ambiguous mutation.
        assert repository.unresolved_mutation(child_id) is not None
    else:
        assert record.status is SubagentStatus.FAILED and record.recovery_required

    journal.claim_owner(_PROVENANCE_RUN, _PROVENANCE_WORKER, 2)
    decision = _validate_resume(checkpoint, source, epoch=2)
    assert decision.allowed, decision.detail
    settled = decision.receipts if cut == "after_cas" else decision.later_receipts
    assert tuple(settled) == ("write-1",)
    assert settled["write-1"].receipt_key == "receipt:write-1"


def test_actual_pair_refuses_superseded_and_foreign_checkpoint_provenance(
    repository, tmp_path
):
    from sonder_runtime.adapters.persistence.durable_continuation import (
        SQLiteJournalProvenanceSource,
    )
    from sonder_runtime.adapters.persistence.sqlite.effect_journal import (
        SQLiteEffectJournal,
    )
    from sonder_runtime.application.context import local_owner_context
    from sonder_runtime.application.execution.worker_bindings import (
        AuthenticatedWorkerBinding,
        journaled_effect,
    )
    from sonder_runtime.application.subagents.checkpoint_provenance import (
        CheckpointResumeRefusal,
    )
    from sonder_runtime.application.subagents.durable_continuation import (
        DurableContinuationService,
    )

    journal, source, binding, stamp = _provenance_journal(tmp_path / "journal")
    root_id = "pg-provenance-root-" + uuid.uuid4().hex
    child_id = "pg-provenance-" + uuid.uuid4().hex
    service = DurableContinuationService(repository, checkpoint_provenance=stamp)
    service.register_root(root_id, SubagentBudget(max_steps=8))

    def effect(owner, key):
        journaled_effect(
            owner, operation_id=f"op-{key}", idempotency_key=key,
            request={"key": key}, invoke=lambda: key, receipt_key=f"receipt:{key}",
        )

    def runner(_state, save, _control):
        effect(binding, "a")
        save({"step": 1}, "cursor-1")
        raise _Cut("host stopped after the checkpoint")

    handle = service.spawn(
        SubagentRequest(root_id, "bounded provenance fixture", SubagentBudget(max_steps=8), child_id),
        local_owner_context(correlation_id="pg-provenance-refusals"), runner,
    )
    handle.result(10)
    assert service.close(5)
    checkpoint = repository.get(child_id).checkpoint
    assert checkpoint.provenance.settled_position == 1

    # A newer owner settles more work: the old stamp still names a valid
    # prefix, so it is accepted while the stale epoch itself is refused.
    journal.claim_owner(_PROVENANCE_RUN, _PROVENANCE_WORKER, 2)
    newer = AuthenticatedWorkerBinding(
        journal, _PROVENANCE_RUN, _PROVENANCE_WORKER, 2, "local-subagents",
    )
    effect(newer, "b")
    assert _validate_resume(checkpoint, source, epoch=2).allowed
    assert _validate_resume(checkpoint, source, epoch=1).reason is (
        CheckpointResumeRefusal.STALE_OWNER_EPOCH
    )

    # A journal file with another identity cannot authorize the checkpoint.
    other = SQLiteEffectJournal(tmp_path / "other" / "worker-effects.db")
    other_source = SQLiteJournalProvenanceSource(other, create_identity=True)
    other.claim_owner(_PROVENANCE_RUN, _PROVENANCE_WORKER, 2)
    assert _validate_resume(checkpoint, other_source, epoch=2).reason is (
        CheckpointResumeRefusal.JOURNAL_IDENTITY_MISMATCH
    )


def test_actual_pair_store_refuses_provenance_for_a_different_subject(
    repository, tmp_path
):
    from sonder_runtime.application.ports.subagents import InvalidSubagentRequest
    from sonder_runtime.application.subagents.continuable import (
        CheckpointProvenance,
        checkpoint_state_digest,
    )

    _journal, source, _binding, _stamp = _provenance_journal(tmp_path)
    record = repository.create(new_record())
    child_id = record.request.child_id
    stamped = CheckpointProvenance.stamp(
        child_id=child_id, sequence=0, state_digest=checkpoint_state_digest({"step": 1}),
        cursor="c1",
        journal_identity=source.position(_PROVENANCE_RUN, _PROVENANCE_WORKER).journal_identity,
        run_id=_PROVENANCE_RUN, worker_id=_PROVENANCE_WORKER, owner_epoch=1,
        settled_position=0,
    )
    forged = ContinuableCheckpoint(child_id, 0, {"step": 999}, "c1", stamped)
    with pytest.raises(InvalidSubagentRequest, match="provenance"):
        repository.save_checkpoint(forged, expected_sequence=-1)
    assert repository.get(child_id).checkpoint is None
    genuine = ContinuableCheckpoint(child_id, 0, {"step": 1}, "c1", stamped)
    saved = repository.save_checkpoint(genuine, expected_sequence=-1)
    assert saved.checkpoint.provenance == stamped
    assert repository.get(child_id).checkpoint.provenance == stamped


_RESUME_WRITE = "append-once"


def _resume_runner_factory(root, *, fail_after_write):
    """Checkpoint, perform one journaled append, checkpoint again."""
    import hashlib

    from sonder_runtime.application.execution import effect_journal

    target = root / "workspace" / "append.txt"
    trace = root / "runner-trace.log"

    def bind(request, _context):
        key = f"{request.child_id}:{_RESUME_WRITE}"

        def run(state, save, _control):
            binding = effect_journal.current()
            assert binding is not None, "runner must execute under the child journal binding"
            if int(state.get("step", 0)) == 0:
                save({"step": 1}, "before-write")
            settled = effect_journal.settled_receipt(key)
            if settled is None:
                intent = binding.begin_request(
                    operation_id=_RESUME_WRITE, idempotency_key=key,
                    request_digest=hashlib.sha256(b"append x").hexdigest(),
                    reconciliation="manual",
                )
                with target.open("a", encoding="utf-8") as handle:
                    handle.write("x")
                    handle.flush()
                    os.fsync(handle.fileno())
                binding.complete(
                    intent, outcome_digest=hashlib.sha256(b"x").hexdigest(),
                    receipt_key="append.txt:1",
                )
                with trace.open("a", encoding="utf-8") as handle:
                    handle.write("wrote\n")
                if fail_after_write:
                    # Receipt committed; the next checkpoint was never saved.
                    raise _Cut("host stopped after the journaled append")
                settled = "append.txt:1"
            else:
                with trace.open("a", encoding="utf-8") as handle:
                    handle.write(f"consumed {settled}\n")
            save({"step": 2, "write_receipt": settled}, "after-write")
            return "resumed output"

        return run

    return lambda *_dependencies: bind


def _compose_resume_application(storage_config, root, monkeypatch, *, fail_after_write):
    from dataclasses import replace

    from sonder_runtime.adapters import conversational_subagents
    from sonder_runtime.bootstrap.app import build_application
    from sonder_runtime.platform.config import SonderConfig

    monkeypatch.setattr(
        conversational_subagents, "conversational_runner_factory",
        _resume_runner_factory(root, fail_after_write=fail_after_write),
    )
    config = SonderConfig()
    return build_application(config=replace(
        config,
        child_storage=storage_config,
        state=replace(
            config.state, home=str(root / "state"),
            workspace_roots=(str(root / "workspace"),),
        ),
    ))


def _delegate_resume_child(application, root, child_id, delegation_id):
    from sonder_runtime.application.agents.lineage_delegation import (
        DelegationRequest,
        LineageRecord,
        WorkspaceAssignment,
    )
    from sonder_runtime.application.agents.presets import resolve_preset
    from sonder_runtime.application.context import local_owner_context

    workspace = root / "workspace"
    delegation = application.delegation_service()
    context = local_owner_context(
        correlation_id="pg-resume-op", workspace_roots=(workspace,),
    )
    root_id = delegation.root_id_for_context(context)
    preset = resolve_preset("researcher")
    assignment = WorkspaceAssignment((str(workspace),))
    lineage = LineageRecord(
        "lineage-" + delegation_id, root_id, root_id, child_id, 1,
        preset.name, preset.role, assignment,
    )
    request = DelegationRequest(delegation_id, lineage, "append once", preset, assignment)
    return delegation.dispatch(request, context)


def _interrupted_child(storage_config, root, monkeypatch):
    """Run a child whose host stops after its append's receipt committed."""
    from sonder_runtime.application.ports.subagents import SubagentStatus

    (root / "workspace").mkdir(parents=True)
    child_id = "pg-resume-" + uuid.uuid4().hex
    delegation_id = "pg-resume-delegation-" + uuid.uuid4().hex
    application = _compose_resume_application(
        storage_config, root, monkeypatch, fail_after_write=True,
    )
    try:
        result = _delegate_resume_child(application, root, child_id, delegation_id).result(30)
        assert result.status is SubagentStatus.FAILED, result
    finally:
        # A clean close leaves the pair's owner marker eligible for the next
        # composition, which is the restart below.
        application.close_providers(timeout=10)
    assert (root / "workspace" / "append.txt").read_text(encoding="utf-8") == "x"
    return child_id, delegation_id


def test_actual_pair_child_resumes_from_stamped_checkpoint_and_consumes_settled_write(
    storage_config, tmp_path, monkeypatch
):
    from sonder_runtime.adapters.persistence.postgres_continuation import (
        PostgreSQLDurableContinuationRepository,
    )
    from sonder_runtime.adapters.persistence.sqlite.effect_journal import (
        SQLiteEffectJournal,
    )
    from sonder_runtime.application.execution.effect_journal import EffectState
    from sonder_runtime.application.ports.subagents import SubagentStatus

    child_id, delegation_id = _interrupted_child(storage_config, tmp_path, monkeypatch)
    journal = SQLiteEffectJournal(tmp_path / "state" / "worker-effects.db")
    run_id = f"subagent:{child_id}"
    assert [(r.operation_id, r.state) for r in journal.effects_since(run_id, 0).records] == [
        (f"subagent-dispatch:{child_id}", EffectState.COMPLETED),
        (_RESUME_WRITE, EffectState.COMPLETED),
    ]

    application = _compose_resume_application(
        storage_config, tmp_path, monkeypatch, fail_after_write=False,
    )
    try:
        repository = application.delegation_service()._provider._local_service._repository
        assert isinstance(repository, PostgreSQLDurableContinuationRepository)
        interrupted = repository.get(child_id)
        assert interrupted.status is SubagentStatus.FAILED and interrupted.recovery_required
        # The production save path stamped the checkpoint and the live
        # PostgreSQL store returned the provenance intact.
        provenance = interrupted.checkpoint.provenance
        assert provenance is not None and provenance.digest_valid
        assert (provenance.run_id, provenance.settled_position) == (run_id, 1)
        assert provenance.worker_id.startswith("subagent:")
        assert interrupted.checkpoint.state == {"step": 1}

        result = _delegate_resume_child(
            application, tmp_path, child_id, delegation_id,
        ).result(30)
        assert result.status is SubagentStatus.SUCCEEDED, result
        assert result.output == "resumed output"
        final = repository.get(child_id)
    finally:
        application.close_providers(timeout=10)

    # The append was consumed from its settled receipt, never repeated.
    assert (tmp_path / "workspace" / "append.txt").read_text(encoding="utf-8") == "x"
    assert (tmp_path / "runner-trace.log").read_text(encoding="utf-8").splitlines() == [
        "wrote", "consumed append.txt:1",
    ]
    assert [r.operation_id for r in journal.effects_since(run_id, 0).records] == [
        f"subagent-dispatch:{child_id}", _RESUME_WRITE,
    ]
    assert final.checkpoint.state == {"step": 2, "write_receipt": "append.txt:1"}
    assert final.checkpoint.provenance is not None
    assert final.checkpoint.provenance.owner_epoch > provenance.owner_epoch


def test_actual_pair_resume_refuses_a_swapped_journal_identity(
    storage_config, tmp_path, monkeypatch
):
    import sqlite3

    from sonder_runtime.application.ports.subagents import SubagentStatus
    from sonder_runtime.application.subagents.checkpoint_provenance import (
        CheckpointResumeRefusal,
        ChildResumeRefused,
    )

    child_id, delegation_id = _interrupted_child(storage_config, tmp_path, monkeypatch)
    # The checkpoint was stamped against the original journal identity.
    with sqlite3.connect(tmp_path / "state" / "worker-effects.db") as connection:
        connection.execute("UPDATE effect_journal_identity SET identity='journal-swapped'")

    application = _compose_resume_application(
        storage_config, tmp_path, monkeypatch, fail_after_write=False,
    )
    try:
        with pytest.raises(ChildResumeRefused) as refused:
            _delegate_resume_child(application, tmp_path, child_id, delegation_id)
        assert refused.value.reason is CheckpointResumeRefusal.JOURNAL_IDENTITY_MISMATCH
        assert refused.value.recovery_required is True
        repository = application.delegation_service()._provider._local_service._repository
        child = repository.get(child_id)
        assert child.status is SubagentStatus.FAILED and child.recovery_required
    finally:
        application.close_providers(timeout=10)
    assert (tmp_path / "workspace" / "append.txt").read_text(encoding="utf-8") == "x"
    assert (tmp_path / "runner-trace.log").read_text(encoding="utf-8").splitlines() == ["wrote"]
