"""The lock owner sidecar is diagnostic; its I/O must not decide lock outcomes.

On Windows a waiter that times out opens ``<lock>.owner.json`` to report the
holder. Python opens files without FILE_SHARE_DELETE, so the holder's
``os.replace``/``os.unlink`` of that sidecar can fail with a sharing
violation (PermissionError) at exactly that moment. These tests reproduce the
violation portably and require the guarded critical section to be unaffected.
"""
import os

import pytest

from sonder_runtime.adapters.filesystem import durable_locks


def _sidecar_permission_error(real, sidecar_suffix=".owner.json"):
    def patched(*args, **kwargs):
        names = [os.fspath(arg) for arg in args[:2] if isinstance(arg, (str, os.PathLike))]
        if any(name.endswith(sidecar_suffix) for name in names):
            raise PermissionError(32, "sharing violation", names[-1])
        return real(*args, **kwargs)

    return patched


def _assert_lock_free(lock_path):
    with durable_locks.exclusive_file_lock(lock_path, timeout=0):
        pass


def test_owner_publication_failure_still_runs_critical_section(tmp_path, monkeypatch):
    lock_path = tmp_path / "journal.lock"
    monkeypatch.setattr(
        durable_locks.os, "replace", _sidecar_permission_error(os.replace),
    )
    entered = []
    with durable_locks.exclusive_file_lock(lock_path, timeout=0.2, purpose="probe"):
        entered.append(True)
    assert entered == [True]
    monkeypatch.undo()
    _assert_lock_free(lock_path)
    # No temporary owner record is left behind by the failed publication.
    assert sorted(item.name for item in tmp_path.iterdir()) == ["journal.lock"]


def test_owner_publication_failure_does_not_report_stale_holder(tmp_path, monkeypatch):
    lock_path = tmp_path / "journal.lock"
    # A crashed earlier holder left its diagnostic record behind.
    durable_locks._write_owner(os.fspath(lock_path), "crashed-holder")
    monkeypatch.setattr(
        durable_locks.os, "replace", _sidecar_permission_error(os.replace),
    )
    with durable_locks.exclusive_file_lock(lock_path, timeout=0.2, purpose="live"):
        holder = durable_locks.read_owner(lock_path)
        assert holder is None or holder.get("purpose") != "crashed-holder"


def test_owner_clear_failure_does_not_fail_completed_critical_section(tmp_path, monkeypatch):
    lock_path = tmp_path / "journal.lock"
    completed = []
    with durable_locks.exclusive_file_lock(lock_path, timeout=0.2, purpose="probe"):
        monkeypatch.setattr(
            durable_locks.os, "unlink", _sidecar_permission_error(os.unlink),
        )
        completed.append(True)
    assert completed == [True]
    monkeypatch.undo()
    _assert_lock_free(lock_path)


def test_owner_clear_failure_never_masks_the_critical_section_error(tmp_path, monkeypatch):
    lock_path = tmp_path / "journal.lock"

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom), durable_locks.exclusive_file_lock(lock_path, timeout=0.2):
        monkeypatch.setattr(
            durable_locks.os, "unlink", _sidecar_permission_error(os.unlink),
        )
        raise Boom()
    monkeypatch.undo()
    _assert_lock_free(lock_path)
