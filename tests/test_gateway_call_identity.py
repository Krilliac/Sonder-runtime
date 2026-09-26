"""Deterministic gateway call identities for child runners (#515).

Unit coverage for the pieces the crash-cut wiring test
(``test_wiring_journal_child_gateway_calls.py``) composes: the call sequence
and its identity derivation, the gateway's choice between a bound sequence
and the caller's ``request_id``, the ordinal recorded in checkpoint
provenance (version 2), its compatibility with version-1 records in SQLite
and the codec, and the resume validator's ordinal checks.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from sonder_runtime.adapters.persistence.durable_continuation import (
    SQLiteDurableContinuationRepository,
    SQLiteJournalProvenanceSource,
)
from sonder_runtime.adapters.persistence.sqlite.effect_journal import (
    SQLiteEffectJournal,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.execution import gateway_calls
from sonder_runtime.application.execution.effect_journal import (
    DivergentEffectReplay,
    EffectJournalError,
    EffectState,
    JournalBinding,
    SettledEffectReplay,
    bound,
    settled_receipts,
)
from sonder_runtime.application.execution.gateway_calls import (
    GatewayCallSequence,
    GatewayCallSequenceHalted,
    gateway_call_operation_id,
    gateway_request_digest,
    parse_gateway_call_operation,
)
from sonder_runtime.application.ports.continuation_mutations import canonical
from sonder_runtime.application.ports.subagents import (
    SubagentBudget,
    SubagentRequest,
)
from sonder_runtime.application.subagents.checkpoint_provenance import (
    CheckpointProvenanceError,
    CheckpointResumeRefusal,
    JournalProvenanceStamp,
    ProvenanceBinding,
    ProvenanceSubject,
    validate_checkpoint_resume,
)
from sonder_runtime.application.subagents.continuable import (
    CheckpointProvenance,
    ContinuableCheckpoint,
    checkpoint_state_digest,
)
from sonder_runtime.application.subagents.continuation_codec import (
    provenance_from_data,
    session_from_data,
)
from sonder_runtime.application.subagents.durable_continuation import (
    DurableContinuationService,
)
from sonder_runtime.application.tools.gateway_contract import (
    RedactedOutput,
    ToolGateway,
    ToolGatewayRequest,
    ToolInvocationOutput,
    ToolPermission,
    ToolScope,
)

RUN = "subagent:child-1"
WORKER = "subagent:node-1"
CHILD = "child-1"


def _sequence(issued: int = 0, *, attempt: int = 1) -> GatewayCallSequence:
    return GatewayCallSequence(
        run_id=RUN, worker_id=WORKER, child_id=CHILD, dispatch_attempt=attempt, issued=issued,
    )


# --- identity -------------------------------------------------------------


class _Journal:
    """Minimal journal: records admitted intents, optionally fails ``begin``."""

    def __init__(self, fail: BaseException | None = None):
        self.fail = fail
        self.intents = {}

    def get(self, intent_id):
        return self.intents.get(intent_id)

    def begin(self, intent):
        if self.fail is not None:
            error, self.fail = self.fail, None
            raise error
        self.intents[intent.intent_id] = intent
        return intent


def _admit(sequence, binding, content="x"):
    return sequence.admit(binding, tool_name="write_file",
                          arguments={"path": "a", "content": content},
                          effects={"write_files"})


def test_call_identity_is_fixed_by_child_attempt_and_ordinal_and_keyed_by_request():
    binding = JournalBinding(_Journal(), RUN, WORKER, 1, "/workspace")  # type: ignore[arg-type]
    request = {"tool_name": "write_file", "arguments": {"path": "a", "content": "x"},
               "effects": {"write_files"}}
    first = _sequence().identity(1, **request)
    admitted = _admit(_sequence(), binding)
    # A new incarnation (new epoch) re-derives the same identity.
    again = _admit(_sequence(), replace(binding, journal=_Journal(), owner_epoch=7))
    assert (admitted.operation_id, admitted.idempotency_key, admitted.request_digest) \
        == (again.operation_id, again.idempotency_key, again.request_digest) \
        == (first.operation_id, first.idempotency_key, first.request_digest)
    assert first.ordinal == 1
    assert first.operation_id == gateway_call_operation_id(CHILD, 1, 1)
    assert parse_gateway_call_operation(first.operation_id) == gateway_calls.GatewayCallOperation(
        CHILD, 1, 1,
    )
    assert first.request_digest == gateway_request_digest(
        "write_file", {"content": "x", "path": "a"}, ["write_files"],
    )
    assert json.loads(first.idempotency_key) == [
        "gateway-call", RUN, WORKER, CHILD, 1, 1, first.request_digest,
    ]
    # A different request at the same ordinal: same intent id, different key.
    other = _sequence().identity(1, tool_name="write_file",
                                 arguments={"path": "a", "content": "y"},
                                 effects={"write_files"})
    assert other.operation_id == first.operation_id
    assert other.idempotency_key != first.idempotency_key
    # The tool and its effects are part of the digest, not only arguments.
    assert gateway_request_digest("edit_file", {"path": "a", "content": "x"}, ["write_files"]) \
        != first.request_digest
    assert gateway_request_digest("write_file", {"path": "a", "content": "x"}, []) \
        != first.request_digest
    # The dispatch attempt and a resumed starting ordinal move the identity.
    assert _sequence(attempt=2).identity(1, **request).operation_id \
        == gateway_call_operation_id(CHILD, 2, 1)
    resumed = _sequence(issued=3)
    assert _admit(resumed, replace(binding, journal=_Journal())).operation_id \
        == gateway_call_operation_id(CHILD, 1, 4)
    assert resumed.issued == 4
    with pytest.raises(EffectJournalError):
        _sequence().identity(0, **request)
    for text in ("gateway-call:c#dispatch-attempt-0#call-1", "gateway-call:c#call-1",
                 "subagent-dispatch:c", "gateway-call:#dispatch-attempt-1#call-1"):
        assert parse_gateway_call_operation(text) is None


def test_sequence_refuses_a_foreign_journal_binding_without_consuming_an_ordinal():
    sequence = _sequence()
    for run_id, worker_id in ((RUN + "-other", WORKER), (RUN, WORKER + "-other")):
        with pytest.raises(EffectJournalError, match="does not belong"):
            _admit(sequence, JournalBinding(_Journal(), run_id, worker_id, 1, "/w"))  # type: ignore[arg-type]
    # A malformed request is refused before any ordinal is addressed.
    with pytest.raises(EffectJournalError, match="tool name"):
        sequence.admit(JournalBinding(_Journal(), RUN, WORKER, 1, "/w"),  # type: ignore[arg-type]
                       tool_name=" ", arguments={}, effects=())
    assert (sequence.issued, sequence.halted) == (0, None)
    for bad in ({"dispatch_attempt": 0}, {"issued": -1}, {"child_id": " "}):
        with pytest.raises(EffectJournalError):
            GatewayCallSequence(**{"run_id": RUN, "worker_id": WORKER, "child_id": CHILD,
                                   "dispatch_attempt": 1, "issued": 0, **bad})


def test_a_failed_admission_halts_the_sequence_without_consuming_its_ordinal():
    journal = _Journal(fail=sqlite3.OperationalError("database is locked"))
    binding = JournalBinding(journal, RUN, WORKER, 1, "/w")  # type: ignore[arg-type]
    sequence = _sequence()
    with pytest.raises(sqlite3.OperationalError):
        _admit(sequence, binding)
    assert (sequence.issued, sequence.halted) == (0, "OperationalError")
    # The journal would now accept the retry, but the runner may not move
    # past a call the journal did not account for.
    with pytest.raises(GatewayCallSequenceHalted, match="OperationalError"):
        _admit(sequence, binding)
    assert sequence.issued == 0 and journal.intents == {}


# --- gateway --------------------------------------------------------------


class _Schema:
    def validate(self, *_args):
        return None


class _Permissions:
    def authorize_request(self, _request):
        return "permission:test"


class _Approval:
    def approve(self, _request):
        return True


class _Invoker:
    def __init__(self):
        self.calls = []

    def invoke(self, request):
        self.calls.append(dict(request.arguments))
        return ToolInvocationOutput(True, output="done")


class _Redactor:
    def redact(self, _tool, value):
        return RedactedOutput(value, False)


class _Receipts:
    def record(self, _receipt):
        return None


def _gateway():
    invoker = _Invoker()
    return ToolGateway(_Schema(), _Permissions(), _Approval(), invoker, _Redactor(),
                       _Receipts()), invoker


def _request(request_id: str, content: str = "x") -> ToolGatewayRequest:
    return ToolGatewayRequest(
        request_id, "write_file", {"path": "a.txt", "content": content},
        ToolScope("worker", allowed_effects=frozenset({"write_files"}), source="worker"),
        ToolPermission(frozenset({"write_files"})),
    )


def test_gateway_outside_a_child_runner_keeps_the_request_id_as_its_key(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    gateway, _invoker = _gateway()
    with bound(JournalBinding(journal, "plain-run", "worker", 1, "/w")):
        gateway.execute(_request("req-1"))
    stored = journal.get("plain-run:req-1")
    assert (stored.operation_id, stored.idempotency_key) == ("req-1", "req-1")
    assert stored.state is EffectState.COMPLETED


def test_gateway_in_a_child_runner_matches_settled_calls_and_refuses_divergence(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    gateway, invoker = _gateway()
    with bound(JournalBinding(journal, RUN, WORKER, 1, "/w")), \
            gateway_calls.bound(_sequence()) as calls:
        first = gateway.execute(_request("fresh-1"))
        gateway.execute(_request("fresh-2", "second"))
        # A pure call is not journaled and consumes no ordinal.
        pure = ToolGatewayRequest(
            "fresh-3", "read_file", {"path": "a.txt"},
            ToolScope("worker", source="worker"), ToolPermission(),
        )
        gateway.execute(pure)
        assert calls.issued == 2
    stored = journal.get(f"{RUN}:{gateway_call_operation_id(CHILD, 1, 1)}")
    assert stored.state is EffectState.COMPLETED and stored.receipt_key == first.request_id
    assert journal.get(f"{RUN}:fresh-1") is None

    # A resumed incarnation (newer epoch) re-issues from the checkpoint's
    # ordinal 1 with the settled receipts bound, as the provider does.
    journal.claim_owner(RUN, WORKER, 2)
    settled = {record.idempotency_key: record.receipt_key
               for record in journal.effects_since(RUN, 0).records}
    with bound(JournalBinding(journal, RUN, WORKER, 2, "/w")), settled_receipts(settled):
        with gateway_calls.bound(_sequence(issued=1)):
            with pytest.raises(SettledEffectReplay) as replay:
                gateway.execute(_request("fresh-4", "second"))
            assert replay.value.receipt_key == "fresh-2"
            # Ordinal 3 is new: it runs.
            gateway.execute(_request("fresh-5", "third"))
        with gateway_calls.bound(_sequence(issued=0)):
            with pytest.raises(DivergentEffectReplay):
                gateway.execute(_request("fresh-6", "not-what-ran"))
    assert [call.get("content") for call in invoker.calls] == ["x", "second", None, "third"]
    assert journal.high_water(RUN) == 3


def test_gateway_refuses_a_call_sequence_for_another_run(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    gateway, invoker = _gateway()
    with bound(JournalBinding(journal, "other-run", WORKER, 1, "/w")), \
            gateway_calls.bound(_sequence()):
        with pytest.raises(EffectJournalError, match="does not belong"):
            gateway.execute(_request("fresh-1"))
    assert invoker.calls == [] and journal.high_water("other-run") == 0


def _resumed_after_crash(journal, *, issued: int = 0):
    """Settled receipts and binding for a resumed incarnation (epoch 2)."""
    journal.claim_owner(RUN, WORKER, 2)
    settled = {record.idempotency_key: record.receipt_key
               for record in journal.effects_since(RUN, 0).records}
    return JournalBinding(journal, RUN, WORKER, 2, "/w"), settled


def test_runner_that_swallows_a_divergent_replay_cannot_rerun_a_settled_call(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    gateway, invoker = _gateway()
    with bound(JournalBinding(journal, RUN, WORKER, 1, "/w")), \
            gateway_calls.bound(_sequence()):
        gateway.execute(_request("fresh-1", "x"))
    # Crash; resume from a checkpoint at ordinal 0 with the receipts bound.
    binding, settled = _resumed_after_crash(journal)
    with bound(binding), settled_receipts(settled), \
            gateway_calls.bound(_sequence(issued=0)) as calls:
        with pytest.raises(DivergentEffectReplay):
            gateway.execute(_request("fresh-2", "z"))
        # The runner handles the refusal and asks for the call that settled.
        # It must not run again at a fresh ordinal.
        with pytest.raises(GatewayCallSequenceHalted, match="DivergentEffectReplay"):
            gateway.execute(_request("fresh-3", "x"))
        with pytest.raises(GatewayCallSequenceHalted):
            gateway.execute(_request("fresh-4", "new"))
        assert (calls.issued, calls.halted) == (0, "DivergentEffectReplay")
    assert [call["content"] for call in invoker.calls] == ["x"]
    assert [(r.operation_id, r.state) for r in journal.effects_since(RUN, 0).records] == [
        (gateway_call_operation_id(CHILD, 1, 1), EffectState.COMPLETED),
    ]


def test_transient_admission_failure_then_retry_cannot_shift_a_settled_ordinal(
    tmp_path, monkeypatch,
):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    gateway, invoker = _gateway()
    actual_begin = journal.begin
    failures = [sqlite3.OperationalError("database is locked")]

    def begin_once_locked(intent):
        if failures:
            raise failures.pop()
        return actual_begin(intent)

    monkeypatch.setattr(journal, "begin", begin_once_locked)
    with bound(JournalBinding(journal, RUN, WORKER, 1, "/w")), \
            gateway_calls.bound(_sequence()) as calls:
        with pytest.raises(sqlite3.OperationalError):
            gateway.execute(_request("fresh-1", "x"))
        # The runner retries: refused, so nothing settles at ordinal 2.
        with pytest.raises(GatewayCallSequenceHalted, match="OperationalError"):
            gateway.execute(_request("fresh-2", "x"))
        assert calls.issued == 0
    assert invoker.calls == [] and journal.high_water(RUN) == 0

    # The child fails and resumes from ordinal 0: the call runs once, at 1.
    binding, settled = _resumed_after_crash(journal)
    with bound(binding), settled_receipts(settled), \
            gateway_calls.bound(_sequence(issued=0)) as calls:
        gateway.execute(_request("fresh-3", "x"))
        assert (calls.issued, calls.halted) == (1, None)
    assert [call["content"] for call in invoker.calls] == ["x"]
    assert [(r.operation_id, r.state) for r in journal.effects_since(RUN, 0).records] == [
        (gateway_call_operation_id(CHILD, 1, 1), EffectState.COMPLETED),
    ]


# --- provenance -----------------------------------------------------------


def _source(root: Path):
    journal = SQLiteEffectJournal(root / "effects.db")
    source = SQLiteJournalProvenanceSource(journal, create_identity=True)
    journal.claim_owner(RUN, WORKER, 1)
    return journal, source


def _stamp(source):
    return JournalProvenanceStamp(source, lambda _subject: ProvenanceBinding(RUN, WORKER, 1))


def _subject(state=None, child_id=CHILD):
    state = state or {"step": 1}
    return ProvenanceSubject(child_id, 0, checkpoint_state_digest(state), "cursor")


def test_stamp_records_the_bound_ordinal_and_refuses_a_foreign_sequence(tmp_path):
    _journal, source = _source(tmp_path)
    stamp = _stamp(source)
    assert stamp(_subject()).gateway_call_ordinal == 0
    with gateway_calls.bound(_sequence(issued=5)):
        provenance = stamp(_subject())
    assert (provenance.version, provenance.gateway_call_ordinal) == (2, 5)
    assert provenance.digest_valid
    assert not replace(provenance, gateway_call_ordinal=4).digest_valid
    for foreign in (
        GatewayCallSequence(run_id=RUN, worker_id=WORKER, child_id="child-2", dispatch_attempt=1),
        GatewayCallSequence(run_id="subagent:x", worker_id=WORKER, child_id=CHILD,
                            dispatch_attempt=1),
    ):
        with gateway_calls.bound(foreign), pytest.raises(CheckpointProvenanceError):
            stamp(_subject())
    halted = _sequence(issued=2)
    with pytest.raises(sqlite3.OperationalError):
        _admit(halted, JournalBinding(
            _Journal(fail=sqlite3.OperationalError("database is locked")),  # type: ignore[arg-type]
            RUN, WORKER, 1, "/w",
        ))
    with gateway_calls.bound(halted), pytest.raises(CheckpointProvenanceError, match="halted"):
        stamp(_subject())


def _v1(provenance: CheckpointProvenance) -> CheckpointProvenance:
    fields = {name: getattr(provenance, name) for name in (
        "child_id", "sequence", "state_digest", "cursor", "journal_identity", "run_id",
        "worker_id", "owner_epoch", "settled_position",
    )}
    return CheckpointProvenance(
        **fields, record_digest=CheckpointProvenance.compute_digest(**fields, version=1),
        version=1,
    )


def test_version_one_records_stay_valid_only_without_an_ordinal(tmp_path):
    _journal, source = _source(tmp_path)
    legacy = _v1(_stamp(source)(_subject()))
    assert legacy.digest_valid and legacy.gateway_call_ordinal == 0
    # The version-1 digest does not cover the ordinal, so one cannot be added.
    assert not replace(legacy, gateway_call_ordinal=1).digest_valid
    # Codec: a snapshot written before the field existed decodes as version 1.
    data = asdict(legacy)
    del data["gateway_call_ordinal"]
    assert provenance_from_data(data) == legacy
    assert provenance_from_data(asdict(legacy)) == legacy
    with pytest.raises(Exception, match="malformed"):
        provenance_from_data({**asdict(legacy), "extra": 1})


def test_sqlite_store_adds_the_ordinal_column_to_a_version_one_table(tmp_path):
    _journal, source = _source(tmp_path)
    repository = SQLiteDurableContinuationRepository(tmp_path / "children.db")
    service = DurableContinuationService(repository, checkpoint_provenance=_stamp(source))

    def runner(_state, save, _control):
        with gateway_calls.bound(_sequence(issued=2)):
            save({"step": 1}, "cursor")
        return "done"

    request = SubagentRequest("parent", "work", SubagentBudget(max_steps=4), CHILD)
    service.spawn(request, local_owner_context(correlation_id="c"), runner).result(10)
    assert service.close(2)
    stamped = repository.get(CHILD).checkpoint.provenance
    assert stamped.gateway_call_ordinal == 2 and stamped.digest_valid
    snapshot = json.loads(canonical(asdict(repository.get(CHILD))))
    assert session_from_data(snapshot) == repository.get(CHILD)

    # Rebuild the table as it was before version 2 and store a v1 record.
    legacy = _v1(stamped)
    with sqlite3.connect(tmp_path / "children.db") as raw:
        raw.execute("DROP TRIGGER child_checkpoint_provenance_no_update")
        raw.execute("DROP TRIGGER child_checkpoint_provenance_no_delete")
        raw.execute("DROP TABLE child_checkpoint_provenance")
        raw.execute(
            "CREATE TABLE child_checkpoint_provenance (child_id TEXT NOT NULL, "
            "sequence INTEGER NOT NULL, version INTEGER NOT NULL, state_digest TEXT NOT NULL, "
            "cursor TEXT, journal_identity TEXT NOT NULL, run_id TEXT NOT NULL, "
            "worker_id TEXT NOT NULL, owner_epoch INTEGER NOT NULL, "
            "settled_position INTEGER NOT NULL, record_digest TEXT NOT NULL, "
            "PRIMARY KEY (child_id, sequence))"
        )
        raw.execute(
            "INSERT INTO child_checkpoint_provenance VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (legacy.child_id, legacy.sequence, 1, legacy.state_digest, legacy.cursor,
             legacy.journal_identity, legacy.run_id, legacy.worker_id, legacy.owner_epoch,
             legacy.settled_position, legacy.record_digest),
        )
    reopened = SQLiteDurableContinuationRepository(tmp_path / "children.db").get(CHILD)
    assert reopened.checkpoint.provenance == legacy
    assert reopened.checkpoint.provenance.digest_valid
    with sqlite3.connect(tmp_path / "children.db") as raw:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            raw.execute("UPDATE child_checkpoint_provenance SET gateway_call_ordinal=9")


# --- resume validation ----------------------------------------------------


def _complete(journal, operation_id: str, key: str, epoch: int = 1) -> None:
    binding = JournalBinding(journal, RUN, WORKER, epoch, "/w")
    intent = binding.begin_request(operation_id=operation_id, idempotency_key=key,
                                   request_digest="d" * 64)
    binding.complete(intent, outcome_digest="o" * 64, receipt_key=f"receipt-{key}")


def _checkpoint(source, *, ordinal: int, position: int, legacy: bool = False):
    state = {"step": 1}
    identity = source.position(RUN, WORKER).journal_identity
    provenance = CheckpointProvenance.stamp(
        child_id=CHILD, sequence=0, state_digest=checkpoint_state_digest(state),
        cursor="cursor", journal_identity=identity, run_id=RUN, worker_id=WORKER,
        owner_epoch=1, settled_position=position, gateway_call_ordinal=ordinal,
    )
    if legacy:
        provenance = _v1(provenance)
    return ContinuableCheckpoint(CHILD, 0, state, "cursor", provenance)


def _validate(checkpoint, source):
    return validate_checkpoint_resume(checkpoint, source, run_id=RUN, worker_id=WORKER,
                                      resumer_owner_epoch=2)


def test_validator_hands_back_the_ordinal_and_refuses_one_behind_the_journal(tmp_path):
    journal, source = _source(tmp_path)
    _complete(journal, gateway_call_operation_id(CHILD, 1, 1), "call-1")
    _complete(journal, gateway_call_operation_id(CHILD, 1, 2), "call-2")
    journal.claim_owner(RUN, WORKER, 2)

    allowed = _validate(_checkpoint(source, ordinal=1, position=1), source)
    assert allowed.allowed and allowed.gateway_call_ordinal == 1
    assert set(allowed.receipts) == {"call-1"} and set(allowed.later_receipts) == {"call-2"}

    behind = _validate(_checkpoint(source, ordinal=1, position=2), source)
    assert behind.reason is CheckpointResumeRefusal.GATEWAY_ORDINAL_BEHIND_JOURNAL


def test_validator_refuses_a_request_id_keyed_call_after_any_checkpoint(tmp_path):
    journal, source = _source(tmp_path)
    _complete(journal, "op-a", "key-a")
    journal.claim_owner(RUN, WORKER, 2)
    assert _validate(_checkpoint(source, ordinal=0, position=0, legacy=True), source).allowed
    assert _validate(_checkpoint(source, ordinal=0, position=0), source).allowed
    _complete(journal, "fresh-request-id", "fresh-request-id", epoch=2)
    # Version 1 predates deterministic identities.  In a version-2 run such
    # an intent means a runner bound the child's journal without its call
    # sequence; a resumed runner would re-issue it under another key.
    for legacy in (True, False):
        refused = _validate(_checkpoint(source, ordinal=0, position=0, legacy=legacy), source)
        assert refused.reason is CheckpointResumeRefusal.REQUEST_ID_KEYED_CALL_AFTER_POSITION


def test_validator_refuses_a_gap_in_the_gateway_calls_after_the_checkpoint(tmp_path):
    journal, source = _source(tmp_path)
    _complete(journal, gateway_call_operation_id(CHILD, 1, 1), "call-1")
    _complete(journal, gateway_call_operation_id(CHILD, 1, 3), "call-3")
    journal.claim_owner(RUN, WORKER, 2)
    gap = _validate(_checkpoint(source, ordinal=0, position=0), source)
    assert gap.reason is CheckpointResumeRefusal.GATEWAY_ORDINAL_GAP
    assert "call 3, expected 2" in gap.detail
    # Resumed after ordinal 1 the gap is still the first thing it meets.
    assert _validate(_checkpoint(source, ordinal=1, position=1), source).reason \
        is CheckpointResumeRefusal.GATEWAY_ORDINAL_GAP
    # A checkpoint that covers both calls has nothing after it to re-address.
    assert _validate(_checkpoint(source, ordinal=3, position=2), source).allowed
    # Ordinals that do not start right after the checkpoint's are a gap too.
    assert _validate(_checkpoint(source, ordinal=1, position=0), source).reason \
        is CheckpointResumeRefusal.GATEWAY_ORDINAL_GAP
