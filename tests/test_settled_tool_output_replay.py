"""Settled gateway outputs survive reopen; missing retained output blocks continuation."""
from dataclasses import replace

import pytest

from sonder_runtime.adapters.persistence.sqlite.effect_journal import SQLiteEffectJournal
from sonder_runtime.adapters.persistence.tool_audit import DurableToolAuditRepository, ToolAuditLimits
from sonder_runtime.application.execution import gateway_calls
from sonder_runtime.application.execution.effect_journal import (
    EffectJournalError, JournalBinding, bound, settled_receipts,
)
from sonder_runtime.application.execution.gateway_calls import GatewayCallSequence, GatewayCallSequenceHalted
from sonder_runtime.application.tools.gateway_contract import (
    ToolGateway, ToolGatewayRequest, ToolInvocationOutput, ToolPermission, ToolScope,
)


class _Ports:
    def validate(self, *_args): pass
    def authorize(self, *_args): pass
    def approve(self, *_args): return True
    def redact(self, _name, value): return value
    def record(self, receipt): self.receipts.append(receipt)

    def __init__(self, target, *, success=True):
        self.target = target
        self.success = success
        self.calls = 0
        self.receipts = []

    def invoke(self, request):
        self.calls += 1
        with self.target.open("a") as stream:
            stream.write(request.arguments["content"])
        return ToolInvocationOutput(
            self.success, {"files": ["append.txt"], "bytes": len(request.arguments["content"])},
            "CHECK_FAILED" if not self.success else "",
            "check did not pass" if not self.success else "",
        )


def _sequence():
    return GatewayCallSequence(run_id="child-run", worker_id="worker", child_id="child", dispatch_attempt=1)


def _request(request_id, content="x"):
    return ToolGatewayRequest(
        request_id, "write_file", {"path": "append.txt", "content": content},
        ToolScope("owner", allowed_effects=frozenset({"write_files"}), source="worker"),
        ToolPermission(frozenset({"write_files"})),
    )


def _gateway(ports, audit):
    return ToolGateway(ports, ports, ports, ports, ports, ports, audit=audit)


@pytest.mark.parametrize("success", [True, False])
@pytest.mark.parametrize("rotate", [False, True])
def test_settled_output_is_consumed_after_store_reopen(tmp_path, success, rotate):
    journal_path = tmp_path / "effects.db"
    audit_path = tmp_path / "audit.jsonl"
    limits = ToolAuditLimits(max_records=1 if rotate else 20)
    journal = SQLiteEffectJournal(journal_path)
    audit = DurableToolAuditRepository(audit_path, limits=limits)
    ports = _Ports(tmp_path / "append.txt", success=success)
    binding = JournalBinding(journal, "child-run", "worker", 1, "/workspace")
    with bound(binding), gateway_calls.bound(_sequence()):
        first = _gateway(ports, audit).execute(_request("first"))
    if rotate:
        # An unrelated call naturally rotates the settled output's chain.
        _gateway(ports, audit).execute(_request("unrelated", "y"))
    before = journal.effects_since("child-run", 0).records
    journal = SQLiteEffectJournal(journal_path)
    audit = DurableToolAuditRepository(audit_path, limits=limits)
    journal.claim_owner("child-run", "worker", 2)
    receipts = {record.idempotency_key: record.receipt_key for record in before}
    with bound(replace(binding, journal=journal, owner_epoch=2)), \
            settled_receipts(receipts), gateway_calls.bound(_sequence()) as calls:
        replay = _gateway(ports, audit).execute(_request("resumed"))
        assert calls.issued == 1 and calls.halted is None
    assert replay.output == first.output
    assert (replay.success, replay.error_code, replay.error, replay.terminal) == (
        first.success, first.error_code, first.error, first.terminal,
    )
    assert replay.request_id == "resumed" and replay.evidence["replayed_from"] == "first"
    assert journal.effects_since("child-run", 0).records == before
    assert ports.calls == (2 if rotate else 1)
    assert ports.target.read_text() == ("xy" if rotate else "x")
    assert audit.read_receipt("resumed")["evidence"]["replayed_from"] == "first"


def test_pruned_settled_output_halts_the_runner_without_repeating_the_effect(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    audit = DurableToolAuditRepository(
        tmp_path / "audit.jsonl", limits=ToolAuditLimits(max_records=1, max_rotated_files=1),
    )
    ports = _Ports(tmp_path / "append.txt")
    binding = JournalBinding(journal, "child-run", "worker", 1, "/workspace")
    with bound(binding), gateway_calls.bound(_sequence()):
        _gateway(ports, audit).execute(_request("first"))
    before = journal.effects_since("child-run", 0).records
    _gateway(ports, audit).execute(_request("unrelated-1", "y"))
    _gateway(ports, audit).execute(_request("unrelated-2", "z"))
    assert audit.read_receipt("first") is None
    journal.claim_owner("child-run", "worker", 2)
    with bound(replace(binding, owner_epoch=2)), \
            settled_receipts({r.idempotency_key: r.receipt_key for r in before}), \
            gateway_calls.bound(_sequence()) as calls:
        with pytest.raises(EffectJournalError, match="no longer retained"):
            _gateway(ports, audit).execute(_request("resumed"))
        assert calls.halted == "EffectJournalError"
        with pytest.raises(GatewayCallSequenceHalted):
            _gateway(ports, audit).execute(_request("later", "next"))
    assert ports.target.read_text() == "xyz"
    assert ports.calls == 3
    assert journal.effects_since("child-run", 0).records == before
