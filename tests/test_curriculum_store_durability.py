"""Adversarial durability tests for curriculum_store and durable_locks.

The historical failure modes: concurrent curriculum runs interleaving
partial JSONL lines into generated_tasks.jsonl, and a single torn or
corrupt line making the entire curriculum unreadable (load() raised).
"""
import json
import logging
import os
import subprocess
import sys

import pytest

import curriculum_store
import durable_locks

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --- load: corruption containment -----------------------------------------


def test_load_ignores_torn_unterminated_tail(tmp_path):
    path = tmp_path / "gen.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"name": "a"}) + "\n")
        f.write(json.dumps({"name": "b"}) + "\n")
        f.write('{"name": "torn-mid-app')  # crash mid-append: no newline
    tasks = curriculum_store.load(path)
    assert [t["name"] for t in tasks] == ["a", "b"]


def test_load_skips_interior_corruption_with_a_counted_warning(tmp_path, caplog):
    path = tmp_path / "gen.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"name": "a"}) + "\n")
        f.write("%%% not json %%%\n")
        f.write('"valid json but not a task dict"\n')
        f.write(json.dumps({"name": "b"}) + "\n")
    with caplog.at_level(logging.WARNING):
        tasks = curriculum_store.load(path)
    assert [t["name"] for t in tasks] == ["a", "b"]
    assert any("skipped 2 corrupt line(s)" in rec.getMessage() for rec in caplog.records)


def test_load_of_missing_file_is_empty(tmp_path):
    assert curriculum_store.load(tmp_path / "absent.jsonl") == []


# --- append: batch atomicity ----------------------------------------------


def test_unserializable_batch_leaves_store_untouched(tmp_path):
    path = tmp_path / "gen.jsonl"
    curriculum_store.append([{"name": "a"}], path)
    before = path.read_bytes()
    with pytest.raises(TypeError):
        curriculum_store.append([{"name": "b"}, {"bad": {1, 2}}], path)
    assert path.read_bytes() == before


def test_append_refuses_symlinked_store(tmp_path):
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("", encoding="utf-8")
    link = tmp_path / "gen.jsonl"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform/privilege level")
    with pytest.raises(OSError):
        curriculum_store.append([{"name": "a"}], link)
    assert target.read_text(encoding="utf-8") == ""


# --- append: cross-process serialization ----------------------------------

_WRITER = r"""
import os, sys
root = sys.argv[1]
for sub in (os.path.join(root, "scripts"), os.path.join(root, "runtime"), root):
    if sub not in sys.path:
        sys.path.insert(0, sub)
import curriculum_store
path, worker, batches = sys.argv[2], sys.argv[3], int(sys.argv[4])
blob = "x" * 65536
for i in range(batches):
    curriculum_store.append(
        [{"name": "w%s-b%d" % (worker, i), "blob": blob}], path
    )
"""


def test_concurrent_multiprocess_appends_never_interleave(tmp_path):
    path = tmp_path / "gen.jsonl"
    workers, batches = 4, 12
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _WRITER, REPO_ROOT, str(path), str(w), str(batches)],
            cwd=REPO_ROOT,
        )
        for w in range(workers)
    ]
    for proc in procs:
        assert proc.wait(timeout=120) == 0
    tasks = curriculum_store.load(path)
    names = {t["name"] for t in tasks}
    expected = {"w%d-b%d" % (w, i) for w in range(workers) for i in range(batches)}
    # Every line parsed, every task present exactly once, nothing interleaved.
    assert names == expected
    assert len(tasks) == workers * batches
    assert all(len(t["blob"]) == 65536 for t in tasks)


# --- durable_locks primitive ----------------------------------------------


def test_lock_is_exclusive_and_times_out(tmp_path):
    lock_path = tmp_path / "x.lock"
    with durable_locks.exclusive_file_lock(lock_path, timeout=5, purpose="test-holder"):
        owner = durable_locks.read_owner(lock_path)
        assert owner["pid"] == os.getpid()
        assert owner["host"]
        assert owner["started"]
        assert owner["purpose"] == "test-holder"
        with pytest.raises(durable_locks.LockTimeout, match="test-holder") as error:
            with durable_locks.exclusive_file_lock(lock_path, timeout=0.2):
                pass
        assert error.value.holder["pid"] == os.getpid()
        assert os.path.exists(durable_locks.owner_path(lock_path))
    assert not os.path.exists(durable_locks.owner_path(lock_path))
    # Released: a fresh acquisition succeeds immediately.
    with durable_locks.exclusive_file_lock(lock_path, timeout=1):
        pass


