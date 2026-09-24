"""Real-process regressions for identity-aware startup and training waits."""
import json
import os
import socket
import subprocess
import sys
import time

import pytest

import adaptive_training
import sonder_paths
from scripts import nightly_self_improve as nightly
from sonder_runtime.adapters.process_liveness import process_identity


@pytest.fixture
def live_child():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert process_identity(child.pid)
        yield child
    finally:
        # This test owns the Popen handle, not a discovered PID.
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)


def _owner(child, *, identity=None, host=None):
    return {
        "pid": child.pid,
        "host": host or socket.gethostname(),
        "process_identity": identity or process_identity(child.pid),
        "started": time.time(),
    }


def test_training_claim_distinguishes_reused_pid_without_touching_process(tmp_path, live_child):
    claim = tmp_path / ".launch-claimed"
    claim.write_text(json.dumps(_owner(live_child)), encoding="ascii")
    assert adaptive_training._training_claim_alive(adaptive_training._read_training_claim(claim))
    claim.write_text(json.dumps(_owner(live_child, identity="different-instance")), encoding="ascii")
    assert not adaptive_training._training_claim_alive(adaptive_training._read_training_claim(claim))
    assert live_child.poll() is None


def test_foreign_training_claim_is_unknown_and_not_reclaimed(live_child):
    assert adaptive_training._training_claim_alive(_owner(live_child, host="other-host"))
    assert live_child.poll() is None


def test_partial_training_claim_is_unknown_not_absent(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"run_dir": str(tmp_path)}), encoding="utf-8")
    claim = tmp_path / ".launch-claimed"
    claim.write_text("", encoding="ascii")
    assert adaptive_training._recorded_training_child_alive({"state_path": str(state)})
    claim.unlink()
    assert not adaptive_training._recorded_training_child_alive({"state_path": str(state)})


def test_migration_wait_has_deadline_and_names_live_holder(tmp_path, monkeypatch, live_child, caplog):
    legacy = tmp_path / "old.db"
    target = tmp_path / "memory.db"
    legacy.write_bytes(b"old content")
    lock = tmp_path / ".memory.db.legacy-migrate.lock"
    identity = process_identity(live_child.pid)
    lock.write_text(
        f"{live_child.pid}\n1.0\nowned\n{identity}\n{socket.gethostname()}\n", encoding="ascii",
    )
    monkeypatch.setattr(sonder_paths, "_LEGACY_DB_MIGRATION_WAIT_SECONDS", 0.15)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match=str(live_child.pid)):
        sonder_paths._migrate_legacy_memory_db(legacy, target)
    assert time.monotonic() - started < 3
    assert identity in caplog.text
    assert lock.exists() and not target.exists()
    assert live_child.poll() is None


def test_migration_reclaims_reused_pid_but_never_signals_replacement(tmp_path, live_child):
    legacy = tmp_path / "old.db"
    target = tmp_path / "memory.db"
    legacy.write_bytes(b"old content")
    lock = tmp_path / ".memory.db.legacy-migrate.lock"
    lock.write_text(
        f"{live_child.pid}\n1.0\nold-owner\ndifferent-instance\n{socket.gethostname()}\n",
        encoding="ascii",
    )
    sonder_paths._migrate_legacy_memory_db(legacy, target)
    assert target.read_bytes() == b"old content"
    assert live_child.poll() is None


def test_nightly_reclaims_reused_pid_claim_and_records_own_identity(tmp_path, live_child):
    lock = tmp_path / "nightly.lock"
    lock.write_text(json.dumps(_owner(live_child, identity="different-instance")), encoding="utf-8")
    messages = []
    assert nightly._claim_lock(lock, messages.append)
    try:
        current = json.loads(lock.read_text(encoding="utf-8"))
        assert current["pid"] == os.getpid()
        assert current["process_identity"] == process_identity(os.getpid())
        assert not nightly._claim_lock(lock, messages.append)
        assert str(os.getpid()) in messages[-1]
        assert live_child.poll() is None
    finally:
        nightly._release_lock(lock)


def test_nightly_live_or_foreign_owner_is_reported_not_reclaimed(tmp_path, live_child):
    lock = tmp_path / "nightly.lock"
    for owner in (_owner(live_child), _owner(live_child, host="other-host")):
        lock.write_text(json.dumps(owner), encoding="utf-8")
        messages = []
        assert not nightly._claim_lock(lock, messages.append)
        assert str(live_child.pid) in messages[-1]
        assert json.loads(lock.read_text(encoding="utf-8")) == owner
        assert live_child.poll() is None


def test_alias_transition_error_names_age_and_policy(tmp_path, monkeypatch):
    transition = tmp_path / "transition.json"
    transition.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(adaptive_training, "_shared_alias_paths", lambda: {"transition": transition})
    monkeypatch.setattr(adaptive_training, "_read_shared_alias_record", lambda _: {
        "created_ts": 12345, "policy_path": "foreign-policy.json",
    })
    with pytest.raises(RuntimeError, match="created_ts=12345 policy_path=foreign-policy.json"):
        adaptive_training._claim_shared_alias_transition("candidate", "unused-token")
