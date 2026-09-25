"""Host-stamped child checkpoint provenance bound to the effect journal (#515).

These tests qualify the SQLite/codec half of the child-checkpoint saga: a
checkpoint written through the durable child CAS carries an immutable record
binding its sequence, state digest and cursor to one journal identity, run,
worker, owner epoch and settled journal position.  The pure validator decides
whether a checkpoint may be used as a resume point.  It never executes work.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.persistence.durable_continuation import (
    SQLiteDurableContinuationRepository,
    SQLiteJournalProvenanceSource,
)
from sonder_runtime.adapters.persistence.postgres_continuation import (
    _apply as postgres_apply,
)
from sonder_runtime.adapters.persistence.postgres_continuation import (
    decode_child_snapshot,
    encode_child_snapshot,
)
from sonder_runtime.adapters.persistence.sqlite.effect_journal import (
    SQLiteEffectJournal,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.execution.effect_journal import (
    EffectIntent,
    EffectJournalPage,
    EffectState,
)
from sonder_runtime.application.execution.worker_bindings import (
    AuthenticatedWorkerBinding,
    journaled_effect,
)
from sonder_runtime.application.ports.continuation_mutations import (
    ContinuationCommitAmbiguous,
    canonical,
    prepare_call,
)
from sonder_runtime.application.ports.subagents import (
    InvalidSubagentRequest,
    SubagentBudget,
    SubagentRequest,
    SubagentStatus,
)
from sonder_runtime.application.subagents.checkpoint_provenance import (
    CheckpointResumeRefusal as Refusal,
)
from sonder_runtime.application.subagents.checkpoint_provenance import (
    JournalProvenanceStamp,
    ProvenanceBinding,
    validate_checkpoint_resume,
)
from sonder_runtime.application.subagents.continuable import (
    CheckpointProvenance,
    ContinuableCheckpoint,
    checkpoint_state_digest,
)
from sonder_runtime.application.subagents.continuation_codec import (
    decode_call,
    session_from_data,
)
from sonder_runtime.application.subagents.durable_continuation import (
    ChildSessionLineage,
    DurableChildSession,
    DurableContinuationService,
)

RUN = "subagent:child-1"
WORKER = "subagent:worker-1"
CHILD = "child-1"
CRASH_EXIT = 81


def _env(root: Path) -> SimpleNamespace:
    root.mkdir(parents=True, exist_ok=True)
    journal = SQLiteEffectJournal(root / "effects.db")
    source = SQLiteJournalProvenanceSource(journal, create_identity=True)
    journal.claim_owner(RUN, WORKER, 1)
    return SimpleNamespace(
        root=root,
        journal=journal,
        source=source,
        repo=SQLiteDurableContinuationRepository(root / "children.db"),
        binding=AuthenticatedWorkerBinding(journal, RUN, WORKER, 1, "local-subagents"),
        stamp=JournalProvenanceStamp(
            source, lambda _subject: ProvenanceBinding(RUN, WORKER, 1),
        ),
    )


def _effect(binding: AuthenticatedWorkerBinding, key: str) -> str:
    return journaled_effect(
        binding, operation_id=f"op-{key}", idempotency_key=key,
        request={"key": key}, invoke=lambda: f"done-{key}",
        receipt_key=f"receipt:{key}",
    )


def _request(child_id: str = CHILD) -> SubagentRequest:
    return SubagentRequest("session-parent", "bounded work", SubagentBudget(max_steps=8), child_id)


def _context(name: str):
    return local_owner_context(correlation_id=name)


def _run_child(env, runner, *, hook=True, child_id: str = CHILD):
    service = DurableContinuationService(
        env.repo, checkpoint_provenance=env.stamp if hook else None,
    )
    result = service.spawn(_request(child_id), _context(child_id), runner).result(10)
    assert service.close(2)
    return result


def _stamped(source, child_id, sequence, state, cursor, position, *,
             run_id=RUN, worker_id=WORKER, epoch=1) -> ContinuableCheckpoint:
    identity = source.position(run_id, worker_id).journal_identity
    provenance = CheckpointProvenance.stamp(
        child_id=child_id, sequence=sequence,
        state_digest=checkpoint_state_digest(state), cursor=cursor,
        journal_identity=identity, run_id=run_id, worker_id=worker_id,
        owner_epoch=epoch, settled_position=position,
    )
    return ContinuableCheckpoint(child_id, sequence, state, cursor, provenance)


def _validate(checkpoint, journal, *, run_id=RUN, worker_id=WORKER, epoch=2, **kwargs):
    return validate_checkpoint_resume(
        checkpoint, journal, run_id=run_id, worker_id=worker_id,
        resumer_owner_epoch=epoch, **kwargs,
    )


def _two_effects_then_checkpoint(env, *, fail_after_save: bool = True):
    def runner(_state, save, _control):
        _effect(env.binding, "a")
        _effect(env.binding, "b")
        save({"step": 2, "notes": ["a", "b"]}, "cursor-2")
        if fail_after_save:
            raise RuntimeError("host stopped after checkpoint")
        return "finished"

    return _run_child(env, runner)


# --- round trip ---------------------------------------------------------


def test_provenance_round_trips_sqlite_and_codec_with_identical_digests(tmp_path):
    env = _env(tmp_path)
    assert _two_effects_then_checkpoint(env).status is SubagentStatus.FAILED
    record = env.repo.get(CHILD)
    checkpoint = record.checkpoint
    provenance = checkpoint.provenance
    assert provenance is not None and not checkpoint.provenance_absent
    assert (provenance.child_id, provenance.sequence, provenance.cursor) == (CHILD, 0, "cursor-2")
    assert (provenance.run_id, provenance.worker_id, provenance.owner_epoch) == (RUN, WORKER, 1)
    assert provenance.settled_position == 2 == env.journal.settled_high_water(RUN)
    assert provenance.journal_identity == env.source.position(RUN, WORKER).journal_identity
    assert provenance.state_digest == checkpoint_state_digest(checkpoint.state)
    assert provenance.digest_valid

    reopened = SQLiteDurableContinuationRepository(tmp_path / "children.db").get(CHILD)
    assert reopened == record
    again = reopened.checkpoint.provenance
    assert (again.record_digest, again.state_digest) == (provenance.record_digest, provenance.state_digest)
    with sqlite3.connect(tmp_path / "children.db") as raw:
        stored = raw.execute(
            "SELECT record_digest,state_digest FROM child_checkpoint_provenance "
            "WHERE child_id=? AND sequence=0", (CHILD,),
        ).fetchone()
    assert stored == (provenance.record_digest, provenance.state_digest)

    # Codec: session snapshot and the prepared save_checkpoint mutation.
    assert session_from_data(json.loads(canonical(asdict(record)))) == record
    prepared = prepare_call("save_checkpoint", checkpoint, expected_sequence=-1)
    args, kwargs = decode_call(prepared)
    assert args[0] == checkpoint and args[0].provenance == provenance
    assert prepare_call("save_checkpoint", *args, operation_id=prepared.operation_id, **kwargs) == prepared


def test_provenance_record_is_immutable_in_sqlite(tmp_path):
    env = _env(tmp_path)
    _two_effects_then_checkpoint(env)
    with sqlite3.connect(tmp_path / "children.db") as raw:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            raw.execute("UPDATE child_checkpoint_provenance SET settled_position=0")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            raw.execute("DELETE FROM child_checkpoint_provenance")


# --- atomicity with the child CAS ---------------------------------------


def test_provenance_insert_and_child_cas_share_one_transaction(tmp_path):
    env = _env(tmp_path)
    env.repo.create(DurableChildSession(_request(), ChildSessionLineage("session-parent")))
    checkpoint = _stamped(env.source, CHILD, 0, {"step": 1}, "c1", 0)
    with sqlite3.connect(tmp_path / "children.db") as raw:
        # Fires only when the provenance row is already visible to the CAS
        # statement, i.e. between provenance insert and CAS commit.
        raw.execute(
            "CREATE TRIGGER inject_cas_failure BEFORE UPDATE OF checkpoint_sequence "
            "ON durable_child_session WHEN EXISTS (SELECT 1 FROM child_checkpoint_provenance "
            "WHERE child_id=NEW.child_id AND sequence=NEW.checkpoint_sequence) "
            "BEGIN SELECT RAISE(ABORT,'injected CAS failure'); END"
        )
    prepared = prepare_call("save_checkpoint", checkpoint, expected_sequence=-1)
    with pytest.raises(ContinuationCommitAmbiguous):
        env.repo.mutate(prepared)

    reopened = SQLiteDurableContinuationRepository(tmp_path / "children.db")
    assert reopened.get(CHILD).checkpoint is None
    assert reopened.unresolved_mutation(CHILD) == prepared  # intent stays fenced
    with sqlite3.connect(tmp_path / "children.db") as raw:
        assert raw.execute("SELECT COUNT(*) FROM child_checkpoint_provenance").fetchone() == (0,)
        raw.execute("DROP TRIGGER inject_cas_failure")
    # Control: replaying the exact retained intent without the fault commits
    # provenance and CAS together.
    fresh = SQLiteDurableContinuationRepository(tmp_path / "children.db")
    assert fresh.mutate(prepared).disposition == "applied"
    assert fresh.get(CHILD).checkpoint == checkpoint
    with sqlite3.connect(tmp_path / "children.db") as raw:
        assert raw.execute("SELECT COUNT(*) FROM child_checkpoint_provenance").fetchone() == (1,)


def test_store_refuses_provenance_for_a_different_subject(tmp_path):
    env = _env(tmp_path)
    env.repo.create(DurableChildSession(_request(), ChildSessionLineage("session-parent")))
    good = _stamped(env.source, CHILD, 0, {"step": 1}, "c1", 0)
    forged = ContinuableCheckpoint(CHILD, 0, {"step": 999}, "c1", good.provenance)
    with pytest.raises(InvalidSubagentRequest, match="provenance"):
        env.repo.save_checkpoint(forged, expected_sequence=-1)
    assert env.repo.get(CHILD).checkpoint is None
    with sqlite3.connect(tmp_path / "children.db") as raw:
        assert raw.execute("SELECT COUNT(*) FROM child_checkpoint_provenance").fetchone() == (0,)


# --- provenance-absent rows ---------------------------------------------

_PRE_PROVENANCE_DDL = """
CREATE TABLE continuation_intent(position INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT UNIQUE NOT NULL, child_id TEXT NOT NULL, kind TEXT NOT NULL, digest TEXT NOT NULL, payload BLOB NOT NULL);
CREATE TABLE continuation_receipt(operation_id TEXT PRIMARY KEY REFERENCES continuation_intent(operation_id), disposition TEXT NOT NULL, result BLOB NOT NULL, revision INTEGER);
CREATE TABLE durable_child_session (
    child_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL, ancestors_json TEXT NOT NULL,
    prompt TEXT NOT NULL, budget_json TEXT NOT NULL, metadata_json TEXT NOT NULL,
    status TEXT NOT NULL, checkpoint_sequence INTEGER, checkpoint_state_json TEXT,
    checkpoint_cursor TEXT, revision INTEGER NOT NULL, usage_json TEXT NOT NULL,
    result_json TEXT, recovery_required INTEGER NOT NULL,
    cancellation_requested INTEGER NOT NULL, cancellation_reason TEXT
);
"""


def test_unhooked_and_pre_provenance_rows_read_as_absent_and_refuse(tmp_path):
    env = _env(tmp_path / "hookless")

    def runner(_state, save, _control):
        _effect(env.binding, "a")
        save({"step": 1}, "c1")
        raise RuntimeError("stop")

    assert _run_child(env, runner, hook=False).status is SubagentStatus.FAILED
    unhooked = env.repo.get(CHILD).checkpoint
    assert unhooked.provenance is None and unhooked.provenance_absent
    env.journal.claim_owner(RUN, WORKER, 2)
    decision = _validate(unhooked, env.source)
    assert not decision.allowed and decision.reason is Refusal.PROVENANCE_ABSENT
    assert _validate(None, env.source).reason is Refusal.NO_CHECKPOINT

    legacy_path = tmp_path / "legacy" / "children.db"
    legacy_path.parent.mkdir()
    with sqlite3.connect(legacy_path) as raw:
        raw.executescript(_PRE_PROVENANCE_DDL)
        raw.execute(
            "INSERT INTO durable_child_session VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (CHILD, "session-parent", "[]", "legacy", json.dumps({"max_steps": 8}), "[]",
             "failed", 3, json.dumps({"legacy": True}), "legacy-cursor", 7,
             json.dumps({"steps": 4, "output_tokens": 0, "wall_seconds": 1.0}),
             None, 1, 0, None),
        )
    legacy = SQLiteDurableContinuationRepository(legacy_path).get(CHILD)
    assert legacy.checkpoint == ContinuableCheckpoint(CHILD, 3, {"legacy": True}, "legacy-cursor")
    assert legacy.checkpoint.provenance_absent
    refused = _validate(legacy.checkpoint, env.source)
    assert not refused.allowed and refused.reason is Refusal.PROVENANCE_ABSENT
    assert refused.receipts == {} and refused.later_receipts == {}


# --- validator refusals -------------------------------------------------


class _PageFault:
    """Wrap a real journal source and corrupt its bounded pages."""

    def __init__(self, inner, *, drop_sequence=None, lie_has_more=False, inject=None):
        self._inner = inner
        self._drop, self._lie, self._inject = drop_sequence, lie_has_more, inject

    def position(self, run_id, worker_id):
        return self._inner.position(run_id, worker_id)

    def settled_high_water(self, run_id):
        return self._inner.settled_high_water(run_id)

    def effects_since(self, run_id, after_sequence, *, limit=100, worker_id=None):
        page = self._inner.effects_since(run_id, after_sequence, limit=limit, worker_id=worker_id)
        records = tuple(r for r in page.records if r.sequence != self._drop)
        high_water, truncated = page.high_water, page.truncated
        if self._inject is not None and not page.truncated:
            records += (self._inject,)
            high_water = self._inject.sequence
        if self._lie:
            truncated = False
        return EffectJournalPage(run_id, after_sequence, records, high_water,
                                 page.settled_high_water, truncated)


def _refusal_env(tmp_path, name):
    env = _env(tmp_path / name)
    _two_effects_then_checkpoint(env)
    env.journal.claim_owner(RUN, WORKER, 2)
    return env, env.repo.get(CHILD).checkpoint


def test_validator_refusals_each_have_a_distinct_typed_reason(tmp_path):
    reasons = {}

    env, checkpoint = _refusal_env(tmp_path, "swap")
    other = _env(tmp_path / "other-journal")
    _effect(other.binding, "a")
    _effect(other.binding, "b")
    other.journal.claim_owner(RUN, WORKER, 2)
    reasons["journal_swapped"] = _validate(checkpoint, other.source).reason
    (env.root / "effects.db").unlink()
    reasons["journal_missing"] = _validate(checkpoint, env.source).reason
    assert _validate(checkpoint, None).reason is Refusal.JOURNAL_MISSING

    env, checkpoint = _refusal_env(tmp_path, "run")
    reasons["run_mismatch"] = _validate(checkpoint, env.source, run_id="subagent:child-2").reason
    reasons["worker_mismatch"] = _validate(checkpoint, env.source, worker_id="subagent:worker-9").reason

    env, checkpoint = _refusal_env(tmp_path, "epoch")
    env.journal.claim_owner(RUN, WORKER, 3)
    reasons["stale_owner_epoch"] = _validate(checkpoint, env.source, epoch=2).reason
    assert _validate(checkpoint, env.source, epoch=3).allowed  # control: current owner

    env, checkpoint = _refusal_env(tmp_path, "ahead")
    ahead = _stamped(env.source, CHILD, 0, checkpoint.state, checkpoint.cursor, 5)
    reasons["position_ahead"] = _validate(ahead, env.source).reason

    env, checkpoint = _refusal_env(tmp_path, "unresolved-below")
    successor = AuthenticatedWorkerBinding(env.journal, RUN, WORKER, 2, "local-subagents")
    open_intent = successor.binding().begin_request(
        operation_id="op-c", idempotency_key="c", request_digest="d" * 64,
    )
    covering = _stamped(env.source, CHILD, 0, checkpoint.state, checkpoint.cursor, 3)
    reasons["unresolved_at_or_below"] = _validate(covering, env.source).reason
    reasons["unresolved_after"] = _validate(checkpoint, env.source).reason
    assert open_intent.sequence == 3

    env, checkpoint = _refusal_env(tmp_path, "overlap")
    overlap = EffectIntent(
        f"{RUN}:op-a-retry", RUN, WORKER, "op-a-retry", "local-subagents", 2,
        "a", "e" * 64, sequence=3,
    )
    reasons["overlapping_key"] = _validate(checkpoint, _PageFault(env.source, inject=overlap)).reason

    env, checkpoint = _refusal_env(tmp_path, "pages")
    reasons["has_more_lie"] = _validate(
        checkpoint, _PageFault(env.source, lie_has_more=True), page_limit=1,
    ).reason
    reasons["missing_sequence"] = _validate(checkpoint, _PageFault(env.source, drop_sequence=1)).reason
    assert _validate(checkpoint, env.source, page_limit=1).allowed  # control: honest paging
    reasons["page_budget"] = _validate(checkpoint, env.source, page_limit=1, max_pages=1).reason

    env, checkpoint = _refusal_env(tmp_path, "tamper")
    with sqlite3.connect(env.root / "children.db") as raw:
        raw.execute(
            "UPDATE durable_child_session SET checkpoint_state_json=? WHERE child_id=?",
            (json.dumps({"step": 2, "notes": ["a", "b", "forged"]}), CHILD),
        )
    tampered = SQLiteDurableContinuationRepository(env.root / "children.db").get(CHILD).checkpoint
    reasons["state_digest"] = _validate(tampered, env.source).reason
    bad_digest = replace(checkpoint.provenance, record_digest="0" * 64)
    reasons["record_digest"] = _validate(
        ContinuableCheckpoint(CHILD, 0, checkpoint.state, checkpoint.cursor, bad_digest), env.source,
    ).reason

    assert reasons == {
        "journal_swapped": Refusal.JOURNAL_IDENTITY_MISMATCH,
        "journal_missing": Refusal.JOURNAL_MISSING,
        "run_mismatch": Refusal.RUN_MISMATCH,
        "worker_mismatch": Refusal.WORKER_MISMATCH,
        "stale_owner_epoch": Refusal.STALE_OWNER_EPOCH,
        "position_ahead": Refusal.POSITION_AHEAD_OF_JOURNAL,
        "unresolved_at_or_below": Refusal.UNRESOLVED_AT_OR_BELOW_POSITION,
        "unresolved_after": Refusal.UNRESOLVED_AFTER_POSITION,
        "overlapping_key": Refusal.OVERLAPPING_UNRESOLVED_INTENT,
        "has_more_lie": Refusal.INCOMPLETE_JOURNAL_PAGE,
        "missing_sequence": Refusal.INCOMPLETE_JOURNAL_PAGE,
        "page_budget": Refusal.JOURNAL_PAGE_BUDGET_EXHAUSTED,
        "state_digest": Refusal.STATE_DIGEST_MISMATCH,
        "record_digest": Refusal.PROVENANCE_DIGEST_MISMATCH,
    }


def test_checkpoint_written_by_a_superseded_owner_is_refused(tmp_path):
    env = _env(tmp_path)
    _effect(env.binding, "a")
    stale = _stamped(env.source, CHILD, 0, {"s": 1}, None, 1, epoch=1)
    env.journal.claim_owner(RUN, WORKER, 2)
    newer = AuthenticatedWorkerBinding(env.journal, RUN, WORKER, 2, "local-subagents")
    _effect(newer, "b")
    covering_newer = _stamped(env.source, CHILD, 0, {"s": 1}, None, 2, epoch=1)
    assert _validate(stale, env.source).allowed
    assert _validate(covering_newer, env.source).reason is Refusal.OWNER_SUPERSEDED
    future = _stamped(env.source, CHILD, 0, {"s": 1}, None, 1, epoch=5)
    assert _validate(future, env.source).reason is Refusal.OWNER_EPOCH_AHEAD


def test_stamp_refuses_a_stale_or_unclaimed_owner(tmp_path):
    env = _env(tmp_path)
    env.journal.claim_owner(RUN, WORKER, 2)

    def runner(_state, save, _control):
        save({"step": 1}, None)
        return "unreachable"

    result = _run_child(env, runner)
    assert result.status is SubagentStatus.FAILED
    assert env.repo.get(CHILD).checkpoint is None


def test_stamp_binds_the_settled_prefix_not_the_high_water(tmp_path):
    """The #515 blocker shape: an outer run intent stays open around inner effects."""
    env = _env(tmp_path)
    outer = env.binding.binding().begin_request(
        operation_id=f"subagent-run:{CHILD}", idempotency_key="outer", request_digest="f" * 64,
    )

    def runner(_state, save, _control):
        _effect(env.binding, "inner")
        save({"after": "inner"}, None)
        raise RuntimeError("stop")

    _run_child(env, runner)
    checkpoint = env.repo.get(CHILD).checkpoint
    assert (outer.sequence, env.journal.high_water(RUN)) == (1, 2)
    assert checkpoint.provenance.settled_position == 0
    decision = _validate(checkpoint, env.source, epoch=1)
    assert decision.reason is Refusal.UNRESOLVED_AFTER_POSITION


