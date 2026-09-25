"""Startup reconciliation must not fence a live peer host process (#515).

Two runtime processes on one node share the production worker-effects
journal (for example ``sonder serve`` and an IDE-launched ``sonder mcp``).
Both compose worker identities ``<family>:<node>``, so identity alone cannot
tell a crashed predecessor's intent from a live peer's in-flight effect.  A
real child interpreter composes the application, admits an intent through
the production journal and keeps running.  A second ``build_application`` in
this process must leave that intent and its owner epoch untouched, report the
pass as deferred, and the live peer must still commit its receipt.  Once the
peer exits, the next pass reconciles normally.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

RUN = "subagent:wiring-live-peer"


def _config(root: Path):
    from sonder_runtime.platform.config import SonderConfig

    config = SonderConfig()
    return replace(config, state=replace(
        config.state, home=str(root / "state"), workspace_roots=(str(root / "workspace"),),
    ))


def _live_peer(root: Path) -> None:
    from sonder_runtime.application.execution.worker_bindings import AuthenticatedWorkerBinding
    from sonder_runtime.bootstrap.app import build_application

    config = _config(root)
    application = build_application(config=config)
    provider_binding = application.process_job_provider()._effect_binding
    binding = AuthenticatedWorkerBinding(
        provider_binding.journal, RUN, f"subagent:{config.compute.node_id}",
        provider_binding.owner_epoch, "subagent-dispatch",
    )
    binding.recover_before_restart()
    journal_binding = binding.binding()
    intent = journal_binding.begin_request(
        operation_id="peer-effect:one", idempotency_key="peer-one",
        request_digest="c" * 64, reconciliation="manual",
    )
    print("admitted", flush=True)
    sys.stdin.readline()  # The effect is in flight until the test says so.
    try:
        journal_binding.complete(intent, outcome_digest="d" * 64, receipt_key="peer-receipt")
    except Exception as exc:  # noqa: BLE001 - reported to the parent test
        print(f"receipt-refused {type(exc).__name__}: {exc}", flush=True)
        os._exit(3)
    print("receipt-committed", flush=True)
    os._exit(0)


@pytest.mark.skipif(os.name != "posix", reason="real peer process")
def test_startup_reconciliation_defers_while_a_peer_host_process_is_live(tmp_path):
    from sonder_runtime.adapters.persistence.sqlite.effect_journal import SQLiteEffectJournal
    from sonder_runtime.application.execution.effect_journal import EffectState
    from sonder_runtime.bootstrap.app import build_application

    (tmp_path / "workspace").mkdir()
    repo_root = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(filter(None, (str(repo_root), os.environ.get("PYTHONPATH")))),
    }
    peer = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--live-peer", str(tmp_path)],
        cwd=repo_root, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    try:
        line = peer.stdout.readline()
        assert line.strip() == "admitted", (line, peer.poll())
        database = tmp_path / "state" / "worker-effects.db"
        intent_id = f"{RUN}:peer-effect:one"
        with sqlite3.connect(database) as connection:
            before = connection.execute(
                "SELECT owner_epoch,recovery_required FROM effect_owner WHERE run_id=?", (RUN,),
            ).fetchall()

        application = build_application(config=_config(tmp_path))
        journal = SQLiteEffectJournal(database)
        assert journal.get(intent_id).state is EffectState.INTENT
        with sqlite3.connect(database) as connection:
            after = connection.execute(
                "SELECT owner_epoch,recovery_required FROM effect_owner WHERE run_id=?", (RUN,),
            ).fetchall()
        assert after == before
        again = application.worker_effect_reconciliation()
        assert again.deferred == "live-peer-host-process"
        assert again.runs == () and again.foreign_runs == ()
        events = [
            row for row in application.events.recent_events(limit=256)
            if "worker.effects.reconciled" in str(row)
        ]
        assert events and "live-peer-host-process" in str(events[-1])

        # The live peer still owns its effect and commits the receipt.
        output, _errors = peer.communicate("go\n", timeout=60)
        assert peer.returncode == 0, output
        assert "receipt-committed" in output
        settled = journal.get(intent_id)
        assert settled.state is EffectState.COMPLETED and settled.receipt_key == "peer-receipt"

        # With the peer gone its lease is provably dead and is reaped, and a
        # pass with nothing unresolved claims nothing and is not deferred.
        from sonder_runtime.adapters.persistence.worker_effect_hosts import host_lease

        lease = host_lease(database)
        assert lease.live_peers() == 0
        assert [entry.name for entry in lease.directory.iterdir()] == [lease.name]
        final = application.worker_effect_reconciliation()
        assert final.deferred == "" and final.runs == ()
    finally:
        if peer.poll() is None:
            peer.kill()
            peer.wait(timeout=30)


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--live-peer":
    _live_peer(Path(sys.argv[2]))
