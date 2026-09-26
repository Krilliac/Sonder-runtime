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

The node-shared process and compute job runs are claimed by the provider
constructor, not by the startup pass.  A peer that lazily composes those
providers while another live process owns the runs is refused with
``PeerWorkerLive`` before any owner row or intent is touched; once the owner
exits (cleanly or killed) the same lazy composition claims the runs and
routes the dead owner's orphan through verifier reconciliation.
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
SHARED_RUNS = ("runtime:process-jobs", "runtime:compute-jobs")


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


def _shared_run_owner(root: Path) -> None:
    """Compose both node-shared workers and keep one intent in flight in each."""
    from sonder_runtime.bootstrap.app import build_application

    application = build_application(config=_config(root))
    compute = application.compute_job_worker()
    process = application.process_job_provider()
    admitted = []
    for binding in (process._effect_binding, compute._effect_binding):
        journal_binding = binding.binding()
        admitted.append((journal_binding, journal_binding.begin_request(
            operation_id="peer-effect:shared", idempotency_key="peer-shared",
            request_digest="c" * 64, reconciliation="manual",
        )))
    print("admitted", flush=True)
    if sys.stdin.readline().strip() != "go":
        os._exit(4)
    try:
        for journal_binding, intent in admitted:
            journal_binding.complete(intent, outcome_digest="d" * 64, receipt_key="peer-receipt")
        # The owner still admits and settles new work in the shared run.
        journal_binding = process._effect_binding.binding()
        later = journal_binding.begin_request(
            operation_id="peer-effect:later", idempotency_key="peer-later",
            request_digest="e" * 64, reconciliation="manual",
        )
        journal_binding.complete(later, outcome_digest="f" * 64, receipt_key="peer-later")
    except Exception as exc:  # noqa: BLE001 - reported to the parent test
        print(f"receipt-refused {type(exc).__name__}: {exc}", flush=True)
        os._exit(3)
    print("receipt-committed", flush=True)
    os._exit(0)


def _spawn(mode: str, root: Path) -> subprocess.Popen:
    repo_root = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(filter(None, (str(repo_root), os.environ.get("PYTHONPATH")))),
    }
    return subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), mode, str(root)],
        cwd=repo_root, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )


def _owner_rows(database: Path) -> list[tuple]:
    with sqlite3.connect(database) as connection:
        return connection.execute(
            "SELECT run_id,worker_id,owner_epoch,recovery_required FROM effect_owner "
            "WHERE run_id IN (?,?) ORDER BY run_id", SHARED_RUNS,
        ).fetchall()


def _intent_states(database: Path) -> dict[str, str]:
    with sqlite3.connect(database) as connection:
        return dict(connection.execute(
            "SELECT intent_id,state FROM effect_journal WHERE run_id IN (?,?)", SHARED_RUNS,
        ).fetchall())


@pytest.mark.skipif(os.name != "posix", reason="real peer process")
def test_lazy_shared_worker_composition_refuses_while_a_peer_owns_the_run(tmp_path):
    from sonder_runtime.application.execution.worker_bindings import PeerWorkerLive
    from sonder_runtime.bootstrap.app import build_application

    (tmp_path / "workspace").mkdir()
    peer = _spawn("--shared-run-owner", tmp_path)
    try:
        line = peer.stdout.readline()
        assert line.strip() == "admitted", (line, peer.poll(), peer.stderr.read())
        database = tmp_path / "state" / "worker-effects.db"
        owners_before = _owner_rows(database)
        states_before = _intent_states(database)
        assert [row[0] for row in owners_before] == sorted(SHARED_RUNS)
        assert set(states_before.values()) == {"intent"}

        application = build_application(config=_config(tmp_path))
        with pytest.raises(PeerWorkerLive) as refused:
            application.process_job_provider()
        assert refused.value.run_id == "runtime:process-jobs"
        # Compute composes the process provider first; both stay refused.
        with pytest.raises(PeerWorkerLive):
            application.compute_job_worker()
        assert _owner_rows(database) == owners_before
        assert _intent_states(database) == states_before
        events = [
            row for row in application.events.recent_events(limit=256)
            if "worker.effects.peer_owned" in str(row)
        ]
        assert events and "runtime:process-jobs" in str(events[-1])

        # The owner still settles its in-flight effects and admits new work.
        output, errors = peer.communicate("go\n", timeout=60)
        assert peer.returncode == 0, (output, errors)
        assert "receipt-committed" in output
        assert set(_intent_states(database).values()) == {"completed"}
        assert _owner_rows(database) == owners_before

        # With the owner gone the same lazy composition claims both runs.
        provider = application.process_job_provider()
        worker = application.compute_job_worker()
        epoch = provider._effect_binding.owner_epoch
        assert worker._effect_binding.owner_epoch == epoch
        assert {row[2] for row in _owner_rows(database)} == {epoch}
        assert epoch > max(row[2] for row in owners_before)
    finally:
        if peer.poll() is None:
            peer.kill()
            peer.wait(timeout=30)