def test_lock_refuses_symlinked_lock_path(tmp_path):
    target = tmp_path / "real.lock"
    target.write_text("0", encoding="utf-8")
    link = tmp_path / "x.lock"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform/privilege level")
    with pytest.raises(OSError):
        with durable_locks.exclusive_file_lock(link, timeout=0.2):
            pass


def test_nt_try_lock_locks_before_writing_byte_zero(monkeypatch):
    """Cross-platform regression test for the Windows 0-byte lock write race.

    Writing byte 0 before calling msvcrt.locking raised PermissionError when
    another process held the mandatory byte-0 lock on a newly created file.
    """
    import types

    from sonder_runtime.adapters.filesystem import durable_locks as durable_locks_impl

    events = []
    held = {"locked": True}
    file_size = {"n": 0}

    fake_msvcrt = types.SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2)

    def fake_locking(fd, mode, nbytes):
        events.append(("locking", mode, nbytes))
        if mode == fake_msvcrt.LK_NBLCK:
            if held["locked"]:
                raise PermissionError(13, "Permission denied")
            held["locked"] = True
        elif mode == fake_msvcrt.LK_UNLCK:
            held["locked"] = False

    fake_msvcrt.locking = fake_locking

    def fake_write(fd, data):
        events.append(("write", data))
        if held["locked"] and ("locking", fake_msvcrt.LK_NBLCK, 1) not in events:
            raise PermissionError(13, "Permission denied")
        file_size["n"] += len(data)
        return len(data)

    monkeypatch.setattr(durable_locks_impl.os, "name", "nt")
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(durable_locks_impl.os, "lseek", lambda fd, pos, how: 0)
    monkeypatch.setattr(
        durable_locks_impl.os,
        "fstat",
        lambda fd: types.SimpleNamespace(st_size=file_size["n"]),
    )
    monkeypatch.setattr(durable_locks_impl.os, "write", fake_write)
    monkeypatch.setattr(durable_locks_impl.os, "fsync", lambda fd: events.append(("fsync",)))

    # Contended 0-byte file: must return False without attempting os.write.
    assert durable_locks_impl._try_lock(99) is False
    assert events == [("locking", fake_msvcrt.LK_NBLCK, 1)]

    # Uncontended 0-byte file: must lock FIRST, then write b"0" and fsync.
    events.clear()
    held["locked"] = False
    assert durable_locks_impl._try_lock(99) is True
    assert events == [
        ("locking", fake_msvcrt.LK_NBLCK, 1),
        ("write", b"0"),
        ("fsync",),
    ]

    # Failure during post-lock initialization must release the acquired lock.
    events.clear()
    held["locked"] = False
    file_size["n"] = 0

    def failing_write(fd, data):
        events.append(("write_fail", data))
        raise OSError("disk full")

    monkeypatch.setattr(durable_locks_impl.os, "write", failing_write)
    with pytest.raises(OSError, match="disk full"):
        durable_locks_impl._try_lock(99)
    assert held["locked"] is False
    assert ("locking", fake_msvcrt.LK_UNLCK, 1) in events


@pytest.mark.skipif(os.name != "nt", reason="Windows mandatory byte-range locking semantics")
def test_windows_contended_zero_byte_lock_returns_false_not_permission_error(tmp_path):
    """Reproduces the exact Windows race window on a live 0-byte lock file."""
    import msvcrt

    from sonder_runtime.adapters.filesystem import durable_locks as durable_locks_impl

    lock_path = tmp_path / "fresh.lock"
    flags = os.O_RDWR | os.O_CREAT
    fd1 = os.open(lock_path, flags, 0o600)
    fd2 = os.open(lock_path, flags, 0o600)
    try:
        assert os.fstat(fd1).st_size == 0
        os.lseek(fd1, 0, os.SEEK_SET)
        msvcrt.locking(fd1, msvcrt.LK_NBLCK, 1)
        try:
            # fd1 holds byte 0 while file size is still 0. Before the fix,
            # _try_lock(fd2) called os.write(fd2, b"0") and raised PermissionError.
            assert durable_locks_impl._try_lock(fd2) is False
        finally:
            os.lseek(fd1, 0, os.SEEK_SET)
            msvcrt.locking(fd1, msvcrt.LK_UNLCK, 1)

        # Once fd1 releases, fd2 acquires cleanly and initializes byte 0.
        assert durable_locks_impl._try_lock(fd2) is True
        try:
            assert os.fstat(fd2).st_size == 1
        finally:
            durable_locks_impl._unlock(fd2)
    finally:
        os.close(fd1)
        os.close(fd2)
