"""Concrete DATA-003/DATA-004 SQLite qualification at the production repository seam."""

from __future__ import annotations

import multiprocessing as mp
import os
import sqlite3
from contextlib import contextmanager

from sonder_runtime.adapters.persistence.sqlite.cas import SQLiteOutboxCASRepository
from sonder_runtime.adapters.persistence.sqlite.graph import (
    build_sqlite_persistence_facade,
)
from sonder_runtime.application.persistence.outbox_cas import (
    OutboxEvent,
    TransactionNeutralRecord,
)

DOMAINS = ("memory", "automation", "operations", "selfmod", "training", "updates")


def _record(domain: str, aggregate: str, revision: int, suffix: str) -> tuple[TransactionNeutralRecord, OutboxEvent]:
    record = TransactionNeutralRecord(aggregate, revision, {"domain": domain, "suffix": suffix})
    event = OutboxEvent(f"{domain}-{suffix}", aggregate, "qualified", revision, {"suffix": suffix}, "2026-09-24T00:00:00Z")
    return record, event


def _contending_writer(path: str, domain: str, barrier, result_queue) -> None:
    repository = SQLiteOutboxCASRepository(path)
    record, event = _record(domain, "shared", 0, str(os.getpid()))
    barrier.wait(timeout=10)
    try:
        result_queue.put((os.getpid(), repository.append(record, event, expected_revision=-1) is not None, None))
    except Exception as exc:  # noqa: BLE001 - child diagnostics must reach parent
        result_queue.put((os.getpid(), False, type(exc).__name__ + ": " + str(exc)))


def _crash_during_repository_append(path: str, domain: str) -> None:
    repository = SQLiteOutboxCASRepository(path)
    original_connect = repository._connect

    @contextmanager
    def traced_connect():
        with original_connect() as connection:
            def fail_before_outbox_insert(statement: str) -> None:
                if "INSERT INTO persistence_outbox_events" in statement:
                    os._exit(17)
            connection.set_trace_callback(fail_before_outbox_insert)
            yield connection

    repository._connect = traced_connect
    record, event = _record(domain, "crash", 0, "crash")
    repository.append(record, event, expected_revision=-1)


def _fresh_reader(path: str, result_queue) -> None:
    repository = SQLiteOutboxCASRepository(path)
    record = repository.get("shared")
    result_queue.put((None if record is None else record.revision, len(repository.outbox())))


def _run_processes(target, args, *, expected_exitcodes=None):
    context = mp.get_context("spawn")
    processes = [context.Process(target=target, args=item) for item in args]
    expected = expected_exitcodes or [0] * len(processes)
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(20)
            if process.is_alive():
                process.terminate()
                process.join(5)
            assert process.exitcode == expected[processes.index(process)], (process.pid, process.exitcode)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(5)
    return processes


def test_every_graph_repository_has_one_multiprocess_cas_winner_and_one_event(tmp_path):
    facade = build_sqlite_persistence_facade(tmp_path)
    context = mp.get_context("spawn")
    for domain in DOMAINS:
        path = str(facade.registry.binding_for(domain).path)
        barrier = context.Barrier(4)
        queue = context.Queue()
        processes = _run_processes(
            _contending_writer,
            [(path, domain, barrier, queue)] * 4,
        )
        results = [queue.get(timeout=5) for _ in processes]
        assert sum(item[1] for item in results) == 1, (domain, results)
        assert all(item[2] is None for item in results), (domain, results)
        reader_queue = context.Queue()
        _run_processes(_fresh_reader, [(path, reader_queue)])
        assert reader_queue.get(timeout=5) == (0, 1)


def test_stale_revision_loser_leaves_no_record_or_outbox_event_after_reopen(tmp_path):
    facade = build_sqlite_persistence_facade(tmp_path)
    for domain in DOMAINS:
        path = facade.registry.binding_for(domain).path
        repository = SQLiteOutboxCASRepository(path)
        first, first_event = _record(domain, "aggregate", 0, "first")
        assert repository.append(first, first_event, expected_revision=-1) is not None
        stale, stale_event = _record(domain, "aggregate", 0, "stale")
        assert repository.append(stale, stale_event, expected_revision=-1) is None
        reopened = SQLiteOutboxCASRepository(path)
        assert reopened.get("aggregate").payload == {"domain": domain, "suffix": "first"}
        assert [event.event_id for event in reopened.outbox()] == [f"{domain}-first"]


def test_process_crash_between_state_insert_and_outbox_append_rolls_back_atomically(tmp_path):
    facade = build_sqlite_persistence_facade(tmp_path)
    for domain in DOMAINS:
        path = str(facade.registry.binding_for(domain).path)
        _run_processes(
            _crash_during_repository_append,
            [(path, domain)],
            expected_exitcodes=[17],
        )
        reopened = SQLiteOutboxCASRepository(path)
        assert reopened.get("crash") is None
        assert reopened.outbox() == ()
        with sqlite3.connect(path) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
