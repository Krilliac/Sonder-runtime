"""A killed ToolGateway worker cannot replay a non-idempotent file effect.

The direct process/compute/subagent/selfmod families have their own crash
matrix. This exercises the distinct typed tool gateway and its separate
worker checkpoint, with a real external append and SQLite state in a killed
interpreter. A marker counts each physical effect independently of receipts.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from threading import Event, Thread

import pytest

from sonder_runtime.adapters.persistence.sqlite.effect_journal import (
    SQLiteEffectJournal,
)
from sonder_runtime.application.execution.effect_journal import (
    EffectJournalError,
    EffectState,
    ReconciliationProof,
    bound,
)
from sonder_runtime.application.execution.worker_bindings import (
    AuthenticatedWorkerBinding,
)
from sonder_runtime.application.tools.gateway_contract import (
    ToolGateway,
    ToolGatewayRequest,
    ToolInvocationOutput,
    ToolPermission,
    ToolScope,
)

CRASH_EXIT = 86
CUTS = (
    "before_effect", "during_effect", "after_effect_before_receipt",
    "after_receipt_before_checkpoint", "after_checkpoint",
)


def _append(root: Path, value: str) -> None:
    with (root / "external.log").open("a", encoding="utf-8") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def _physical_effects(root: Path) -> str:
    marker = root / "external.log"
    return marker.read_text(encoding="utf-8") if marker.exists() else ""


def _gateway(invoke):
    class Schema:
        def validate(self, *_args):
            return None

    class Permissions:
        def authorize_request(self, _request):
            return "host:worker"

    class Approval:
        def approve(self, _request):
            return True

    class Invoker:
        def invoke(self, request):
            return invoke(request)

    class Redactor:
        def redact(self, _tool, value):
            return value

    class Receipts:
        def record(self, _receipt):
            return None

    return ToolGateway(Schema(), Permissions(), Approval(), Invoker(), Redactor(), Receipts())


def _request(identifier: str) -> ToolGatewayRequest:
    return ToolGatewayRequest(
        identifier, "append_non_idempotent", {"value": identifier},
        ToolScope("worker", allowed_effects=frozenset({"write_files"}), source="worker"),
        ToolPermission(frozenset({"write_files"}), reconciliation="manual"),
    )


def _child(root: Path, cut: str) -> None:
    journal = SQLiteEffectJournal(root / "effects.db")
    binding = AuthenticatedWorkerBinding(journal, "run", "worker", 1, str(root))
    assert binding.recover_before_restart().action == "resume"

    if cut == "after_effect_before_receipt":
        journal.outcome = lambda *_args, **_kwargs: os._exit(CRASH_EXIT)

    def invoke(_request):
        if cut == "before_effect":
            os._exit(CRASH_EXIT)
        _append(root, "x")
        if cut == "during_effect":
            os._exit(CRASH_EXIT)
        return ToolInvocationOutput(True, "effect-result")

    with bound(binding.binding()):
        receipt = _gateway(invoke).execute(_request("tool-1"))
    assert receipt.success
    if cut == "after_receipt_before_checkpoint":
        os._exit(CRASH_EXIT)
    journal.append_checkpoint(
        "run", {"tool_receipt": receipt.request_id}, worker_id="worker", owner_epoch=1,
    )
    if cut == "after_checkpoint":
        os._exit(CRASH_EXIT)
    os._exit(0)  # A missed crash hook never counts as a passing case.


def _overlap_child(root: Path) -> None:
    journal = SQLiteEffectJournal(root / "effects.db")
    binding = AuthenticatedWorkerBinding(journal, "run", "worker", 1, str(root))
    assert binding.recover_before_restart().action == "resume"
    first_effect_done, release = Event(), Event()

    def first(_request):
        _append(root, "a")
        first_effect_done.set()
        if not release.wait(timeout=10):
            os._exit(3)
        os._exit(CRASH_EXIT)  # First intent never receives a receipt.

    def first_worker():
        with bound(binding.binding()):
            _gateway(first).execute(_request("tool-a"))

    Thread(target=first_worker, daemon=True).start()
    if not first_effect_done.wait(timeout=10):
        os._exit(4)

    def second(_request):
        _append(root, "b")
        return ToolInvocationOutput(True, "settled-b")

    with bound(binding.binding()):
        settled = _gateway(second).execute(_request("tool-b"))
    assert settled.success
    checkpoint = journal.append_checkpoint(
        "run", {"settled": "tool-b"}, worker_id="worker", owner_epoch=1,
    )
    assert checkpoint["effect_high_water"] == 0
    release.set()
    Event().wait(timeout=10)
    os._exit(5)  # A missed crash hook never counts as a passing case.


def _run(root: Path, cut: str) -> None:
    env = {**os.environ, "SONDER_STATE_HOME": str(root / "state")}
    repo = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(repo), env.get("PYTHONPATH"))))
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(root), cut],
        cwd=repo, env=env,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == CRASH_EXIT, (result.returncode, result.stderr[-3000:])


@pytest.mark.parametrize("cut", CUTS)
def test_gateway_hard_crash_retains_exact_effect_and_refuses_duplicate(cut, tmp_path):
    _run(tmp_path, cut)
    expected = "" if cut == "before_effect" else "x"
    assert _physical_effects(tmp_path) == expected
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    intent = journal.get("run:tool-1")
    assert intent is not None and journal.high_water("run") == 1
    settled = cut in {"after_receipt_before_checkpoint", "after_checkpoint"}
    assert intent.state is (EffectState.COMPLETED if settled else EffectState.INTENT)
    assert bool(intent.receipt_key) is settled

    restarted = AuthenticatedWorkerBinding(journal, "run", "worker", 2, str(tmp_path))
    if settled:
        assert restarted.recover_before_restart().action == "resume"
        checkpoint = journal.restore_checkpoint("run")
        if cut == "after_checkpoint":
            assert checkpoint["effect_high_water"] == 1
            assert checkpoint["state"] == {"tool_receipt": "tool-1"}
        else:
            assert checkpoint is None  # Settled receipt alone does not invent work state.
    else:
        with pytest.raises(EffectJournalError, match="reconciliation"):
            restarted.recover_before_restart()
        assert journal.get(intent.intent_id).state is EffectState.UNCERTAIN
        with pytest.raises(EffectJournalError, match="without a definitive outcome"):
            journal.validate_checkpoint("run", 1)

    invoked = []
    with bound(restarted.binding()), pytest.raises(EffectJournalError):
        _gateway(lambda _request: invoked.append(1) or ToolInvocationOutput(True, "duplicate")).execute(
            _request("tool-1")
        )
    assert invoked == [] and _physical_effects(tmp_path) == expected


def test_overlapping_effect_keeps_later_receipt_but_fences_checkpoint_replay(tmp_path):
    _run(tmp_path, "overlap")
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    assert _physical_effects(tmp_path) == "ab"

    assert journal.get("run:tool-a").state is EffectState.INTENT
    assert journal.get("run:tool-b").state is EffectState.COMPLETED
    with pytest.raises(EffectJournalError, match="without a definitive outcome"):
        journal.restore_checkpoint("run")
    with pytest.raises(EffectJournalError, match="reconciliation"):
        AuthenticatedWorkerBinding(journal, "run", "worker", 2, str(tmp_path)).recover_before_restart()
    assert journal.get("run:tool-a").state is EffectState.UNCERTAIN
    assert journal.get("run:tool-b").state is EffectState.COMPLETED
    assert _physical_effects(tmp_path) == "ab"

    class MarkerVerifier:
        verifier_id = "external-marker-v1"
        operation_ids = frozenset({"tool-a"})

        def verify(self, intent):
            # This host verifier reads the independently fsynced effect; a
            # caller's strategy observation or free-form text is not proof.
            if _physical_effects(tmp_path).count("a") != 1:
                return None
            return ReconciliationProof(
                intent_id=intent.intent_id, operation_id=intent.operation_id,
                receipt_key="marker-a", outcome_digest=hashlib.sha256(b"a").hexdigest(),
                state=EffectState.COMPLETED, verifier_id=self.verifier_id,
                external_reference="external.log:a",
            )

    verified = SQLiteEffectJournal(
        tmp_path / "effects.db", reconciliation_verifiers={"tool-a": MarkerVerifier()},
    )
    assert verified.reconcile("run:tool-a", owner_epoch=2).state is EffectState.COMPLETED
    checkpoint = verified.restore_checkpoint("run")
    assert (checkpoint["effect_high_water"], checkpoint["journal_high_water"]) == (0, 2)
    later = verified.effects_since("run", checkpoint["effect_high_water"])
    assert [(item.intent_id, item.state) for item in later.records] == [
        ("run:tool-a", EffectState.COMPLETED), ("run:tool-b", EffectState.COMPLETED),
    ]
    assert AuthenticatedWorkerBinding(
        verified, "run", "worker", 2, str(tmp_path),
    ).recover_before_restart().action == "resume"
    assert _physical_effects(tmp_path) == "ab"


def test_redactor_failure_after_physical_effect_requires_reconciliation(tmp_path):
    """An in-process post-invoke error cannot leave a reattachable bare intent."""
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    binding = AuthenticatedWorkerBinding(journal, "run", "worker", 1, str(tmp_path))
    assert binding.recover_before_restart().action == "resume"
    gateway = _gateway(lambda _request: _append(tmp_path, "x") or ToolInvocationOutput(True, "ok"))

    def broken_redaction(_tool, _value):
        raise RuntimeError("redaction unavailable")

    gateway._redactor.redact = broken_redaction
    with bound(binding.binding()), pytest.raises(RuntimeError, match="redaction unavailable"):
        gateway.execute(_request("tool-1"))
    assert _physical_effects(tmp_path) == "x"
    assert journal.get("run:tool-1").state is EffectState.UNCERTAIN
    assert journal.recover("run", live_workers={"worker": 1}).action == "reconcile"


@pytest.mark.parametrize("committed", [False, True])
def test_receipt_failure_preserves_original_error_and_durable_effect_state(tmp_path, committed):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    binding = AuthenticatedWorkerBinding(journal, "run", "worker", 1, str(tmp_path))
    assert binding.recover_before_restart().action == "resume"
    original = journal.outcome

    def fail_receipt(outcome):
        if committed:
            original(outcome)
        raise RuntimeError("receipt acknowledgement lost")

    journal.outcome = fail_receipt
    with bound(binding.binding()), pytest.raises(RuntimeError, match="receipt acknowledgement lost"):
        _gateway(lambda _request: _append(tmp_path, "x") or ToolInvocationOutput(True, "ok")).execute(
            _request("tool-1")
        )
    assert _physical_effects(tmp_path) == "x"
    assert journal.get("run:tool-1").state is (
        EffectState.COMPLETED if committed else EffectState.UNCERTAIN
    )
    assert journal.recover("run", live_workers={"worker": 1}).action == (
        "resume" if committed else "reconcile"
    )


def test_uncertainty_write_failure_preserves_original_post_invoke_error(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    binding = AuthenticatedWorkerBinding(journal, "run", "worker", 1, str(tmp_path))
    assert binding.recover_before_restart().action == "resume"
    gateway = _gateway(lambda _request: _append(tmp_path, "x") or ToolInvocationOutput(True, "ok"))

    def failed_redaction(_tool, _value):
        raise RuntimeError("redaction unavailable")

    def failed_uncertainty(_intent_id, *, detail):
        raise OSError("journal unavailable")

    gateway._redactor.redact = failed_redaction
    journal.uncertain = failed_uncertainty
    with bound(binding.binding()), pytest.raises(RuntimeError, match="redaction unavailable"):
        gateway.execute(_request("tool-1"))
    assert _physical_effects(tmp_path) == "x"
    # A storage fault can prevent marking uncertainty immediately; the
    # unresolved durable intent still blocks the next worker owner's restart.
    assert journal.get("run:tool-1").state is EffectState.INTENT
    with pytest.raises(EffectJournalError, match="reconciliation"):
        AuthenticatedWorkerBinding(
            journal, "run", "worker", 2, str(tmp_path),
        ).recover_before_restart()
    assert journal.get("run:tool-1").state is EffectState.UNCERTAIN


if __name__ == "__main__":
    _root, _cut = Path(sys.argv[1]), sys.argv[2]
    _overlap_child(_root) if _cut == "overlap" else _child(_root, _cut)