# --- validator accept ----------------------------------------------------


def test_validator_accepts_settled_prefix_and_lists_receipts_by_idempotency_key(tmp_path):
    env = _env(tmp_path)
    _two_effects_then_checkpoint(env)
    _effect(env.binding, "c")  # settled after the checkpoint position
    checkpoint = env.repo.get(CHILD).checkpoint

    current = _validate(checkpoint, env.source, epoch=1)
    assert current.allowed and current.reason is None
    env.journal.claim_owner(RUN, WORKER, 2)
    decision = _validate(checkpoint, env.source, epoch=2)
    assert decision.allowed and decision.reason is None
    assert (decision.child_id, decision.sequence, decision.settled_position) == (CHILD, 0, 2)
    assert tuple(decision.receipts) == ("a", "b")
    assert decision.receipts["a"].receipt_key == "receipt:a"
    assert decision.receipts["b"].state is EffectState.COMPLETED
    assert tuple(decision.later_receipts) == ("c",)
    assert decision.later_receipts["c"].sequence == 3
    assert decision.resume_state == checkpoint.state
    with pytest.raises(TypeError):
        decision.receipts["z"] = decision.receipts["a"]


# --- host-stamped only --------------------------------------------------


def test_caller_supplied_provenance_is_ignored_and_cannot_be_passed(tmp_path):
    env = _env(tmp_path)
    forged = {
        "journal_identity": "attacker", "run_id": RUN, "worker_id": WORKER,
        "owner_epoch": 99, "settled_position": 1000,
    }
    observed = {}

    def runner(_state, save, _control):
        with pytest.raises(TypeError):
            save({"x": 1}, None, provenance=forged)
        saved = save({"x": 1, "provenance": forged}, "c")
        observed["saved"] = saved
        raise RuntimeError("stop")

    _run_child(env, runner)
    checkpoint = env.repo.get(CHILD).checkpoint
    assert checkpoint == observed["saved"]
    assert checkpoint.state["provenance"] == forged  # plain data only
    host = checkpoint.provenance
    assert host.journal_identity == env.source.position(RUN, WORKER).journal_identity
    assert (host.owner_epoch, host.settled_position) == (1, 0)

    hookless = _env(tmp_path / "hookless")

    def runner2(_state, save, _control):
        save({"provenance": forged}, None)
        raise RuntimeError("stop")

    _run_child(hookless, runner2, hook=False)
    unstamped = hookless.repo.get(CHILD).checkpoint
    assert unstamped.provenance is None
    assert _validate(unstamped, hookless.source, epoch=1).reason is Refusal.PROVENANCE_ABSENT


