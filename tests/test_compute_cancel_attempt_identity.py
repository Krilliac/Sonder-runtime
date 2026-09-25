"""Per-attempt journal identity for compute cancellation (#515, LOOP-008).

A cancel whose provider reports cleanup still pending publishes a settled
``cancellation_requested`` receipt.  A later cancel of the same job is a
legitimate retry, so it must be admitted as a new attempt with its own
journal identity.  The attempt number is derived from durable journal state,
never from caller input or an in-memory counter, and a retry is refused while
any prior attempt for that job is unresolved (``intent``/``uncertain``).
"""
from __future__ import annotations

import inspect
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:  # child-interpreter entry point below
    sys.path.insert(0, str(REPO))

from sonder_runtime.adapters.persistence.sqlite.effect_journal import (
    SQLiteEffectJournal,
)
from sonder_runtime.application.compute_fabric.jobs import (
    ComputeJobWorker,
)
from sonder_runtime.application.execution.effect_journal import (
    EffectJournalError,
    EffectState,
)
from sonder_runtime.application.execution.worker_bindings import (
    AuthenticatedWorkerBinding,
)
from sonder_runtime.application.ports.jobs import JobRecord, JobStatus
from tests.test_compute_job_worker import (
    CapturingProvider,
    _entry,
    _envelope,
)

RUN_ID = "runtime:compute-jobs"
BINDING_WORKER = "compute:attempt-node"
SCOPE = "compute-jobs"
WORKER_ID = "compute-worker"
CRASH_EXIT = 87


class CleanupProvider(CapturingProvider):
    """Provider double whose cancel reports cleanup per a scripted sequence."""

    def __init__(self, cleanup: tuple[bool, ...] = (False,)) -> None:
        super().__init__()
        self.cleanup = list(cleanup)
        self.cancel_calls: list[tuple[str, str]] = []
        self.before_cancel = lambda: None
        self._calls_lock = threading.Lock()

    def cancel(self, job_id, reason="cancelled"):
        with self._calls_lock:
            self.cancel_calls.append((job_id, reason))
            index = len(self.cancel_calls) - 1
        self.before_cancel()
        complete = self.cleanup[index] if index < len(self.cleanup) else False
        return {"cleanup_completed": complete}

    def recover(self, *, kind_prefix, limit):
        """Durable rehydration view so a restarted worker knows the job."""
        if self.request is None:
            return ()
        return (SimpleNamespace(
            record=JobRecord(self.request.identity, status=JobStatus.RUNNING),
            metadata=dict(self.request.metadata),
            process_id=42,
        ),)


@pytest.fixture(autouse=True)
def _isolated_spool(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        ComputeJobWorker, "_artifact_stage_base",
        staticmethod(lambda: tmp_path / "artifact-stages"),
    )


def _worker(journal, root: Path, provider, epoch: int = 1) -> ComputeJobWorker:
    return ComputeJobWorker(
        worker_id=WORKER_ID, catalog={"pytest": _entry()},
        workspace_mappings={"sonder": root}, provider=provider,
        effect_binding=AuthenticatedWorkerBinding(
            journal, RUN_ID, BINDING_WORKER, epoch, SCOPE,
        ),
    )


def _cancel_records(journal, job_id: str):
    page = journal.effects_since(RUN_ID, 0, limit=1000)
    assert not page.truncated
    return [
        record for record in page.records
        if record.operation_id.startswith("compute-cancel:")
        and record.operation_id.rsplit(":", 1)[-1] == job_id
    ]


