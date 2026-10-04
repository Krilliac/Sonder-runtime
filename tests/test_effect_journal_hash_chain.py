"""Tamper-evident hash chain and mock-replay records on the effect journal."""
from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from sonder_runtime.adapters.persistence.sqlite import effect_journal_chain as chain
from sonder_runtime.adapters.persistence.sqlite.effect_journal import SQLiteEffectJournal
from sonder_runtime.application.execution.effect_journal import (
    EffectIntent, EffectJournalError, EffectOutcome, EffectState, bound,
)
from sonder_runtime.application.execution.effect_replay import (
    MISSING_CONTENT, MISSING_DIGEST_MISMATCH, MISSING_NOT_RECORDED, MISSING_SEQUENCE_GAP,
    MISSING_UNRESOLVED, ReplayDivergence, ReplayResponseMissing, RecordedToolResponse,
)
from sonder_runtime.application.execution.worker_bindings import AuthenticatedWorkerBinding
from sonder_runtime.application.tools.gateway_contract import (
    ToolGateway, ToolGatewayRequest, ToolInvocationOutput, ToolPermission, ToolScope,
)

# Frozen copy of the effect-journal schema as it was before the hash chain
# (origin/main 45a3e093).  Migration is tested from exactly this shape.
PRE_CHAIN_DDL = """
CREATE TABLE IF NOT EXISTS effect_journal (
    intent_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    owner_epoch INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    reconciliation TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    state TEXT NOT NULL,
    outcome_digest TEXT NOT NULL DEFAULT '',
    receipt_key TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    UNIQUE(run_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_effect_journal_run_sequence
    ON effect_journal(run_id, sequence);
CREATE INDEX IF NOT EXISTS ix_effect_journal_run_state
    ON effect_journal(run_id, state);
CREATE INDEX IF NOT EXISTS ix_effect_journal_state_run_sequence
    ON effect_journal(state, run_id, sequence);
CREATE TABLE IF NOT EXISTS effect_checkpoint (
    run_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    effect_high_water INTEGER NOT NULL,
    state_digest TEXT NOT NULL,
    state_json TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, generation)
);
CREATE INDEX IF NOT EXISTS ix_effect_checkpoint_latest
    ON effect_checkpoint(run_id, generation DESC);
CREATE TABLE IF NOT EXISTS effect_owner (
    run_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    owner_epoch INTEGER NOT NULL,
    recovery_required INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, worker_id)
);
"""


def _intent(n: int, run_id: str = "run-1", worker: str = "w-1") -> EffectIntent:
    return EffectIntent(f"i-{n}", run_id, worker, f"op-{n}", "/ws", 1, f"k-{n}", f"{n:064x}")


def _done(n: int, response=None, *, worker: str = "w-1") -> EffectOutcome:
    kwargs = {} if response is None else {"response": response}
    return EffectOutcome(f"i-{n}", EffectState.COMPLETED, "d" * 64, f"r-{n}",
                         worker_id=worker, owner_epoch=1, **kwargs)


def _raw(path):
    return sqlite3.connect(str(path))


def _populated(tmp_path, count=4, **kwargs):
    journal = SQLiteEffectJournal(tmp_path / "effects.db", **kwargs)
    for n in range(1, count + 1):
        journal.begin(_intent(n))
        journal.outcome(_done(n, {"output": f"out-{n}"}))
    return journal


# -- chain ---------------------------------------------------------------------


def test_fresh_journal_chains_every_write_and_verifies(tmp_path):
    journal = _populated(tmp_path, count=3)
    journal.begin(_intent(4))
    journal.uncertain("i-4", detail="lost", run_id="run-1")
    result = journal.verify_chain()
    assert result.ok and result.first_break is None and result.breaks == ()
    # anchor + (begin, outcome) x3 + begin + uncertain
    assert result.records_checked == 1 + 6 + 2 and result.legacy_rows == 0
    assert journal.chain_head() == (result.head_seq, result.head_hash) and result.head_seq == 9
    with _raw(journal.database_path) as db:
        rows = db.execute(
            "SELECT chain_seq,kind,intent_id,content,prev_hash,row_hash FROM effect_journal_chain "
            "ORDER BY chain_seq").fetchall()
    prev = chain.GENESIS_HASH
    for seq, kind, intent_id, content, prev_hash, row_hash in rows:
        assert prev_hash == prev
        expected = hashlib.sha256(f"{seq}\n{kind}\n{intent_id}\n{prev_hash}\n{content}".encode()).hexdigest()
        assert row_hash == expected
        prev = row_hash
    assert json.loads(rows[-1][3])["state"] == "uncertain"