def test_hook_returning_a_foreign_subject_fails_the_save(tmp_path):
    env = _env(tmp_path)

    def wrong_subject(subject):
        stamped = env.stamp(subject)
        return CheckpointProvenance.stamp(**{
            **{name: getattr(stamped, name) for name in (
                "child_id", "state_digest", "cursor", "journal_identity",
                "run_id", "worker_id", "owner_epoch", "settled_position")},
            "sequence": subject.sequence + 1,
        })

    service = DurableContinuationService(env.repo, checkpoint_provenance=wrong_subject)

    def runner(_state, save, _control):
        save({"x": 1}, None)
        return "unreachable"

    result = service.spawn(_request(), _context("wrong"), runner).result(10)
    assert service.close(2)
    assert result.status is SubagentStatus.FAILED
    assert env.repo.get(CHILD).checkpoint is None


# --- PostgreSQL snapshot codec (no live database) -----------------------


def test_postgres_snapshot_codec_with_and_without_provenance(tmp_path):
    env = _env(tmp_path)
    base = DurableChildSession(_request(), ChildSessionLineage("session-parent"),
                               SubagentStatus.RUNNING)
    stamped = _stamped(env.source, CHILD, 0, {"k": [1, 2]}, "cur", 0)
    with_provenance = replace(base, checkpoint=stamped, revision=2)
    without = replace(base, checkpoint=ContinuableCheckpoint(CHILD, 0, {"k": 1}), revision=2)
    for record in (with_provenance, without):
        raw = encode_child_snapshot(record)
        assert isinstance(raw, bytes)
        assert decode_child_snapshot(raw) == record
        assert encode_child_snapshot(decode_child_snapshot(raw)) == raw
    assert decode_child_snapshot(encode_child_snapshot(without)).checkpoint.provenance_absent

    legacy = json.loads(encode_child_snapshot(without))
    del legacy["checkpoint"]["provenance"]  # snapshot written before this field existed
    decoded = decode_child_snapshot(canonical(legacy))
    assert decoded.checkpoint.provenance is None and decoded.checkpoint.state == {"k": 1}

    applied, _value = postgres_apply("save_checkpoint", base, (stamped,), {"expected_sequence": -1})
    assert applied.checkpoint.provenance == stamped.provenance
    foreign = ContinuableCheckpoint(CHILD, 0, {"k": "tampered"}, "cur", stamped.provenance)
    with pytest.raises(InvalidSubagentRequest, match="provenance"):
        postgres_apply("save_checkpoint", base, (foreign,), {"expected_sequence": -1})


