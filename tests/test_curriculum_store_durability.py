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
import sys
sys.path.insert(0, sys.argv[1])
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
    with durable_locks.exclusive_file_lock(lock_path, timeout=5):
        with pytest.raises(durable_locks.LockTimeout):
            with durable_locks.exclusive_file_lock(lock_path, timeout=0.2):
                pass
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