def test_reopening_does_not_reanchor_or_rewrite(tmp_path):
    journal = _populated(tmp_path, count=2)
    head = journal.chain_head()
    reopened = SQLiteEffectJournal(tmp_path / "effects.db")
    assert reopened.chain_head() == head and reopened.verify_chain().ok


def test_reopening_a_chained_journal_takes_no_write_lock(tmp_path):
    # Startup composition opens the journal while a peer process may be
    # mid-write; the pre-chain constructor never needed the write lock.
    journal = _populated(tmp_path, count=1)
    peer = sqlite3.connect(str(journal.database_path), timeout=0.1, isolation_level=None)
    try:
        peer.execute("BEGIN IMMEDIATE")
        reopened = SQLiteEffectJournal(journal.database_path)
        assert reopened.get("i-1").state is EffectState.COMPLETED
    finally:
        peer.execute("ROLLBACK")
        peer.close()


def test_verify_detects_edited_journal_row(tmp_path):
    journal = _populated(tmp_path)
    with _raw(journal.database_path) as db:
        db.execute("UPDATE effect_journal SET receipt_key='forged' WHERE intent_id='i-2'")
    result = journal.verify_chain()
    assert not result.ok
    assert result.first_break.intent_id == "i-2" and "edited" in result.first_break.reason
    assert result.first_break.chain_seq == 5  # anchor, i-1 x2, i-2 begin, i-2 outcome


def test_verify_detects_deleted_journal_row(tmp_path):
    journal = _populated(tmp_path)
    with _raw(journal.database_path) as db:
        db.execute("DELETE FROM effect_journal WHERE intent_id='i-3'")
    result = journal.verify_chain()
    assert not result.ok and result.first_break.intent_id == "i-3"
    assert "deleted" in result.first_break.reason


def test_verify_detects_inserted_journal_row(tmp_path):
    journal = _populated(tmp_path)
    with _raw(journal.database_path) as db:
        db.execute(
            "INSERT INTO effect_journal(intent_id,run_id,worker_id,operation_id,scope,owner_epoch,"
            "idempotency_key,request_digest,reconciliation,sequence,state,outcome_digest,receipt_key) "
            "VALUES('i-x','run-1','w-1','op','/ws',1,'k-x','e','manual',5,'completed','d','r')")
    result = journal.verify_chain()
    assert not result.ok and result.first_break.intent_id == "i-x"
    assert "inserted" in result.first_break.reason and result.first_break.chain_seq is None


def test_verify_detects_edited_deleted_and_inserted_chain_records(tmp_path):
    journal = _populated(tmp_path)
    path = journal.database_path
    with _raw(path) as db:
        db.execute("UPDATE effect_journal_chain SET content=replace(content,'r-2','zz') WHERE chain_seq=5")
    result = journal.verify_chain()
    assert not result.ok and result.first_break.chain_seq == 5
    assert "record edited" in result.first_break.reason

    # Re-hashing the edited record moves the break to the next link.
    with _raw(path) as db:
        seq, kind, intent_id, content, prev = db.execute(
            "SELECT chain_seq,kind,intent_id,content,prev_hash FROM effect_journal_chain WHERE chain_seq=5"
        ).fetchone()
        db.execute("UPDATE effect_journal_chain SET row_hash=? WHERE chain_seq=5",
                   (chain.link_hash(seq, kind, intent_id, prev, content),))
    result = journal.verify_chain()
    assert not result.ok and result.first_break.chain_seq == 6
    assert "prev_hash" in result.first_break.reason

    fresh = tmp_path / "second"
    fresh.mkdir()
    journal = _populated(fresh)
    with _raw(journal.database_path) as db:
        db.execute("DELETE FROM effect_journal_chain WHERE chain_seq=3")
    result = journal.verify_chain()
    assert not result.ok and result.first_break.chain_seq == 3 and "missing" in result.first_break.reason

    third = tmp_path / "third"
    third.mkdir()
    journal = _populated(third)
    with _raw(journal.database_path) as db:
        db.execute(
            "INSERT INTO effect_journal_chain(chain_seq,kind,intent_id,content,prev_hash,row_hash) "
            "VALUES(100,'row','i-1','{}',?,?)", ("0" * 64, "0" * 64))
    result = journal.verify_chain()
    assert not result.ok and result.first_break.chain_seq == 10