# --- real crash cuts across the two stores ------------------------------


def _crash_worker(root: Path, cut: str) -> None:
    journal = SQLiteEffectJournal(root / "effects.db")
    source = SQLiteJournalProvenanceSource(journal, create_identity=True)
    journal.claim_owner(RUN, WORKER, 1)
    repo = SQLiteDurableContinuationRepository(root / "children.db")
    binding = AuthenticatedWorkerBinding(journal, RUN, WORKER, 1, "local-subagents")
    stamp = JournalProvenanceStamp(source, lambda _subject: ProvenanceBinding(RUN, WORKER, 1))
    if cut == "in_cas":
        original = repo._apply_save_checkpoint

        def crash_inside_cas(connection, checkpoint, **kwargs):
            value = original(connection, checkpoint, **kwargs)
            if checkpoint.sequence == 1:
                os._exit(CRASH_EXIT)  # provenance+CAS applied, not committed
            return value

        repo._apply_save_checkpoint = crash_inside_cas
    service = DurableContinuationService(repo, checkpoint_provenance=stamp)

    def runner(_state, save, _control):
        save({"phase": "before"}, "before")
        _effect(binding, "write-1")  # journal receipt commits first
        if cut == "after_receipt":
            os._exit(CRASH_EXIT)
        save({"phase": "after"}, "after")
        if cut == "after_cas":
            os._exit(CRASH_EXIT)
        return "unreachable"

    service.spawn(_request(), _context("crash"), runner).result(10)
    os._exit(78)  # a crash hook that did not fire must not pass


