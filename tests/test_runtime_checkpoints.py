from __future__ import annotations

import json
import hashlib
import hmac
from concurrent.futures import ThreadPoolExecutor

import pytest

from sonder_runtime.adapters.persistence.sqlite.runtime_checkpoints import SQLiteRuntimeCheckpointRepository
from sonder_runtime.application.ports.runtime_checkpoints import CheckpointConflict, CheckpointError, RestoreStatus, RuntimeCheckpoint, canonical_json


SEAL_KEY = b"k" * 32


def checkpoint(generation=0):
    return RuntimeCheckpoint(
        "run-1", generation, {"purpose": "test"},
        decisions={"choice": "resume"}, memory_refs={"refs": ["m-1"]},
        workers={"worker-1": {"status": "paused"}}, retry_state={"attempt": 1},
        tool_state={"cursor": "tool-2"}, routing={"model": "local"},
        repository_state={"sha": "abc", "worktree": "clean"},
        verification={"tests": "passed"}, resume_cursor="step-2",
        checkpoint_id=f"cp-{generation}", created_at="2026-09-22T00:00:00Z",
    )


def test_round_trip_and_restart(tmp_path):
    db = tmp_path / "checkpoints.db"
    SQLiteRuntimeCheckpointRepository(db, seal_key=SEAL_KEY).save(checkpoint(), expected_generation=-1)
    restored = SQLiteRuntimeCheckpointRepository(db, seal_key=SEAL_KEY).restore("run-1")
    assert restored.status is RestoreStatus.RESTORED
    assert restored.checkpoint == checkpoint()


def test_generation_cas_and_append_only_history(tmp_path):
    store = SQLiteRuntimeCheckpointRepository(tmp_path / "cp.db", seal_key=SEAL_KEY)
    store.save(checkpoint(), expected_generation=-1)
    with pytest.raises(CheckpointConflict):
        store.save(checkpoint(2), expected_generation=0)
    store.save(checkpoint(1), expected_generation=0)
    assert store.restore("run-1").checkpoint.generation == 1


def test_tamper_is_fail_closed(tmp_path):
    db = tmp_path / "cp.db"
    store = SQLiteRuntimeCheckpointRepository(db, seal_key=SEAL_KEY)
    store.save(checkpoint(), expected_generation=-1)
    import sqlite3
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE runtime_checkpoint SET payload_json=? WHERE run_id=?", (json.dumps({"bad": True}), "run-1"))
        conn.commit()
    finally:
        conn.close()
    result = store.restore("run-1")
    assert result.status is RestoreStatus.CORRUPT


def test_schema_mismatch_is_incompatible(tmp_path):
    db = tmp_path / "cp.db"
    store = SQLiteRuntimeCheckpointRepository(db, seal_key=SEAL_KEY)
    store.save(checkpoint(), expected_generation=-1)
    import sqlite3
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT payload_json FROM runtime_checkpoint").fetchone()
        payload = json.loads(row[0]); payload["schema_version"] = 99
        encoded = canonical_json(payload)
        conn.execute("UPDATE runtime_checkpoint SET payload_json=?, seal=?", (encoded.decode("ascii"), hmac.new(SEAL_KEY, encoded, hashlib.sha256).hexdigest()))
        conn.commit()
    finally:
        conn.close()
    assert store.restore("run-1").status is RestoreStatus.INCOMPATIBLE


def test_secret_bearing_fields_are_rejected():
    with pytest.raises(CheckpointError):
        RuntimeCheckpoint("run", 0, {"api_key": "never persist"})


def test_nested_state_is_immutable_and_normal_tokens_are_allowed():
    workers = {"worker": {"token_budget": 10, "tokenizer": "local"}}
    value = RuntimeCheckpoint("run", 0, {"workers": workers}, workers=workers)
    digest = value.digest()
    with pytest.raises(TypeError):
        value.workers["worker"]["token_budget"] = 20
    assert value.digest() == digest


def test_forged_digest_without_seal_key_is_rejected(tmp_path):
    db = tmp_path / "cp.db"
    store = SQLiteRuntimeCheckpointRepository(db, seal_key=SEAL_KEY)
    store.save(checkpoint(), expected_generation=-1)
    import sqlite3
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT payload_json FROM runtime_checkpoint").fetchone()
        payload = json.loads(row[0]); payload["manifest"]["purpose"] = "forged"
        payload["digest"] = RuntimeCheckpoint(
            "run-1", 0, payload["manifest"], decisions=payload["decisions"],
            memory_refs=payload["memory_refs"], workers=payload["workers"],
            retry_state=payload["retry_state"], tool_state=payload["tool_state"],
            routing=payload["routing"], repository_state=payload["repository_state"],
            verification=payload["verification"], resume_cursor=payload["resume_cursor"],
            checkpoint_id=payload["checkpoint_id"], created_at=payload["created_at"],
        ).digest()
        conn.execute("UPDATE runtime_checkpoint SET payload_json=?", (canonical_json(payload).decode("ascii"),))
        conn.commit()
    finally:
        conn.close()
    assert store.restore("run-1").status is RestoreStatus.CORRUPT


def test_concurrent_adapters_have_one_successful_initial_cas(tmp_path):
    db = tmp_path / "cp.db"
    stores = [SQLiteRuntimeCheckpointRepository(db, seal_key=SEAL_KEY) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(_save_initial, stores))
    assert sorted(results) == ["conflict", "saved"]


def _save_initial(store):
    try:
        store.save(checkpoint(), expected_generation=-1)
    except CheckpointConflict:
        return "conflict"
    return "saved"