def test_verify_reports_tampered_response_content(tmp_path):
    journal = _populated(tmp_path, record_response_content=True)
    with _raw(journal.database_path) as db:
        db.execute("UPDATE effect_tool_response SET response_json='{\"output\":\"forged\"}' "
                   "WHERE intent_id='i-2'")
    result = journal.verify_chain()
    assert not result.ok and result.first_break.intent_id == "i-2"
    assert "response content" in result.first_break.reason


def test_every_write_path_keeps_the_chain_consistent(tmp_path):
    class Verifier:
        verifier_id = "test"

        def verify(self, intent):
            from sonder_runtime.application.execution.effect_journal import ReconciliationProof
            return ReconciliationProof(intent.intent_id, intent.operation_id, "rk", "d" * 64,
                                       EffectState.COMPLETED, "test", "ext")

    journal = SQLiteEffectJournal(tmp_path / "effects.db", reconciliation_verifiers={"op-1": Verifier()})
    journal.begin(_intent(1))
    decision = journal.recover("run-1", live_workers={})
    assert decision.action == "reconcile"
    journal.claim_owner("run-1", "w-1", 1)
    assert journal.reconcile("i-1", owner_epoch=1).state is EffectState.COMPLETED
    journal.begin(_intent(2))
    assert journal.outcome_and_checkpoint(_done(2, {"output": 2}), {"s": 1})["effect_high_water"] == 2
    journal.append_checkpoint("run-1", {"s": 2}, worker_id="w-1", owner_epoch=1)
    assert journal.restore_checkpoint("run-1")["generation"] == 1
    result = journal.verify_chain()
    assert result.ok, result.first_break
    # begin, recover->uncertain, reconcile, begin, outcome = 5 row records.
    assert result.records_checked == 6


def test_rolled_back_write_leaves_no_chain_record(tmp_path):
    journal = _populated(tmp_path, count=1)
    head = journal.chain_head()
    with pytest.raises(EffectJournalError):
        journal.outcome(EffectOutcome("i-1", EffectState.FAILED, "c" * 64, "other",
                                      worker_id="w-1", owner_epoch=1))
    journal.outcome(_done(1))  # idempotent replay: no write, no record
    assert journal.chain_head() == head and journal.verify_chain().ok


# -- migration from the pre-chain schema --------------------------------------