@pytest.mark.skipif(os.name != "posix", reason="real POSIX process crash probe")
@pytest.mark.parametrize("cut", ["after_receipt", "in_cas", "after_cas"])
def test_crash_cuts_never_expose_provenance_past_the_settled_journal(tmp_path, cut):
    repo_root = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(repo_root), os.environ.get("PYTHONPATH", ""))),
    }
    crashed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(tmp_path), cut],
        cwd=repo_root, env=environment, capture_output=True, text=True,
        timeout=60, check=False,
    )
    assert crashed.returncode == CRASH_EXIT, (crashed.returncode, crashed.stderr[-2000:])

    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    source = SQLiteJournalProvenanceSource(journal)
    repo = SQLiteDurableContinuationRepository(tmp_path / "children.db")
    checkpoint = repo.get(CHILD).checkpoint
    receipt = journal.effects_since(RUN, 0).records
    assert [(r.idempotency_key, r.state) for r in receipt] == [("write-1", EffectState.COMPLETED)]
    provenance = checkpoint.provenance
    assert provenance is not None
    assert provenance.settled_position <= journal.settled_high_water(RUN)
    assert provenance.state_digest == checkpoint_state_digest(checkpoint.state)
    if cut == "after_cas":
        assert (checkpoint.sequence, checkpoint.state, provenance.settled_position) == (1, {"phase": "after"}, 1)
    else:
        assert (checkpoint.sequence, checkpoint.state, provenance.settled_position) == (0, {"phase": "before"}, 0)
    if cut == "in_cas":
        assert repo.unresolved_mutation(CHILD) is not None  # child store stays fenced
        with sqlite3.connect(tmp_path / "children.db") as raw:
            assert raw.execute(
                "SELECT COUNT(*) FROM child_checkpoint_provenance WHERE sequence=1"
            ).fetchone() == (0,)

    journal.claim_owner(RUN, WORKER, 2)
    decision = _validate(checkpoint, source, epoch=2)
    assert decision.allowed, decision.detail
    settled = decision.receipts if cut == "after_cas" else decision.later_receipts
    assert tuple(settled) == ("write-1",)
    assert settled["write-1"].receipt_key == "receipt:write-1"


if __name__ == "__main__":
    _crash_worker(Path(sys.argv[1]), sys.argv[2])
