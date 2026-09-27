"""Startup reconciliation of unresolved worker effects through composition (#515).

A real child interpreter composes the application, leaves one unprovable
selfmod intent and one foreign-worker intent in the production
worker-effects journal, and dies with ``os._exit`` after a delegated child was
durably admitted but before the ``subagent-dispatch`` receipt committed.  The
next ``build_application`` must, on its own:

* resolve the provable dispatch intent from the durable child store (durable
  ``verified:`` receipt, no runner started);
* leave the unprovable selfmod intent ``uncertain`` and its run fenced;
* leave the foreign worker's intent untouched;
* emit a content-free ``worker.effects.reconciled`` operations event;
* be re-entrant: a second pass changes nothing and keeps the fence.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

CRASH_EXIT = 92
CHILD = "wiring-startup-child"
SELFMOD_RUN = "selfmod:wiring-unprovable"
FOREIGN_RUN = "runtime:process-jobs-foreign"


def _config(root: Path):
    from sonder_runtime.platform.config import SonderConfig

    config = SonderConfig()
    return replace(config, state=replace(
        config.state, home=str(root / "state"), workspace_roots=(str(root / "workspace"),),
    ))


def _crashing_owner(root: Path) -> None:
    from sonder_runtime.adapters import conversational_subagents
    from sonder_runtime.adapters.persistence.sqlite.effect_journal import SQLiteEffectJournal
    from sonder_runtime.application.execution.effect_journal import EffectIntent
    from sonder_runtime.bootstrap.app import build_application

    ran = root / "runner-ran"
    conversational_subagents.conversational_runner_factory = (
        lambda *_: lambda request, context: lambda state, save, control: (
            ran.write_text("ran") or "should not run"
        )
    )
    config = _config(root)
    application = build_application(config=config)
    journal = application.process_job_provider()._effect_binding.journal
    node = config.compute.node_id
    # A selfmod deploy admitted by this host's worker identity; no verifier
    # exists for that family, so it can never be proven.
    journal.begin(EffectIntent(
        f"{SELFMOD_RUN}:selfmod-deploy:wiring-unprovable", SELFMOD_RUN,
        f"selfmod:{node}", "selfmod-deploy:wiring-unprovable", "selfmod-mutation",
        1, "deploy-wiring-unprovable", "a" * 64, "manual",
    ))
    # An intent admitted by a worker identity another host owns.
    journal.begin(EffectIntent(
        f"{FOREIGN_RUN}:process-start:foreign", FOREIGN_RUN, "process:other-node",
        "process-start:foreign", "process-jobs", 1, "foreign-start", "b" * 64, "idempotent",
    ))
    original = SQLiteEffectJournal.outcome_and_checkpoint

    def crash_before_dispatch_receipt(self, outcome, state):
        if outcome.intent_id == f"subagent:{CHILD}:subagent-dispatch:{CHILD}":
            os._exit(CRASH_EXIT)  # child admitted durably; receipt never committed
        return original(self, outcome, state)

    SQLiteEffectJournal.outcome_and_checkpoint = crash_before_dispatch_receipt
    _dispatch(application, root)
    os._exit(4)  # The crash cut must have fired inside dispatch.


def _dispatch(application, root: Path):
    from sonder_runtime.application.agents.lineage_delegation import (
        DelegationRequest,
        LineageRecord,
        WorkspaceAssignment,
    )
    from sonder_runtime.application.agents.presets import resolve_preset
    from sonder_runtime.application.context import local_owner_context

    workspace = root / "workspace"
    delegation = application.delegation_service()
    context = local_owner_context(correlation_id="wiring-startup-op", workspace_roots=(workspace,))
    root_id = delegation.root_id_for_context(context)
    preset = resolve_preset("researcher")
    assignment = WorkspaceAssignment((str(workspace),))
    lineage = LineageRecord(
        "wiring-startup-lineage", root_id, root_id, CHILD, 1, preset.name, preset.role, assignment,
    )
    return delegation.dispatch(
        DelegationRequest("wiring-startup-delegation", lineage, "inspect", preset, assignment),
        context,
    )


@pytest.mark.skipif(os.name != "posix", reason="real POSIX process crash cut")
def test_startup_resolves_provable_intent_and_keeps_unprovable_fenced(tmp_path, monkeypatch):
    import sqlite3

    from sonder_runtime.adapters import conversational_subagents
    from sonder_runtime.adapters.persistence.sqlite.effect_journal import SQLiteEffectJournal
    from sonder_runtime.application.execution.effect_journal import (
        EffectJournalError,
        EffectState,
    )
    from sonder_runtime.application.execution.worker_bindings import AuthenticatedWorkerBinding
    from sonder_runtime.application.ports.subagents import SubagentStatus
    from sonder_runtime.bootstrap.app import build_application

    (tmp_path / "workspace").mkdir()
    repo_root = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(filter(None, (str(repo_root), os.environ.get("PYTHONPATH")))),
    }
    crashed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--crash-owner", str(tmp_path)],
        cwd=repo_root, env=environment, capture_output=True, text=True,
        timeout=120, check=False,
    )
    assert crashed.returncode == CRASH_EXIT, (crashed.returncode, crashed.stderr[-3000:])
    database = tmp_path / "state" / "worker-effects.db"
    dispatch_id = f"subagent:{CHILD}:subagent-dispatch:{CHILD}"
    selfmod_id = f"{SELFMOD_RUN}:selfmod-deploy:wiring-unprovable"
    foreign_id = f"{FOREIGN_RUN}:process-start:foreign"
    raw = SQLiteEffectJournal(database)
    assert raw.get(dispatch_id).state is EffectState.INTENT
    assert raw.get(selfmod_id).state is EffectState.INTENT

    monkeypatch.setattr(
        conversational_subagents, "conversational_runner_factory",
        lambda *_: lambda request, context: lambda state, save, control: "unused",
    )
    application = build_application(config=_config(tmp_path))
    try:
        journal = SQLiteEffectJournal(database)
        dispatch = journal.get(dispatch_id)
        assert dispatch.state is EffectState.COMPLETED
        assert dispatch.receipt_key.startswith(f"subagent-dispatch:{CHILD}:")
        assert dispatch.detail.startswith("verified:durable-subagent-dispatch-v1:child-store:")
        selfmod = journal.get(selfmod_id)
        assert selfmod.state is EffectState.UNCERTAIN
        assert journal.get(foreign_id).state is EffectState.INTENT
        with sqlite3.connect(database) as connection:
            fence = connection.execute(
                "SELECT recovery_required FROM effect_owner WHERE run_id=?", (SELFMOD_RUN,),
            ).fetchone()
        assert fence == (1,)
        # Re-entrant: a second pass resolves nothing new and keeps the fence.
        again = application.worker_effect_reconciliation()
        assert [item.intent_id for item in again.resolved] == []
        assert selfmod_id in [item.intent_id for item in again.fenced]
        assert FOREIGN_RUN in again.foreign_runs
        assert journal.get(dispatch_id).state is EffectState.COMPLETED
        assert journal.get(selfmod_id).state is EffectState.UNCERTAIN
        # The fenced run admits nothing new, even for this host's newest epoch.
        successor = AuthenticatedWorkerBinding(
            journal, SELFMOD_RUN, selfmod.worker_id, 2 ** 62, "selfmod-mutation",
            auto_reconcile=True,
        )
        with pytest.raises(EffectJournalError, match="reconciliation"):
            successor.recover_before_restart()
        # Reconciliation never started the admitted child's runner.
        assert not (tmp_path / "runner-ran").exists()
        child = application.delegation_service()._provider._local_service._repository.get(CHILD)
        assert child is not None and child.status is SubagentStatus.RUNNING

        events = [
            row for row in application.events.recent_events(limit=256)
            if "worker.effects.reconciled" in str(row)
        ]
        assert events, "startup reconciliation must emit an operations event"

        # The proven admission lets the exact delegation resume the child
        # (it never checkpointed and made no journaled progress), without a
        # second dispatch intent.
        resumed = _dispatch(application, tmp_path).result(timeout=30)
        assert resumed.status is SubagentStatus.SUCCEEDED and resumed.output == "unused"
        assert [r.intent_id for r in journal.effects_since(f"subagent:{CHILD}", 0).records] == [
            dispatch_id,
        ]
    finally:
        application.close_delegation(timeout=10)


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--crash-owner":
    _crashing_owner(Path(sys.argv[2]))
