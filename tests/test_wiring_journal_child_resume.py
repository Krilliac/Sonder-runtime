"""Crash-then-resume of a delegated child through bootstrap composition (#515).

A real child interpreter composes the application with ``build_application``,
dispatches a delegation through ``DelegationService`` and its runner performs
one journaled file append, then dies with ``os._exit`` after the append's
receipt committed but before the runner saved the next checkpoint.  A second
composition (this test process) then repeats the exact delegation.  The
production path must:

* stamp every child checkpoint with journal provenance (the saved checkpoint
  carries it);
* prove the dead owner, claim a newer journal epoch, validate the checkpoint
  against the journal, and resume from it;
* hand the resumed runner the settled receipts so the append is consumed,
  not repeated (the file holds exactly one append and the journal one intent);
* refuse with a typed ``recovery_required`` reason when provenance cannot be
  validated.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

CRASH_EXIT = 91
CHILD = "wiring-resume-child"
DELEGATION = "wiring-resume-delegation"
WRITE_KEY = f"{CHILD}:append-once"


def _config(root: Path):
    from sonder_runtime.platform.config import SonderConfig

    config = SonderConfig()
    return replace(config, state=replace(
        config.state, home=str(root / "state"), workspace_roots=(str(root / "workspace"),),
    ))


def _runner_factory(root: Path, *, crash_after_write: bool):
    """A runner that checkpoints, performs one journaled append, checkpoints."""
    from sonder_runtime.application.execution import effect_journal

    target = root / "workspace" / "append.txt"
    trace = root / "runner-trace.log"

    def bind(request, _context):
        def run(state, save, _control):
            binding = effect_journal.current()
            assert binding is not None, "runner must execute under the child journal binding"
            if int(state.get("step", 0)) == 0:
                save({"step": 1}, "before-write")
            settled = effect_journal.settled_receipt(WRITE_KEY)
            if settled is None:
                intent = binding.begin_request(
                    operation_id="append-once", idempotency_key=WRITE_KEY,
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
                if crash_after_write:
                    # Receipt committed; the next checkpoint was never saved.
                    os._exit(CRASH_EXIT)
                settled = "append.txt:1"
            else:
                with trace.open("a", encoding="utf-8") as handle:
                    handle.write(f"consumed {settled}\n")
            save({"step": 2, "write_receipt": settled}, "after-write")
            return "resumed output"

        return run

    return lambda *_args: bind


def _delegate(application, root: Path):
    from sonder_runtime.application.agents.lineage_delegation import (
        DelegationRequest,
        LineageRecord,
        WorkspaceAssignment,
    )
    from sonder_runtime.application.agents.presets import resolve_preset
    from sonder_runtime.application.context import local_owner_context

    workspace = root / "workspace"
    delegation = application.delegation_service()
    context = local_owner_context(correlation_id="wiring-resume-op", workspace_roots=(workspace,))
    root_id = delegation.root_id_for_context(context)
    preset = resolve_preset("researcher")
    assignment = WorkspaceAssignment((str(workspace),))
    lineage = LineageRecord(
        "wiring-resume-lineage", root_id, root_id, CHILD, 1, preset.name, preset.role, assignment,
    )
    request = DelegationRequest(DELEGATION, lineage, "append once", preset, assignment)
    return delegation.dispatch(request, context)


def _crashing_owner(root: Path) -> None:
    from sonder_runtime.adapters import conversational_subagents
    from sonder_runtime.bootstrap.app import build_application

    conversational_subagents.conversational_runner_factory = _runner_factory(
        root, crash_after_write=True,
    )
    application = build_application(config=_config(root))
    _delegate(application, root).result(timeout=60)
    os._exit(4)  # The crash cut inside the runner must have fired.


def _crash(root: Path) -> None:
    (root / "workspace").mkdir(parents=True)
    repo_root = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(filter(None, (str(repo_root), os.environ.get("PYTHONPATH")))),
    }
    crashed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--crash-owner", str(root)],
        cwd=repo_root, env=environment, capture_output=True, text=True,
        timeout=120, check=False,
    )
    assert crashed.returncode == CRASH_EXIT, (crashed.returncode, crashed.stderr[-3000:])


def _compose(root: Path, monkeypatch):
    from sonder_runtime.adapters import conversational_subagents
    from sonder_runtime.bootstrap.app import build_application

    monkeypatch.setattr(
        conversational_subagents, "conversational_runner_factory",
        _runner_factory(root, crash_after_write=False),
    )
    return build_application(config=_config(root))


@pytest.mark.skipif(os.name != "posix", reason="real POSIX process crash cut")
def test_crashed_child_resumes_from_stamped_checkpoint_and_consumes_settled_write(tmp_path, monkeypatch):
    from sonder_runtime.adapters.persistence.sqlite.effect_journal import SQLiteEffectJournal
    from sonder_runtime.application.execution.effect_journal import EffectState
    from sonder_runtime.application.ports.subagents import SubagentStatus

    _crash(tmp_path)
    target = tmp_path / "workspace" / "append.txt"
    assert target.read_text(encoding="utf-8") == "x"
    journal = SQLiteEffectJournal(tmp_path / "state" / "worker-effects.db")
    run_id = f"subagent:{CHILD}"
    before = journal.effects_since(run_id, 0).records
    assert [(r.operation_id, r.state) for r in before] == [
        (f"subagent-dispatch:{CHILD}", EffectState.COMPLETED),
        ("append-once", EffectState.COMPLETED),
    ]

    application = _compose(tmp_path, monkeypatch)
    try:
        repository = application.delegation_service()._provider._local_service._repository
        crashed = repository.get(CHILD)
        assert crashed.status is SubagentStatus.RUNNING
        # The production save path stamped the checkpoint (#515 provenance).
        provenance = crashed.checkpoint.provenance
        assert provenance is not None and provenance.digest_valid
        assert (provenance.run_id, provenance.settled_position) == (run_id, 1)
        assert provenance.worker_id.startswith("subagent:")

        result = _delegate(application, tmp_path).result(timeout=60)
        assert result.status is SubagentStatus.SUCCEEDED, result
        assert result.output == "resumed output"
        final = repository.get(CHILD)
    finally:
        application.close_delegation(timeout=10)

    # The append was consumed from its settled receipt, never repeated.
    assert target.read_text(encoding="utf-8") == "x"
    assert (tmp_path / "runner-trace.log").read_text(encoding="utf-8").splitlines() == [
        "wrote", "consumed append.txt:1",
    ]
    after = journal.effects_since(run_id, 0).records
    assert [r.operation_id for r in after] == [f"subagent-dispatch:{CHILD}", "append-once"]
    assert final.checkpoint.state == {"step": 2, "write_receipt": "append.txt:1"}
    assert final.checkpoint.provenance is not None
    assert final.checkpoint.provenance.owner_epoch > provenance.owner_epoch


@pytest.mark.skipif(os.name != "posix", reason="real POSIX process crash cut")
def test_resume_refuses_with_typed_reason_when_journal_identity_is_swapped(tmp_path, monkeypatch):
    import sqlite3

    from sonder_runtime.application.ports.subagents import SubagentStatus
    from sonder_runtime.application.subagents.checkpoint_provenance import (
        CheckpointResumeRefusal,
        ChildResumeRefused,
    )

    _crash(tmp_path)
    # A different journal identity: the checkpoint was stamped against the
    # original file, so it can no longer authorize continuation.
    with sqlite3.connect(tmp_path / "state" / "worker-effects.db") as connection:
        connection.execute("UPDATE effect_journal_identity SET identity='journal-swapped'")

    application = _compose(tmp_path, monkeypatch)
    try:
        with pytest.raises(ChildResumeRefused) as refused:
            _delegate(application, tmp_path)
        assert refused.value.reason is CheckpointResumeRefusal.JOURNAL_IDENTITY_MISMATCH
        assert refused.value.recovery_required is True
        repository = application.delegation_service()._provider._local_service._repository
        child = repository.get(CHILD)
        assert child.status is SubagentStatus.FAILED and child.recovery_required
    finally:
        application.close_delegation(timeout=10)
    assert (tmp_path / "workspace" / "append.txt").read_text(encoding="utf-8") == "x"
    assert (tmp_path / "runner-trace.log").read_text(encoding="utf-8").splitlines() == ["wrote"]


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--crash-owner":
    _crashing_owner(Path(sys.argv[2]))