def _legacy_db(path):
    with _raw(path) as db:
        db.executescript(PRE_CHAIN_DDL)
        rows = [
            ("l-1", "run-1", "w-1", "op-l1", "/ws", 1, "lk-1", "a" * 64, "manual", 1, "completed", "d" * 64, "lr-1", ""),
            ("l-2", "run-1", "w-1", "op-l2", "/ws", 1, "lk-2", "b" * 64, "manual", 2, "intent", "", "", ""),
            ("l-3", "run-2", "w-2", "op-l3", "/ws", 1, "lk-3", "c" * 64, "manual", 1, "failed", "d" * 64, "lr-3", "boom"),
        ]
        db.executemany("INSERT INTO effect_journal VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        db.execute("INSERT INTO effect_owner VALUES('run-1','w-1',1,0)")
        db.execute("INSERT INTO effect_checkpoint VALUES('run-2',0,1,'x','')")
    return rows


def test_migration_from_pre_chain_schema_pins_legacy_rows_without_rewriting(tmp_path):
    path = tmp_path / "effects.db"
    rows = _legacy_db(path)
    with _raw(path) as db:
        before = db.execute("SELECT rowid,* FROM effect_journal ORDER BY rowid").fetchall()
        checkpoint_before = db.execute("SELECT * FROM effect_checkpoint").fetchall()
    journal = SQLiteEffectJournal(path)
    with _raw(path) as db:
        assert db.execute("SELECT rowid,* FROM effect_journal ORDER BY rowid").fetchall() == before
        assert db.execute("SELECT * FROM effect_checkpoint").fetchall() == checkpoint_before
        anchor = json.loads(db.execute(
            "SELECT content FROM effect_journal_chain WHERE chain_seq=1").fetchone()[0])
    assert anchor["legacy_rows"] == len(rows) and anchor["format"] == chain.CHAIN_FORMAT
    result = journal.verify_chain()
    assert result.ok and result.legacy_rows == 3 and result.records_checked == 1
    # #515 reads are unchanged over legacy rows.
    assert journal.high_water("run-1") == 2 and journal.settled_high_water("run-1") == 1
    assert [r.intent_id for r in journal.effects_since("run-1", 0).records] == ["l-1", "l-2"]
    # A legacy intent settled through the journal joins the chain.
    journal.outcome(EffectOutcome("l-2", EffectState.COMPLETED, "e" * 64, "lr-2",
                                  worker_id="w-1", owner_epoch=1))
    journal.begin(EffectIntent("n-1", "run-1", "w-1", "op-n1", "/ws", 1, "nk-1", "f" * 64))
    assert journal.get("n-1").sequence == 3
    result = journal.verify_chain()
    assert result.ok and result.records_checked == 3
    # Re-opening after the migration does not re-anchor.
    assert SQLiteEffectJournal(path).verify_chain().head_seq == 3


def test_legacy_segment_edit_and_delete_are_detected(tmp_path):
    path = tmp_path / "effects.db"
    _legacy_db(path)
    journal = SQLiteEffectJournal(path)
    with _raw(path) as db:
        db.execute("UPDATE effect_journal SET detail='rewritten' WHERE intent_id='l-3'")
    result = journal.verify_chain()
    assert not result.ok and result.first_break.intent_id == "l-3"
    assert "legacy row" in result.first_break.reason
    with _raw(path) as db:
        db.execute("UPDATE effect_journal SET detail='boom' WHERE intent_id='l-3'")
        db.execute("DELETE FROM effect_journal WHERE intent_id='l-1'")
    result = journal.verify_chain()
    assert not result.ok and result.first_break.intent_id == "l-1" and "deleted" in result.first_break.reason
    with _raw(path) as db:
        db.execute("DELETE FROM effect_journal_legacy WHERE intent_id='l-1'")
    result = journal.verify_chain()
    assert not result.ok and "anchor" in result.first_break.reason


# -- tool responses and mock replay -------------------------------------------


def test_response_digest_uses_gateway_canonical_form_and_content_is_opt_in(tmp_path):
    response = {"output": {"b": 1, "a": [1, 2]}, "success": True}
    canonical = json.dumps(response, sort_keys=True, default=str, separators=(",", ":"))
    for keep, folder in ((False, "digest"), (True, "content")):
        root = tmp_path / folder
        root.mkdir()
        journal = SQLiteEffectJournal(root / "effects.db", record_response_content=keep)
        journal.begin(_intent(1))
        journal.outcome(_done(1, response))
        (row,), _ = journal.recorded_responses("run-1")
        assert row.response_digest == hashlib.sha256(canonical.encode()).hexdigest()
        assert row.response_bytes == len(canonical)
        assert row.response_json == (canonical if keep else None)


def test_unencodable_or_oversize_response_never_blocks_the_outcome(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db", record_response_content=True,
                                  max_response_bytes=16)
    circular: dict = {}
    circular["self"] = circular
    journal.begin(_intent(1))
    assert journal.outcome(_done(1, circular)).state is EffectState.COMPLETED
    journal.begin(_intent(2))
    journal.outcome(_done(2, {"output": "x" * 100}))
    rows, _ = journal.recorded_responses("run-1")
    assert rows[0].response_digest == "" and rows[1].response_digest and rows[1].response_json is None
    assert [m.reason for m in journal.tool_response_replay("run-1").missing] == [
        MISSING_NOT_RECORDED, MISSING_CONTENT]
    assert journal.verify_chain().ok


def test_replay_yields_recorded_responses_in_sequence_order(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db", record_response_content=True)
    for n in (1, 2, 3):
        journal.begin(_intent(n))
    for n in (3, 1, 2):  # outcomes arrive out of order
        journal.outcome(_done(n, {"output": f"out-{n}"}))
    replay = journal.tool_response_replay("run-1", page_size=1)
    assert replay.complete and replay.missing == ()
    assert [(r.sequence, r.response["output"]) for r in replay] == [(1, "out-1"), (2, "out-2"), (3, "out-3")]
    assert all(isinstance(r, RecordedToolResponse) for r in replay)
    assert replay.substitute(_intent(1).request_digest).response == {"output": "out-1"}
    with pytest.raises(ReplayDivergence):
        replay.substitute("not-the-recorded-request")
    assert replay.substitute().sequence == 2 and replay.substitute().sequence == 3
    with pytest.raises(ReplayResponseMissing, match="exhausted"):
        replay.substitute()


def test_replay_reports_every_missing_response_with_its_reason(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db", record_response_content=True)
    for n in range(1, 7):
        journal.begin(_intent(n))
    journal.outcome(_done(1, {"output": 1}))
    journal.outcome(_done(2))                       # no response captured
    journal.outcome(_done(3, {"output": 3}))        # content later tampered
    journal.outcome(_done(5, {"output": 5}))        # row 4 deleted below
    journal.uncertain("i-6", detail="lost", run_id="run-1")
    with _raw(journal.database_path) as db:
        db.execute("UPDATE effect_tool_response SET response_json='{\"output\":4}' WHERE intent_id='i-3'")
        db.execute("DELETE FROM effect_journal WHERE intent_id='i-4'")
    replay = journal.tool_response_replay("run-1")
    assert [(m.sequence, m.reason) for m in replay.missing] == [
        (2, MISSING_NOT_RECORDED), (3, MISSING_DIGEST_MISMATCH),
        (4, MISSING_SEQUENCE_GAP), (6, MISSING_UNRESOLVED),
    ]
    assert [r.sequence for r in replay] == [1, 5] and not replay.complete
    assert replay.substitute().sequence == 1
    with pytest.raises(ReplayResponseMissing, match="sequence 2"):
        replay.substitute()


def test_gateway_records_the_redacted_response_for_replay(tmp_path):
    class Allow:
        def validate(self, *_args):
            return None

        def authorize_request(self, _request):
            return "host:worker"

        def approve(self, _request):
            return True

        def redact(self, _tool, value):
            return value.replace("secret", "[redacted]") if isinstance(value, str) else value

        def record(self, _receipt):
            return None

    outputs = iter([ToolInvocationOutput(True, "wrote secret file"),
                    ToolInvocationOutput(False, "", "E_DENIED", "no secret access")])

    class Invoker:
        def invoke(self, _request):
            return next(outputs)

    allow = Allow()
    gateway = ToolGateway(allow, allow, allow, Invoker(), allow, allow)
    journal = SQLiteEffectJournal(tmp_path / "effects.db", record_response_content=True)
    binding = AuthenticatedWorkerBinding(journal, "run", "worker", 1, str(tmp_path))
    assert binding.recover_before_restart().action == "resume"
    receipts = []
    with bound(binding.binding()):
        for name in ("tool-1", "tool-2"):
            receipts.append(gateway.execute(ToolGatewayRequest(
                name, "write", {"value": name},
                ToolScope("worker", allowed_effects=frozenset({"write_files"}), source="worker"),
                ToolPermission(frozenset({"write_files"}), reconciliation="manual"),
            )))
    replay = journal.tool_response_replay("run")
    assert replay.complete
    first, second = replay.responses
    assert first.response == {"success": True, "output": "wrote [redacted] file",
                               "error_code": "", "error": ""}
    assert second.response["success"] is False and second.response["error_code"] == "E_DENIED"
    assert first.receipt_key == receipts[0].request_id
    assert journal.get(first.intent_id).outcome_digest == receipts[0].result_digest
    assert journal.verify_chain().ok
