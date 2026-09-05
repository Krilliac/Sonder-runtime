import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("case", ["repeated", "checkpoint-file", "checkpoint-memory"])
def test_managed_real_store_scopes_converge(tmp_path, case):
    script = r"""
import sys, threading
from pathlib import Path
from sonder_runtime.adapters.persistence.owned_sqlite import OwnedSQLiteConnections, install_disposable_owner
root = Path(sys.argv[1]); case = sys.argv[2]
owner = OwnedSQLiteConnections((root,), max_connections=4)
install_disposable_owner(owner)
if case == 'repeated':
 from sonder_runtime.adapters.persistence.sqlite.extensions import SQLiteExtensionStateRepository
 from sonder_runtime.adapters.persistence.sqlite.loop_state import SQLiteLoopStateRepository, SQLiteRetryEvidenceLedger
 from sonder_runtime.adapters.persistence.sqlite.cas import SQLiteOutboxCASRepository
 from sonder_runtime.adapters.persistence.sqlite.cross_domain import SQLiteCrossDomainCoordinator
 from sonder_runtime.application.persistence.cross_domain import CrossDomainWrite, CoordinationRevisionConflict
 from sonder_runtime.application.persistence.outbox_cas import OutboxEvent, TransactionNeutralRecord
 from sonder_runtime.domain.loop_retry_policy import retry_decision
 extensions = SQLiteExtensionStateRepository(root / 'extensions.db')
 loop = SQLiteLoopStateRepository(root / 'loop.db')
 retry = SQLiteRetryEvidenceLedger(root / 'retry.db')
 cas = SQLiteOutboxCASRepository(root / 'cas.db', namespace='test')
 coordinator = SQLiteCrossDomainCoordinator(root / 'cross.db')
 for index in range(12):
  extensions.save(())
  assert extensions.load() == ()
  assert loop.get('missing') is None
  assert loop.outbox() == ()
  assert cas.get('missing') is None
  assert cas.outbox() == ()
  retry.record('op', retry_decision('timeout', attempt=1, max_attempts=3), attempt=1, failure_code='timeout')
  assert retry.snapshot()
  record = TransactionNeutralRecord('one', index, {'state': 'ok'})
  event = OutboxEvent('event-' + str(index), 'one', 'changed', index, {}, 'now')
  write = CrossDomainWrite('memory', record, event, expected_revision=index-1)
  assert coordinator.coordinate('op-' + str(index), (write,)).committed
  assert coordinator.coordinate('op-' + str(index), (write,)).replayed
  bad_record = TransactionNeutralRecord('one', index+2, {})
  bad_event = OutboxEvent('bad-event-' + str(index), 'one', 'changed', index+2, {}, 'now')
  bad = CrossDomainWrite('memory', bad_record, bad_event, expected_revision=index+1)
  try:
   coordinator.coordinate('bad-' + str(index), (bad,))
  except CoordinationRevisionConflict:
   pass
  else:
   raise AssertionError('expected conflict')
  assert owner.snapshot().clean, owner.snapshot()
else:
 from sonder_runtime.adapters.persistence.checkpoint_store import CheckpointStore
 from sonder_runtime.domain.automation.checkpoint import Checkpoint
 store = CheckpointStore(':memory:' if case == 'checkpoint-memory' else root / 'checkpoints.db')
 errors=[]
 def worker():
  try:
   store.save(Checkpoint(session_id='one', step_index=3, status='running'))
   assert store.latest('one').step_index == 3
   assert owner.snapshot().clean
  except BaseException as error:
   errors.append(type(error).__name__ + ':' + str(error))
 thread=threading.Thread(target=worker); thread.start(); thread.join(5)
 assert not thread.is_alive()
 assert errors == [], errors
 assert store.latest('one').step_index == 3
 store.close()
 assert owner.snapshot().clean, owner.snapshot()
print('closed')
"""
    environment = {key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"}}
    environment["SONDER_HOME"] = str(tmp_path)
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path), case], cwd=Path(__file__).resolve().parents[1], env=environment, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr[-3000:]
    assert result.stdout.strip() == "closed"


@pytest.mark.parametrize("memory", [False, True])
def test_checkpoint_failed_write_rolls_back_and_closes(tmp_path, monkeypatch, memory):
    from sonder_runtime.adapters.persistence.checkpoint_store import CheckpointStore
    from sonder_runtime.domain.automation.checkpoint import Checkpoint
    store = CheckpointStore(":memory:" if memory else tmp_path / "checkpoints.db")
    original = Checkpoint(session_id="one", step_index=1, status="running")
    store.save(original)
    def fail(*args):
        raise RuntimeError("fixture after write")
    monkeypatch.setattr(store, "_prune", fail)
    with pytest.raises(RuntimeError, match="fixture"):
        store.save(Checkpoint(session_id="one", step_index=2, status="running"))
    assert store.latest("one").checkpoint_id == original.checkpoint_id
    store.close()
