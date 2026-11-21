"""Durable recovery fence release after restart; stale host lease reaping."""
from __future__ import annotations

import sqlite3

from sonder_runtime.adapters.persistence.sqlite.effect_journal import SQLiteEffectJournal
from sonder_runtime.adapters.persistence.worker_effect_hosts import WorkerEffectHostLease
from sonder_runtime.application.execution.effect_journal import EffectOutcome, EffectState
from sonder_runtime.application.execution.worker_bindings import AuthenticatedWorkerBinding


def _fenced(path, run="run"):
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT recovery_required FROM effect_owner WHERE run_id=?", (run,),
        ).fetchone()[0]


def test_outcome_after_restart_clears_durable_recovery_fence(tmp_path):
    db = tmp_path / "effects.db"
    journal = SQLiteEffectJournal(db)
    binding = AuthenticatedWorkerBinding(journal, "run", "worker", 1, str(tmp_path)).binding()
    journal.claim_owner("run", "worker", 1)
    intent = binding.begin_request(operation_id="op", idempotency_key="k", request_digest="a" * 64)
    outcome = EffectOutcome(intent.intent_id, EffectState.COMPLETED, "b" * 64, "receipt", "", "worker", 1)
    journal.outcome(outcome)
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE effect_owner SET recovery_required=1 WHERE run_id='run'")
    assert _fenced(db) == 1
    restarted = SQLiteEffectJournal(db)  # empty in-memory latch
    assert restarted.outcome(outcome).state is EffectState.COMPLETED
    assert _fenced(db) == 0


def test_live_peers_reaps_stale_lease_file(tmp_path):
    directory = tmp_path / "hosts"
    lease = WorkerEffectHostLease(directory)
    stale = directory / ("host-1-" + "a" * 32 + ".lock")
    stale.write_bytes(b"0")
    assert lease.live_peers() == 0
    assert not stale.exists()