def test_retry_after_pending_cleanup_is_admitted_as_attempt_two(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    provider = CleanupProvider(cleanup=(False, True))
    worker = _worker(journal, tmp_path, provider)
    job = worker.submit(_envelope()).remote_job_id

    first = worker.cancel(job, "operator cancel")
    assert first.state == "cancellation_requested"
    [attempt_one] = _cancel_records(journal, job)
    assert attempt_one.state is EffectState.COMPLETED
    assert attempt_one.receipt_key == f"{job}:cancellation_requested"
    # Attempt 1 keeps the historical identity byte-for-byte.
    assert attempt_one.operation_id == f"compute-cancel:{WORKER_ID}:{job}"
    assert attempt_one.idempotency_key == f"cancel:{job}"

    second = worker.cancel(job, "operator cancel")
    assert second.state == "cancelled"
    assert len(provider.cancel_calls) == 2
    records = _cancel_records(journal, job)
    assert [record.operation_id for record in records] == [
        f"compute-cancel:{WORKER_ID}:{job}",
        f"compute-cancel:{WORKER_ID}:attempt-2:{job}",
    ]
    assert records[1].idempotency_key == f"cancel:{job}:attempt-2"
    assert records[1].state is EffectState.COMPLETED
    assert records[1].receipt_key == f"{job}:cancelled"
    assert records[0].sequence < records[1].sequence


def test_attempt_numbers_come_from_the_journal_not_the_caller_or_memory(tmp_path):
    # The public surface accepts no attempt number at all.
    assert list(inspect.signature(ComputeJobWorker.cancel).parameters) == [
        "self", "remote_job_id", "reason",
    ]
    db = tmp_path / "effects.db"
    provider = CleanupProvider(cleanup=(False, False, False))
    worker = _worker(SQLiteEffectJournal(db), tmp_path, provider)
    job = worker.submit(_envelope()).remote_job_id
    worker.cancel(job, "first")
    worker.cancel(job, "second")

    # A restarted process (new journal handle, new worker, new epoch) has no
    # in-memory counter; it must still continue at attempt 3.
    reopened = SQLiteEffectJournal(db)
    restarted = _worker(reopened, tmp_path, provider, epoch=2)
    assert restarted.cancel(job, "third").state == "cancellation_requested"
    assert [record.operation_id for record in _cancel_records(reopened, job)] == [
        f"compute-cancel:{WORKER_ID}:{job}",
        f"compute-cancel:{WORKER_ID}:attempt-2:{job}",
        f"compute-cancel:{WORKER_ID}:attempt-3:{job}",
    ]
    assert [call[1] for call in provider.cancel_calls] == ["first", "second", "third"]


def test_retry_after_completed_cancellation_is_refused_without_invoking(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    provider = CleanupProvider(cleanup=(True,))
    worker = _worker(journal, tmp_path, provider)
    job = worker.submit(_envelope()).remote_job_id
    assert worker.cancel(job, "done").state == "cancelled"
    with pytest.raises(EffectJournalError, match="already completed"):
        worker.cancel(job, "again")
    assert len(provider.cancel_calls) == 1
    assert len(_cancel_records(journal, job)) == 1


def test_retry_refused_while_prior_attempt_is_in_flight(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    provider = CleanupProvider(cleanup=(False,))
    worker = _worker(journal, tmp_path, provider)
    job = worker.submit(_envelope()).remote_job_id
    entered, release = threading.Event(), threading.Event()

    def hold() -> None:
        entered.set()
        assert release.wait(10)

    provider.before_cancel = hold
    results: list[object] = []
    first = threading.Thread(target=lambda: results.append(worker.cancel(job, "slow")))
    first.start()
    try:
        assert entered.wait(10)
        [in_flight] = _cancel_records(journal, job)
        assert in_flight.state is EffectState.INTENT
        with pytest.raises(EffectJournalError, match="prior cancellation attempt .* unresolved"):
            worker.cancel(job, "impatient")
    finally:
        release.set()
        first.join(10)
    assert len(provider.cancel_calls) == 1
    assert results and results[0].state == "cancellation_requested"


def test_retry_refused_while_prior_attempt_is_uncertain(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    provider = CleanupProvider()
    worker = _worker(journal, tmp_path, provider)
    job = worker.submit(_envelope()).remote_job_id

    def explode() -> None:
        raise RuntimeError("provider lost the cancel acknowledgement")

    provider.before_cancel = explode
    with pytest.raises(RuntimeError, match="acknowledgement"):
        worker.cancel(job, "first")
    [uncertain] = _cancel_records(journal, job)
    assert uncertain.state is EffectState.UNCERTAIN
    provider.before_cancel = lambda: None
    with pytest.raises(EffectJournalError, match="prior cancellation attempt .* unresolved"):
        worker.cancel(job, "retry")
    assert len(provider.cancel_calls) == 1
    assert len(_cancel_records(journal, job)) == 1


def test_identical_concurrent_retries_admit_at_most_one_new_attempt(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    provider = CleanupProvider(cleanup=(False, False, False))
    worker = _worker(journal, tmp_path, provider)
    job = worker.submit(_envelope()).remote_job_id
    worker.cancel(job, "first")

    release = threading.Event()
    barrier = threading.Barrier(2)
    provider.before_cancel = lambda: release.wait(10)
    outcomes: list[object] = []
    lock = threading.Lock()

    def retry() -> None:
        barrier.wait(10)
        try:
            result: object = worker.cancel(job, "retry")
        except Exception as exc:  # noqa: BLE001 - classified below
            result = exc
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=retry) for _ in range(2)]
    for thread in threads:
        thread.start()
    # One retry is admitted and parks inside the provider; the other must be
    # refused by the journal (unresolved attempt or duplicate identity).
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with lock:
            if outcomes:
                break
        time.sleep(0.01)
    release.set()
    for thread in threads:
        thread.join(10)
    assert len(outcomes) == 2
    refused = [item for item in outcomes if isinstance(item, EffectJournalError)]
    admitted = [item for item in outcomes if not isinstance(item, Exception)]
    assert len(refused) == 1 and len(admitted) == 1, outcomes
    records = _cancel_records(journal, job)
    assert [record.operation_id for record in records] == [
        f"compute-cancel:{WORKER_ID}:{job}",
        f"compute-cancel:{WORKER_ID}:attempt-2:{job}",
    ]
    assert len(provider.cancel_calls) == 2


def _marker(root: Path) -> Path:
    return root / "cancel-effects.log"


def _child(root: Path) -> None:
    """Cancel once with pending cleanup, then crash inside the second attempt."""
    ComputeJobWorker._artifact_stage_base = staticmethod(lambda: root / "artifact-stages")
    journal = SQLiteEffectJournal(root / "effects.db")
    provider = CleanupProvider(cleanup=(False, False))
    worker = _worker(journal, root, provider)
    job = worker.submit(_envelope()).remote_job_id
    (root / "job.txt").write_text(job, encoding="utf-8")
    worker.cancel(job, "first")

    def effect_then_crash() -> None:
        with _marker(root).open("a", encoding="utf-8") as stream:
            stream.write("x")
            stream.flush()
            os.fsync(stream.fileno())
        os._exit(CRASH_EXIT)  # after the provider effect, before the receipt

    provider.before_cancel = effect_then_crash
    worker.cancel(job, "second")
    os._exit(0)  # the crash hook did not fire


def test_crash_between_invoke_and_receipt_refuses_retry_after_reopen(tmp_path):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(REPO), env.get("PYTHONPATH"))))
    env["SONDER_STATE_HOME"] = str(tmp_path / "state")
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(tmp_path)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120, check=False,
    )
    assert completed.returncode == CRASH_EXIT, (completed.returncode, completed.stderr[-4000:])
    assert _marker(tmp_path).read_text(encoding="utf-8") == "x"
    job = (tmp_path / "job.txt").read_text(encoding="utf-8")

    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    first, second = _cancel_records(journal, job)
    assert first.state is EffectState.COMPLETED
    assert second.operation_id == f"compute-cancel:{WORKER_ID}:attempt-2:{job}"
    assert second.state is EffectState.INTENT and second.receipt_key == ""

    provider = CleanupProvider()
    for epoch in (2, 3):
        # Restart refuses; the orphaned attempt becomes explicitly uncertain
        # and stays that way until trusted reconciliation.
        with pytest.raises(EffectJournalError, match="reconciliation"):
            _worker(SQLiteEffectJournal(tmp_path / "effects.db"), tmp_path, provider, epoch)
        assert journal.get(second.intent_id).state is EffectState.UNCERTAIN
    assert provider.cancel_calls == []
    assert _marker(tmp_path).read_text(encoding="utf-8") == "x"
    assert len(_cancel_records(journal, job)) == 2


if __name__ == "__main__":
    _child(Path(sys.argv[1]))
