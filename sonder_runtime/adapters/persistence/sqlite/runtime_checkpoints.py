"""SQLite durability adapter for sealed runtime checkpoints."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import json
from pathlib import Path
from threading import Lock

from sonder_runtime.adapters.persistence.owned_sqlite import transaction as owned_sqlite_transaction
from sonder_runtime.application.execution.effect_journal import EffectJournalError
from sonder_runtime.application.ports.runtime_checkpoints import (
    CheckpointConflict, CheckpointError, RestoreResult, RestoreStatus,
    RuntimeCheckpoint, SCHEMA_VERSION, canonical_json,
)


_DDL = """
CREATE TABLE IF NOT EXISTS runtime_checkpoint (
    run_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    seal TEXT NOT NULL,
    PRIMARY KEY (run_id, generation)
);
CREATE INDEX IF NOT EXISTS ix_runtime_checkpoint_latest
    ON runtime_checkpoint (run_id, generation DESC);
"""


class SQLiteRuntimeCheckpointRepository:
    """Append-only generations with expected-generation compare-and-set."""

    def __init__(self, db_path: str | Path, *, seal_key: bytes | str,
                 max_payload_bytes: int = 256 * 1024, effect_journal=None) -> None:
        if type(max_payload_bytes) is not int or not 1024 <= max_payload_bytes <= 256 * 1024:
            raise ValueError("max_payload_bytes must be between 1024 and 262144")
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._max_payload_bytes = max_payload_bytes
        self._effect_journal = effect_journal
        if isinstance(seal_key, str):
            seal_key = seal_key.encode("utf-8")
        if not isinstance(seal_key, bytes) or len(seal_key) < 32 or len(seal_key) > 4096:
            raise ValueError("checkpoint seal key must be between 32 and 4096 bytes")
        self._seal_key = seal_key
        self._lock = Lock()
        with self._connect() as connection:
            connection.executescript(_DDL + """
            CREATE TABLE IF NOT EXISTS runtime_checkpoint_effect (
                run_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                effect_high_water INTEGER NOT NULL,
                PRIMARY KEY(run_id, generation)
            );
            """)

    @contextmanager
    def _connect(self):
        with owned_sqlite_transaction(str(self._path), timeout=5.0) as connection:
            connection.execute("PRAGMA busy_timeout=5000")
            yield connection

    def save(self, checkpoint: RuntimeCheckpoint, *, expected_generation: int) -> RuntimeCheckpoint:
        if not isinstance(checkpoint, RuntimeCheckpoint):
            raise CheckpointError("checkpoint must be a RuntimeCheckpoint")
        if type(expected_generation) is not int or expected_generation < -1:
            raise CheckpointError("expected_generation must be at least -1")
        if checkpoint.generation != expected_generation + 1:
            raise CheckpointConflict("checkpoint generation must immediately follow expected_generation")
        payload = canonical_json(checkpoint.sealed())
        if len(payload) > self._max_payload_bytes:
            raise CheckpointError("checkpoint exceeds repository payload bound")
        digest = checkpoint.digest()
        seal = hmac.new(self._seal_key, payload, hashlib.sha256).hexdigest()
        effect_high_water = None
        if self._effect_journal is not None:
            effect_high_water = self._effect_journal.high_water(checkpoint.run_id)
            self._effect_journal.validate_checkpoint(checkpoint.run_id, effect_high_water)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT generation,digest FROM runtime_checkpoint WHERE run_id=? ORDER BY generation DESC LIMIT 1",
                (checkpoint.run_id,),
            ).fetchone()
            actual = -1 if current is None else int(current[0])
            if actual != expected_generation:
                raise CheckpointConflict("checkpoint generation changed")
            connection.execute(
                "INSERT INTO runtime_checkpoint(run_id,generation,payload_json,digest,seal) VALUES(?,?,?,?,?)",
                (checkpoint.run_id, checkpoint.generation, payload.decode("ascii"), digest, seal),
            )
            if effect_high_water is not None:
                connection.execute(
                    "INSERT INTO runtime_checkpoint_effect(run_id,generation,effect_high_water) VALUES(?,?,?)",
                    (checkpoint.run_id, checkpoint.generation, effect_high_water),
                )
        return checkpoint

    def restore(self, run_id: str) -> RestoreResult:
        if not isinstance(run_id, str) or not run_id.strip():
            raise CheckpointError("run_id is required")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json,digest,seal FROM runtime_checkpoint WHERE run_id=? ORDER BY generation DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        if row is None:
            return RestoreResult(RestoreStatus.EMPTY, detail="no checkpoint exists")
        effect_binding = None
        if self._effect_journal is not None:
            with self._connect() as connection:
                effect_binding = connection.execute(
                    "SELECT effect_high_water FROM runtime_checkpoint_effect "
                    "WHERE run_id=? AND generation=(SELECT MAX(generation) FROM runtime_checkpoint WHERE run_id=?)",
                    (run_id, run_id),
                ).fetchone()
        raw, stored_digest, stored_seal = str(row[0]), str(row[1]), str(row[2])
        try:
            encoded = raw.encode("ascii")
            if len(encoded) > self._max_payload_bytes:
                return RestoreResult(RestoreStatus.CORRUPT, detail="payload exceeds bound")
            expected_seal = hmac.new(self._seal_key, encoded, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected_seal, stored_seal):
                return RestoreResult(RestoreStatus.CORRUPT, detail="checkpoint seal mismatch")
            value = json.loads(raw)
            if encoded != canonical_json(value):
                return RestoreResult(RestoreStatus.CORRUPT, detail="checkpoint encoding is not canonical")
            if not isinstance(value, dict) or set(value) != {
                "schema_version", "run_id", "generation", "manifest", "decisions", "memory_refs", "workers",
                "retry_state", "tool_state", "routing", "repository_state", "verification", "resume_cursor",
                "checkpoint_id", "created_at", "digest",
            }:
                return RestoreResult(RestoreStatus.CORRUPT, detail="checkpoint envelope is not exact")
            if value["schema_version"] != SCHEMA_VERSION:
                return RestoreResult(RestoreStatus.INCOMPATIBLE, detail="unsupported checkpoint schema")
            body = {key: item for key, item in value.items() if key != "digest"}
            if hashlib.sha256(canonical_json(body)).hexdigest() != value["digest"] or value["digest"] != stored_digest:
                return RestoreResult(RestoreStatus.CORRUPT, detail="checkpoint digest mismatch")
            checkpoint = RuntimeCheckpoint(**body)
            if checkpoint.run_id != run_id or checkpoint.digest() != value["digest"]:
                return RestoreResult(RestoreStatus.CORRUPT, detail="checkpoint identity mismatch")
            if self._effect_journal is not None:
                if effect_binding is None:
                    return RestoreResult(RestoreStatus.CORRUPT, detail="checkpoint effect binding is missing")
                try:
                    self._effect_journal.validate_checkpoint(run_id, int(effect_binding[0]))
                except (EffectJournalError, ValueError) as exc:
                    return RestoreResult(RestoreStatus.CORRUPT, detail=str(exc))
            return RestoreResult(RestoreStatus.RESTORED, checkpoint=checkpoint)
        except (CheckpointError, ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
            return RestoreResult(RestoreStatus.CORRUPT, detail=f"checkpoint rejected: {type(exc).__name__}")


__all__ = ["SQLiteRuntimeCheckpointRepository"]
