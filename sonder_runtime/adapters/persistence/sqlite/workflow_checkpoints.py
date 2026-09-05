"""SQLite adapter for the typed SEAM-010 workflow checkpoint port."""
from __future__ import annotations

from sonder_runtime.adapters.persistence.owned_sqlite import connect as owned_sqlite_connect

import json
from pathlib import Path
import sqlite3
from threading import Lock

from ....application.ports.jobs import WorkflowCheckpoint, WorkflowRepository


_DDL = """
CREATE TABLE IF NOT EXISTS durable_workflow_checkpoint (
    job_id TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL,
    next_step INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    completed_step_id TEXT
);
"""


class SQLiteWorkflowCheckpointRepository(WorkflowRepository):
    """Durable monotonic checkpoint store sharing the job-registry database."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        with self._connect() as connection:
            connection.executescript(_DDL)

    def _connect(self) -> sqlite3.Connection:
        connection = owned_sqlite_connect(str(self._path), timeout=5.0)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _decode(row: tuple[object, ...] | None) -> WorkflowCheckpoint | None:
        if row is None:
            return None
        job_id, sequence, next_step, state_json, completed_step_id = row
        state = json.loads(str(state_json))
        if not isinstance(state, dict):
            raise ValueError("workflow checkpoint state must be an object")
        return WorkflowCheckpoint(
            str(job_id), int(sequence), int(next_step), state,  # type: ignore[arg-type]
            None if completed_step_id is None else str(completed_step_id),
        )

    def get_checkpoint(self, job_id: str) -> WorkflowCheckpoint | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT job_id,sequence,next_step,state_json,completed_step_id "
                "FROM durable_workflow_checkpoint WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return self._decode(row)

    def save_checkpoint(
        self,
        checkpoint: WorkflowCheckpoint,
        *,
        expected_sequence: int,
    ) -> WorkflowCheckpoint | None:
        if not isinstance(checkpoint, WorkflowCheckpoint):
            raise TypeError("checkpoint must be a WorkflowCheckpoint")
        if expected_sequence < -1:
            raise ValueError("expected_sequence cannot be less than -1")
        if checkpoint.sequence != expected_sequence + 1:
            raise ValueError("checkpoint sequence must immediately follow expected_sequence")
        payload = json.dumps(checkpoint.state, sort_keys=True, separators=(",", ":"))
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT sequence FROM durable_workflow_checkpoint WHERE job_id=?",
                (checkpoint.job_id,),
            ).fetchone()
            current_sequence = -1 if row is None else int(row[0])
            if current_sequence != expected_sequence:
                return None
            if row is None:
                connection.execute(
                    "INSERT INTO durable_workflow_checkpoint "
                    "(job_id,sequence,next_step,state_json,completed_step_id) VALUES (?,?,?,?,?)",
                    (checkpoint.job_id, checkpoint.sequence, checkpoint.next_step, payload,
                     checkpoint.completed_step_id),
                )
            else:
                connection.execute(
                    "UPDATE durable_workflow_checkpoint SET sequence=?,next_step=?,"
                    "state_json=?,completed_step_id=? WHERE job_id=? AND sequence=?",
                    (checkpoint.sequence, checkpoint.next_step, payload,
                     checkpoint.completed_step_id, checkpoint.job_id, expected_sequence),
                )
            return checkpoint


__all__ = ["SQLiteWorkflowCheckpointRepository"]
