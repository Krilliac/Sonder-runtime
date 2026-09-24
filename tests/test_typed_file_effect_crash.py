"""A real composed file write cannot be replayed after an unknown worker crash."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import server
from sonder_runtime.adapters.filesystem import file_ops
from sonder_runtime.adapters.persistence.sqlite.effect_journal import (
    SQLiteEffectJournal,
)
from sonder_runtime.adapters.persistence.tool_audit import DurableToolAuditRepository
from sonder_runtime.application.execution.effect_journal import (
    EffectJournalError,
    EffectState,
    bound,
)
from sonder_runtime.application.execution.worker_bindings import (
    AuthenticatedWorkerBinding,
)
from sonder_runtime.bootstrap import app as bootstrap_app
from sonder_runtime.platform import paths as runtime_paths

CRASH_EXIT = 86
RUN_ID = "real-file-effect"
WORKER_ID = "file-worker"


def _composed_worker(root: Path, epoch: int) -> AuthenticatedWorkerBinding:
    workspace = root / "workspace"
    runtime_paths.configure_home(root / "state")
    file_ops.workspace_root = lambda: workspace
    server._maybe_live_reload = lambda: None
    server._APP_GRAPH = bootstrap_app.build_application()
    return AuthenticatedWorkerBinding(
        SQLiteEffectJournal(root / "effects.db"), RUN_ID, WORKER_ID,
        epoch, str(workspace),
    )


def _crash_after_real_file_write(root: Path) -> None:
    binding = _composed_worker(root, 1)
    assert binding.recover_before_restart().action == "resume"
    actual_write = file_ops.write_file

    def write_then_crash(*args, **kwargs):
        actual_write(*args, **kwargs)
        if (root / "workspace" / "append.txt").read_text(encoding="utf-8") != "base|x":
            os._exit(3)
        (root / "cut-after-write").write_text("physical append completed", encoding="utf-8")
        os._exit(CRASH_EXIT)

    file_ops.write_file = write_then_crash
    with bound(binding.binding()):
        server.file_write("append.txt", "|x", mode="append")
    os._exit(4)  # The test cannot pass unless it crossed the actual write.


def test_composed_file_write_crash_refuses_replay_until_reconciliation(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "append.txt"
    target.write_text("base", encoding="utf-8")
    repository = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(filter(None, (
            str(repository), os.environ.get("PYTHONPATH"),
        ))),
        "SONDER_STATE_HOME": str(tmp_path / "state"),
        "SONDER_FLEET_DB": str(tmp_path / "fleet.db"),
        "SONDER_FLEET_PRINCIPAL_FILE": str(tmp_path / "fleet-principal.json"),
    }
    child = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--crash-after-file-write", str(tmp_path)],
        cwd=repository, env=environment, capture_output=True, text=True,
        timeout=30, check=False,
    )
    assert child.returncode == CRASH_EXIT, (child.returncode, child.stderr[-2000:])
    assert (tmp_path / "cut-after-write").read_text(encoding="utf-8") == "physical append completed"
    assert target.read_text(encoding="utf-8") == "base|x"

    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    records = journal.effects_since(RUN_ID, 0).records
    assert len(records) == journal.high_water(RUN_ID) == 1
    assert records[0].state is EffectState.INTENT
    assert records[0].reconciliation == "manual"
    assert DurableToolAuditRepository(
        tmp_path / "state" / "audit" / "tool-receipts.jsonl",
    ).read() == ()
    restarted = AuthenticatedWorkerBinding(journal, RUN_ID, WORKER_ID, 2, str(workspace))

    with pytest.raises(EffectJournalError, match="reconciliation"):
        restarted.recover_before_restart()
    assert journal.get(records[0].intent_id).state is EffectState.UNCERTAIN
    with pytest.raises(EffectJournalError, match="no trusted reconciliation verifier"):
        journal.reconcile(records[0].intent_id, owner_epoch=2)

    # The new owner cannot append again, even though the legacy surface
    # constructs a fresh tool request ID for each attempt.
    previous_home = runtime_paths._configured_home()
    try:
        runtime_paths.configure_home(tmp_path / "state")
        monkeypatch.setattr(file_ops, "workspace_root", lambda: workspace)
        monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
        monkeypatch.setattr(server, "_APP_GRAPH", bootstrap_app.build_application())
        with bound(restarted.binding()):
            refused = server.file_write("append.txt", "|x", mode="append")
        assert refused.startswith("ERROR:") and "reconciliation" in refused
    finally:
        if previous_home is None:
            runtime_paths.reset_home()
        else:
            runtime_paths.configure_home(previous_home)
    assert target.read_text(encoding="utf-8") == "base|x"
    assert journal.high_water(RUN_ID) == 1


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--crash-after-file-write":
    _crash_after_real_file_write(Path(sys.argv[2]))
