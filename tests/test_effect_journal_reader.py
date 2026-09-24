"""Read-only effect journal API for binding checkpoints to a journal position.

Consumed by the issue #510 worker-registry checkpoint saga: a checkpoint
records ``settled_high_water`` and a resume reads ``effects_since`` that
position to classify settled versus unresolved effects.
"""
from __future__ import annotations

import sqlite3

import pytest

from sonder_runtime.adapters.persistence.sqlite.effect_journal import SQLiteEffectJournal
from sonder_runtime.application.execution.effect_journal import (
    EffectJournalError, EffectJournalPage, EffectJournalReader, EffectState,
)
from sonder_runtime.application.execution.worker_bindings import (
    AuthenticatedWorkerBinding, journaled_effect,
)


def _begin(journal, run, op, worker="worker", epoch=1):
    return AuthenticatedWorkerBinding(journal, run, worker, epoch, "/ws").binding().begin_request(
        operation_id=op, idempotency_key=f"key-{op}", request_digest="a" * 64,
    )


def _complete(journal, intent, worker="worker", epoch=1, success=True):
    AuthenticatedWorkerBinding(journal, intent.run_id, worker, epoch, "/ws").binding().complete(
        intent, outcome_digest="b" * 64, receipt_key=f"receipt-{intent.operation_id}",
        success=success,
    )


def _snapshot(path):
    with sqlite3.connect(path) as connection:
        return (
            connection.execute("SELECT * FROM effect_journal ORDER BY sequence").fetchall(),
            connection.execute("SELECT * FROM effect_owner ORDER BY run_id, worker_id").fetchall(),
            connection.execute("SELECT * FROM effect_checkpoint").fetchall(),
        )


def test_reader_satisfies_protocol_and_empty_run_is_zero(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    reader: EffectJournalReader = journal
    assert reader.settled_high_water("run") == 0
    page = reader.effects_since("run", 0)
    assert isinstance(page, EffectJournalPage)
    assert page.records == () and page.high_water == 0
    assert page.settled_high_water == 0 and page.truncated is False


def test_settled_high_water_stops_before_first_unresolved_intent(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    first = _begin(journal, "run", "a")
    _complete(journal, first)
    second = _begin(journal, "run", "b")          # still in flight
    third = _begin(journal, "run", "c")
    _complete(journal, third, success=False)      # FAILED is settled
    assert (first.sequence, second.sequence, third.sequence) == (1, 2, 3)

    assert journal.settled_high_water("run") == 1
    assert journal.high_water("run") == 3
    page = journal.effects_since("run", 0)
    assert [r.sequence for r in page.records] == [1, 2, 3]
    assert [r.state for r in page.records] == [
        EffectState.COMPLETED, EffectState.INTENT, EffectState.FAILED,
    ]
    assert [r.intent_id for r in page.unresolved] == [second.intent_id]
    assert (page.high_water, page.settled_high_water) == (3, 1)

    journal.uncertain(second.intent_id, detail="crash")
    assert journal.settled_high_water("run") == 1  # UNCERTAIN is not settled
    fourth = _begin(journal, "other", "d")
    _complete(journal, fourth)
    assert journal.settled_high_water("other") == 1
    assert journal.settled_high_water("run") == 1  # runs are independent


def test_settled_high_water_advances_when_gap_is_resolved(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    _complete(journal, _begin(journal, "run", "a"))
    in_flight = _begin(journal, "run", "b")
    _complete(journal, _begin(journal, "run", "c"))
    assert journal.settled_high_water("run") == 1
    _complete(journal, in_flight)
    assert journal.settled_high_water("run") == 3


def test_first_intent_unresolved_gives_zero(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    _begin(journal, "run", "a")
    assert journal.settled_high_water("run") == 0
    assert journal.high_water("run") == 1


def test_effects_since_checkpoint_returns_only_newer_records_and_receipts(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    binding = AuthenticatedWorkerBinding(journal, "run", "worker", 1, "/ws")
    journaled_effect(
        binding, operation_id="one", idempotency_key="k1", request={},
        invoke=lambda: "r1", receipt_key="receipt-1",
    )
    checkpoint = journal.restore_checkpoint("run")
    assert checkpoint["effect_high_water"] == 1
    journaled_effect(
        binding, operation_id="two", idempotency_key="k2", request={},
        invoke=lambda: "r2", receipt_key="receipt-2",
    )
    orphan = binding.binding().begin_request(
        operation_id="three", idempotency_key="k3", request_digest="c" * 64,
    )

    page = journal.effects_since("run", checkpoint["effect_high_water"])
    assert [r.idempotency_key for r in page.records] == ["k2", "k3"]
    settled = {r.idempotency_key: r.receipt_key for r in page.records
               if r.state is EffectState.COMPLETED}
    assert settled == {"k2": "receipt-2"}
    assert [r.intent_id for r in page.unresolved] == [orphan.intent_id]
    assert (page.high_water, page.settled_high_water) == (3, 2)


def test_effects_since_pages_and_filters_by_worker(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    for index in range(5):
        worker = "w1" if index % 2 == 0 else "w2"
        _complete(journal, _begin(journal, "run", f"op{index}", worker=worker), worker=worker)

    first = journal.effects_since("run", 0, limit=2)
    assert [r.sequence for r in first.records] == [1, 2] and first.truncated
    second = journal.effects_since("run", first.records[-1].sequence, limit=2)
    assert [r.sequence for r in second.records] == [3, 4] and second.truncated
    last = journal.effects_since("run", second.records[-1].sequence, limit=2)
    assert [r.sequence for r in last.records] == [5] and not last.truncated

    only_w1 = journal.effects_since("run", 0, worker_id="w1")
    assert [r.sequence for r in only_w1.records] == [1, 3, 5]
    # High-water values describe the whole run, not the filtered page.
    assert (only_w1.high_water, only_w1.settled_high_water) == (5, 5)


def test_reader_is_read_only_even_while_recovery_is_required(tmp_path):
    path = tmp_path / "effects.db"
    journal = SQLiteEffectJournal(path)
    _complete(journal, _begin(journal, "run", "a"))
    _begin(journal, "run", "b")
    with pytest.raises(EffectJournalError):
        AuthenticatedWorkerBinding(journal, "run", "worker", 2, "/ws").recover_before_restart()
    before = _snapshot(path)
    assert any(row[3] == 1 for row in before[1])  # recovery_required fence set

    journal.settled_high_water("run")
    journal.effects_since("run", 0)
    journal.effects_since("never-seen", 0, worker_id="nobody")
    journal.settled_high_water("never-seen")

    assert _snapshot(path) == before


@pytest.mark.parametrize("call", [
    lambda j: j.settled_high_water(""),
    lambda j: j.effects_since("", 0),
    lambda j: j.effects_since("run", -1),
    lambda j: j.effects_since("run", True),
    lambda j: j.effects_since("run", 0, limit=0),
    lambda j: j.effects_since("run", 0, limit=10_001),
    lambda j: j.effects_since("run", 0, worker_id=" "),
])
def test_reader_rejects_invalid_arguments(tmp_path, call):
    with pytest.raises(EffectJournalError):
        call(SQLiteEffectJournal(tmp_path / "effects.db"))
