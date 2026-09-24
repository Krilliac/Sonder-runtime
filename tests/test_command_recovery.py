import json

import pytest

import command_recovery
import durable_locks


def test_receive_complete_replay_and_ack_lifecycle(tmp_path):
    path = tmp_path / "commands.jsonl"
    journal = command_recovery.CommandJournal(path)

    received = journal.receive("phone-1", "command-1", {"action": "restart"})
    assert received["state"] == "pending"
    assert received["dispatch"] is True

    duplicate = journal.receive("phone-1", "command-1", {"action": "restart"})
    assert duplicate["state"] == "pending"
    assert duplicate["dispatch"] is False

    result = {"status": 202, "payload": {"operation_id": "abc"}}
    journal.complete("phone-1", "command-1", result)
    replay = journal.receive("phone-1", "command-1", {"action": "restart"})
    assert replay == {
        "client_id": "phone-1",
        "command_id": "command-1",
        "state": "completed",
        "dispatch": False,
        "result": result,
    }

    assert journal.acknowledge("phone-1", "command-1")["state"] == "acknowledged"
    assert journal.acknowledge("phone-1", "command-1")["state"] == "acknowledged"
    assert journal.inspect("phone-1", "command-1")["state"] == "acknowledged"


def test_restart_turns_open_receipt_uncertain_and_never_redispatches(tmp_path):
    path = tmp_path / "commands.jsonl"
    first = command_recovery.CommandJournal(path)
    assert first.receive("desktop", "mutate-9", {"action": "stop"})["dispatch"]

    recovered = command_recovery.CommandJournal(path)
    decision = recovered.receive("desktop", "mutate-9", {"action": "stop"})
    assert decision["state"] == "uncertain"
    assert decision["dispatch"] is False


def test_request_identity_conflict_fails_closed(tmp_path):
    journal = command_recovery.CommandJournal(tmp_path / "commands.jsonl")
    journal.receive("client", "same-id", {"action": "start"})
    with pytest.raises(command_recovery.CommandConflict):
        journal.receive("client", "same-id", {"action": "stop"})


def test_compaction_is_bounded_and_preserves_unacknowledged_replay(tmp_path):
    path = tmp_path / "commands.jsonl"
    journal = command_recovery.CommandJournal(
        path, max_commands=2, compact_after_bytes=command_recovery.MAX_EVENT_BYTES
    )
    for command_id in ("one", "two"):
        journal.receive("client", command_id, {"action": command_id})
        journal.complete("client", command_id, {"ok": True, "id": command_id})
    with pytest.raises(command_recovery.CommandJournalFull):
        journal.receive("client", "three", {"action": "three"})

    journal.acknowledge("client", "one")
    assert journal.receive("client", "three", {"action": "three"})["dispatch"]
    recovered = command_recovery.CommandJournal(path, max_commands=2)
    assert recovered.inspect("client", "two")["result"] == {
        "ok": True,
        "id": "two",
    }
    assert path.stat().st_size < 4 * command_recovery.MAX_EVENT_BYTES


def test_incomplete_final_append_is_discarded_before_next_record(tmp_path):
    path = tmp_path / "commands.jsonl"
    journal = command_recovery.CommandJournal(path)
    journal.receive("client", "one", {"action": "start"})
    with open(path, "ab") as stream:
        stream.write(b'{"incomplete":')
        stream.flush()

    recovered = command_recovery.CommandJournal(path)
    assert recovered.receive("client", "two", {"action": "stop"})["dispatch"]
    for line in path.read_text(encoding="utf-8").splitlines():
        assert isinstance(json.loads(line), dict)


def test_identifiers_and_payloads_are_strictly_bounded(tmp_path):
    journal = command_recovery.CommandJournal(tmp_path / "commands.jsonl")
    with pytest.raises(ValueError):
        journal.receive("bad client", "one", {})
    with pytest.raises(ValueError):
        journal.receive("client", "x" * 129, {})
    with pytest.raises(ValueError):
        journal.receive(
            "client", "large", {"value": "x" * command_recovery.MAX_REQUEST_BYTES}
        )


def test_durable_transition_survives_best_effort_compaction_failure(tmp_path, monkeypatch):
    journal = command_recovery.CommandJournal(
        tmp_path / "commands.jsonl",
        compact_after_bytes=command_recovery.MAX_EVENT_BYTES,
    )
    monkeypatch.setattr(
        journal, "_compact_locked", lambda _states: (_ for _ in ()).throw(OSError("busy")),
    )

    assert journal.receive("client", "one", {"action": "start"})["dispatch"] is True
    completed = journal.complete(
        "client", "one", {"status": 202, "payload": {"ok": True}}
    )

    assert completed["state"] == "completed"
    recovered = command_recovery.CommandJournal(journal.path)
    assert recovered.inspect("client", "one")["state"] == "completed"


def test_command_journal_lock_timeout_reports_holder(tmp_path):
    path = tmp_path / "commands.jsonl"
    journal = command_recovery.CommandJournal(path, lock_timeout=0)
    with durable_locks.exclusive_file_lock(
        journal.lock_path, timeout=1, purpose="test-command-holder"
    ), pytest.raises(durable_locks.LockTimeout, match="test-command-holder"):
        journal.receive("client", "blocked", {"action": "start"})


def test_command_journal_thread_wait_is_bounded_and_names_owner(tmp_path):
    import os
    import threading
    import time

    journal = command_recovery.CommandJournal(tmp_path / "commands.jsonl", lock_timeout=0.1)
    entered, release = threading.Event(), threading.Event()

    def hold():
        with journal._locked():
            entered.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert entered.wait(3)
        started = time.monotonic()
        with pytest.raises(command_recovery.CommandJournalLockTimeout) as raised:
            journal.receive("client", "blocked", {"action": "start"})
        assert time.monotonic() - started < 2
        assert raised.value.holder["pid"] == os.getpid()
        assert raised.value.holder["thread_id"] == holder.ident
    finally:
        release.set()
        holder.join(timeout=3)
    assert not holder.is_alive()


def test_lock_poll_interval_cannot_extend_the_acquisition_deadline(tmp_path):
    import time

    lock = tmp_path / "deadline.lock"
    with durable_locks.exclusive_file_lock(lock, purpose="long-poll-holder"):
        started = time.monotonic()
        with (
            pytest.raises(durable_locks.LockTimeout, match="long-poll-holder"),
            durable_locks.exclusive_file_lock(lock, timeout=0.05, poll_interval=10),
        ):
            pytest.fail("contended lock admitted a second owner")
        assert time.monotonic() - started < 1
