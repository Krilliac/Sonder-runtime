import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("case", ["repeated", "checkpoint-file", "checkpoint-memory", "spill", "terminal", "workflow", "lanes"])
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
elif case == 'spill':
 from sonder_runtime.adapters.execution.durable_output import SQLiteSpillStore
 from sonder_runtime.application.ports.artifact_store import SpillSpec
 store = SQLiteSpillStore(root / 'spill.db')
 for index in range(12):
  handle = store.begin(SpillSpec(16))
  assert handle.write(b'hello') == 5
  try:
   handle.write(b'x' * 17)
  except ValueError:
   pass
  else:
   raise AssertionError('write bound')
  assert handle.snapshot().size_bytes == 5
  artifact = handle.commit()
  try:
   handle.commit()
  except ValueError:
   pass
  else:
   raise AssertionError('spill commit is one-shot')
  assert handle.snapshot().size_bytes == 5
  assert store.read(artifact, max_bytes=16) == b'hello'
  assert SQLiteSpillStore(root / 'spill.db').read(artifact, max_bytes=16) == b'hello'
  handle.close()
  assert owner.snapshot().clean, owner.snapshot()
elif case == 'terminal':
 import sqlite3
 from sonder_runtime.adapters.execution.persistent_terminal import _SQLiteTerminalJournal
 store = _SQLiteTerminalJournal(root / 'terminal.db', max_events=32, max_bytes=4096)
 store.create('term', 'world', 80, 24, 1)
 for index in range(12):
  assert store.append('term', 'stdout', 'hello') == index+1
  assert store.session('term')[0] == 'term'
  assert store.page('term', None, max_events=32, max_bytes=4096).events
  try:
   store.create('term', 'world', 80, 24, 1)
  except sqlite3.IntegrityError:
   pass
  else:
   raise AssertionError('duplicate session')
  assert owner.snapshot().clean, owner.snapshot()
 store.mark_stopped('term')
 assert _SQLiteTerminalJournal(root / 'terminal.db', max_events=32, max_bytes=4096).session('term')[2] == 'stopped'
elif case == 'workflow':
 from sonder_runtime.adapters.persistence.sqlite.workflow_checkpoints import SQLiteWorkflowCheckpointRepository
 from sonder_runtime.application.ports.jobs import WorkflowCheckpoint
 store = SQLiteWorkflowCheckpointRepository(root / 'workflow.db')
 for index in range(12):
  checkpoint = WorkflowCheckpoint('job', index, index, {'step': index})
  assert store.save_checkpoint(checkpoint, expected_sequence=index-1) == checkpoint
  assert store.save_checkpoint(checkpoint, expected_sequence=index-1) is None
  assert store.get_checkpoint('job') == checkpoint
  assert SQLiteWorkflowCheckpointRepository(root / 'workflow.db').get_checkpoint('job') == checkpoint
  assert owner.snapshot().clean, owner.snapshot()
elif case == 'lanes':
 from sonder_runtime.adapters.persistence.agent_lanes import SQLiteAgentLaneStore
 store = SQLiteAgentLaneStore(root / 'fleet.db', None)
 for index in range(12):
  store.validate_parent_grant('absent', 'principal')
  store.events('absent', 0, 2)
  store.flush()
  try:
   store.read_lane('absent')
  except KeyError:
   pass
  else:
   raise AssertionError('missing lane')
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
if case in {'spill', 'terminal', 'workflow', 'lanes'}:
 scope = store._connection_scope if case == 'lanes' else store._connect
 with scope() as connection:
  connection.execute('CREATE TABLE scope_rollback_probe(value INTEGER)')
  connection.execute('INSERT INTO scope_rollback_probe VALUES(1)')
 for index in range(12):
  try:
   with scope() as connection:
    connection.execute('INSERT INTO scope_rollback_probe VALUES(2)')
    raise RuntimeError('failure after actual write')
  except RuntimeError:
   pass
  with scope() as connection:
   assert [row[0] for row in connection.execute('SELECT value FROM scope_rollback_probe')] == [1]
  assert owner.snapshot().clean, owner.snapshot()
if case == 'lanes':
 import sqlite3
 import sonder_runtime.adapters.persistence.agent_lanes as lanes
 original = lanes.owned_sqlite_connect
 def deny_pragma(*args, **kwargs):
  connection = original(*args, **kwargs)
  connection.set_authorizer(lambda action, *rest: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_PRAGMA else sqlite3.SQLITE_OK)
  return connection
 lanes.owned_sqlite_connect = deny_pragma
 try:
  store.connect()
 except sqlite3.DatabaseError:
  pass
 else:
  raise AssertionError('expected initialization refusal')
 finally:
  lanes.owned_sqlite_connect = original
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
