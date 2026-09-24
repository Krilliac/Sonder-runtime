"""Diagnostic owner writes must use the same directory authority as the lock."""
import json
import os

import pytest

from sonder_runtime.adapters.filesystem.durable_locks import exclusive_descriptor_lock


@pytest.mark.skipif(os.name == "nt", reason="Windows directory anchors deny rename")
def test_owner_metadata_stays_anchored_when_parent_is_replaced(tmp_path):
    original = tmp_path / "original"
    original.mkdir()
    lock_path = original / "transfer.lock"
    lock_path.write_bytes(b"0")
    descriptor = os.open(lock_path, os.O_RDWR)
    directory = os.open(original, os.O_RDONLY | os.O_DIRECTORY)
    anchored = tmp_path / "anchored"
    try:
        original.rename(anchored)
        original.mkdir()
        with exclusive_descriptor_lock(
            descriptor, lock_path, timeout=0.2, purpose="swap-test", directory_fd=directory,
        ):
            assert not (original / "transfer.lock.owner.json").exists()
            record = json.loads((anchored / "transfer.lock.owner.json").read_text())
            assert record["pid"] == os.getpid()
            assert record["purpose"] == "swap-test"
        assert not (anchored / "transfer.lock.owner.json").exists()
        assert (anchored / "transfer.lock").exists()
    finally:
        os.close(descriptor)
        os.close(directory)
