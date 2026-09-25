"""Persisted snapshot: atomic private save, fail-soft load, tamper re-validation."""
import json
import os
import shutil
import stat
import sys

import pytest

from sonder_runtime.adapters.host_tools import guards
from sonder_runtime.adapters.host_tools.snapshot_store import JsonSnapshotStore, MAX_SNAPSHOT_BYTES
from sonder_runtime.application.host_tools.service import HostToolInventoryService
from sonder_runtime.domain.host_tools.model import (
    DiscoverySource,
    ToolCategory,
    ToolRecord,
    VersionStatus,
    build_snapshot,
    snapshot_to_wire,
)


def _snapshot(path="/usr/bin/python3", created_at=None):
    import time

    record = ToolRecord(
        name="python3", category=ToolCategory.RUNTIME, path=path, source=DiscoverySource.PATH,
        on_path=True, version="3.12.3", version_status=VersionStatus.OK, identity="1:1",
    )
    return build_snapshot(os="Linux", os_release="x", machine="x86_64",
                          created_at=time.time() if created_at is None else created_at,
                          duration_ms=1, tools=[record])


def test_save_is_atomic_private_and_round_trips(tmp_path):
    target = tmp_path / "state" / "host-tools.json"
    store = JsonSnapshotStore(str(target))
    snapshot = _snapshot()
    store.save(snapshot)
    assert store.load() == snapshot
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert [p.name for p in target.parent.iterdir()] == ["host-tools.json"]  # no temp leftovers


def test_missing_corrupt_oversized_and_invalid_files_load_as_none(tmp_path):
    target = tmp_path / "host-tools.json"
    store = JsonSnapshotStore(str(target))
    assert store.load() is None
    target.write_text("{not json")
    assert store.load() is None
    target.write_text(json.dumps({"schema": "other"}))
    assert store.load() is None
    target.write_bytes(b" " * (MAX_SNAPSHOT_BYTES + 1))
    assert store.load() is None
    # Control: a valid document loads.
    target.write_text(json.dumps(snapshot_to_wire(_snapshot())))
    assert store.load() is not None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_symlinked_snapshot_is_ignored(tmp_path):
    real = tmp_path / "real.json"
    real.write_text(json.dumps(snapshot_to_wire(_snapshot())))
    link = tmp_path / "host-tools.json"
    link.symlink_to(real)
    assert JsonSnapshotStore(str(link)).load() is None
    assert JsonSnapshotStore(str(real)).load() is not None  # control


class _NoDiscovery:
    def discover(self, *, previous, full):
        raise AssertionError("lookup must not rediscover a fresh snapshot")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable bits")
def test_tampered_snapshot_pointing_at_planted_file_is_rejected_by_lookup(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    planted = project / "python3"
    planted.write_text("#!/bin/sh\necho planted\n")
    planted.chmod(0o755)
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(project))
    target = tmp_path / "host-tools.json"
    # An attacker who can write the state home also recomputes the digest.
    target.write_text(json.dumps(snapshot_to_wire(_snapshot(str(planted)))))
    service = HostToolInventoryService(
        _NoDiscovery(), JsonSnapshotStore(str(target)), clock=__import__("time").time,
        redact_path=lambda p: p, executable_guard=guards.executable_allowed,
    )
    assert service.cached().tools[0].path == str(planted)
    assert service.lookup("python3") is None
    with pytest.raises(PermissionError):
        guards.require_host_executable(str(planted))

    real_python = shutil.which("python3") or sys.executable
    target.write_text(json.dumps(snapshot_to_wire(_snapshot(os.path.realpath(real_python)))))
    control = HostToolInventoryService(
        _NoDiscovery(), JsonSnapshotStore(str(target)), clock=__import__("time").time,
        redact_path=lambda p: p, executable_guard=guards.executable_allowed,
    )
    assert control.lookup("python3") is not None
    assert guards.require_host_executable(os.path.realpath(real_python))