@pytest.mark.skipif(os.name != "posix", reason="real peer process")
def test_killed_shared_run_owner_is_reconciled_by_the_next_lazy_composition(tmp_path):
    from sonder_runtime.application.execution.worker_bindings import (
        EffectRecoveryRequired,
        PeerWorkerLive,
    )
    from sonder_runtime.bootstrap.app import build_application

    (tmp_path / "workspace").mkdir()
    peer = _spawn("--shared-run-owner", tmp_path)
    try:
        line = peer.stdout.readline()
        assert line.strip() == "admitted", (line, peer.poll(), peer.stderr.read())
        application = build_application(config=_config(tmp_path))
        with pytest.raises(PeerWorkerLive):
            application.process_job_provider()
    finally:
        peer.kill()
        peer.wait(timeout=30)
    database = tmp_path / "state" / "worker-effects.db"
    owners_before = _owner_rows(database)

    # The kernel released the dead owner's run lease.  The next composition
    # claims the run and offers the orphan to the trusted verifiers; no
    # verifier can prove a manual effect, so it stays fenced (fail closed).
    with pytest.raises(EffectRecoveryRequired) as fenced:
        application.process_job_provider()
    report = fenced.value.report
    assert report.run_id == "runtime:process-jobs"
    assert [item.intent_id for item in report.fenced] == [
        "runtime:process-jobs:peer-effect:shared",
    ]
    rows = {row[0]: row for row in _owner_rows(database)}
    before = {row[0]: row for row in owners_before}
    assert rows["runtime:process-jobs"][2] > before["runtime:process-jobs"][2]
    assert rows["runtime:process-jobs"][3] == 1
    states = _intent_states(database)
    assert states["runtime:process-jobs:peer-effect:shared"] == "uncertain"
    # The compute run was not claimed by the refused process composition.
    assert rows["runtime:compute-jobs"] == before["runtime:compute-jobs"]
    assert states["runtime:compute-jobs:peer-effect:shared"] == "intent"


def test_startup_pass_claims_a_node_shared_run_only_through_its_guard(tmp_path):
    from sonder_runtime.adapters.persistence.sqlite.effect_journal import SQLiteEffectJournal
    from sonder_runtime.application.execution.effect_journal import EffectState
    from sonder_runtime.application.execution.effect_reconciliation import (
        reconcile_unresolved_effects,
    )
    from sonder_runtime.application.execution.worker_bindings import (
        AuthenticatedWorkerBinding,
        PeerWorkerLive,
    )

    journal = SQLiteEffectJournal(tmp_path / "worker-effects.db")
    run_id, worker = SHARED_RUNS[0], "process:n1"
    owner = AuthenticatedWorkerBinding(journal, run_id, worker, 1, "process-jobs")
    owner.recover_before_restart()
    intent = owner.binding().begin_request(
        operation_id="peer-effect:guarded", idempotency_key="guarded",
        request_digest="c" * 64, reconciliation="manual",
    )
    database = tmp_path / "worker-effects.db"
    before = _owner_rows(database)
    guarded: list[tuple[str, str]] = []

    def refuse(run: str, worker_id: str) -> None:
        guarded.append((run, worker_id))
        raise PeerWorkerLive(run, worker_id)

    report = reconcile_unresolved_effects(
        journal, owner_epoch=2, owns_worker={worker}.__contains__, claim_guard=refuse,
    )
    assert guarded == [(run_id, worker)]
    assert report.failed_runs == ((run_id, "PeerWorkerLive"),) and report.runs == ()
    assert _owner_rows(database) == before
    assert journal.get(intent.intent_id).state is EffectState.INTENT

    # A guard that grants the lease lets the same pass claim and reconcile.
    report = reconcile_unresolved_effects(
        journal, owner_epoch=2, owns_worker={worker}.__contains__,
        claim_guard=lambda _run, _worker: None,
    )
    assert [item.intent_id for item in report.fenced] == [intent.intent_id]
    assert [row[2] for row in _owner_rows(database)] == [2]


def test_run_lease_is_shared_in_process_and_refused_to_a_live_peer(tmp_path):
    from sonder_runtime.adapters.persistence.worker_effect_hosts import acquire_run_lease

    journal = tmp_path / "worker-effects.db"
    assert acquire_run_lease(journal, SHARED_RUNS[0], "process:n1") is True
    assert acquire_run_lease(journal, SHARED_RUNS[0], "process:n1") is True
    probe = (
        "import sys; from sonder_runtime.adapters.persistence.worker_effect_hosts "
        "import acquire_run_lease as a; "
        "print(a(sys.argv[1], sys.argv[2], 'process:n1'), a(sys.argv[1], sys.argv[3], 'process:n1'))"
    )
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", probe, str(journal), SHARED_RUNS[0], SHARED_RUNS[1]],
        cwd=repo_root, capture_output=True, text=True, timeout=60,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            filter(None, (str(repo_root), os.environ.get("PYTHONPATH"))))},
    )
    assert result.returncode == 0, result.stderr
    # The held run is refused; an unrelated run is free.
    assert result.stdout.split() == ["False", "True"]


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--live-peer":
    _live_peer(Path(sys.argv[2]))
if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--shared-run-owner":
    _shared_run_owner(Path(sys.argv[2]))
