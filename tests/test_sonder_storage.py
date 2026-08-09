from __future__ import annotations

import io
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import sonder_storage


def test_model_roots_prefers_environment_without_hardcoded_drive(tmp_path):
    configured = tmp_path / "ollama-models"
    roots = sonder_storage.model_roots({"OLLAMA_MODELS": str(configured)})
    assert roots[0] == configured.resolve()
    assert all(not str(root).startswith("D:\\") for root in roots)


def test_windows_dispatch_uses_native_classifier(monkeypatch, tmp_path):
    expected = {"mount": "C:\\", "filesystem": "ntfs", "drive_type": "fixed",
                "network": False, "removable": False}
    monkeypatch.setattr(sonder_storage, "_windows_storage", lambda path: expected)
    assert sonder_storage.classify(tmp_path, system="Windows") == expected


def test_linux_mountinfo_detects_network_storage(monkeypatch, tmp_path):
    mount = tmp_path.resolve()
    line = (
        "42 31 0:55 / %s rw,relatime - nfs4 server:/models rw\n"
        % str(mount).replace(" ", "\\040")
    ).encode()
    real_open = open

    def bounded_open(path, mode="r", *args, **kwargs):
        if str(path) == "/proc/self/mountinfo":
            return io.BytesIO(line)
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", bounded_open)
    result = sonder_storage._linux_storage(mount)
    assert result["filesystem"] == "nfs4"
    assert result["network"] is True
    assert result["drive_type"] == "network"


def test_linux_mountinfo_read_is_strictly_bounded(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "builtins.open",
        lambda *args, **kwargs: io.BytesIO(b"x" * (sonder_storage._MOUNTINFO_LIMIT + 1)),
    )
    with pytest.raises(OSError, match="bounded inspection limit"):
        sonder_storage._linux_storage(tmp_path)


def test_macos_fallback_is_metadata_only(monkeypatch, tmp_path):
    monkeypatch.setattr(sonder_storage.shutil, "which", lambda name: None)
    result = sonder_storage.classify(tmp_path, system="Darwin")
    assert result["drive_type"] == "local-unknown"
    assert result["network"] is False
    assert result["removable"] is False


def test_macos_diskutil_classifies_removable_volume(monkeypatch, tmp_path):
    import plistlib

    payload = plistlib.dumps({
        "MountPoint": "/Volumes/Models", "FilesystemType": "exfat",
        "BusProtocol": "USB", "Removable": True,
    })
    monkeypatch.setattr(sonder_storage.shutil, "which", lambda name: "/bin/diskutil")
    monkeypatch.setattr(
        sonder_storage.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=payload),
    )
    result = sonder_storage.classify(tmp_path, system="Darwin")
    assert result["filesystem"] == "exfat"
    assert result["drive_type"] == "removable"
    assert result["removable"] is True


@pytest.mark.parametrize("kind", ["network", "removable"])
def test_inspection_warns_for_slow_or_disconnectable_storage(
    monkeypatch, tmp_path, kind
):
    monkeypatch.setattr(
        sonder_storage,
        "classify",
        lambda path: {
            "mount": str(tmp_path), "filesystem": "nfs" if kind == "network" else "exfat",
            "drive_type": kind, "network": kind == "network",
            "removable": kind == "removable",
        },
    )
    record = sonder_storage.inspect_root(tmp_path, role="models")
    assert record["warnings"]
    assert kind in " ".join(record["warnings"])


def test_inspection_reports_free_space_threshold(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sonder_storage.shutil, "disk_usage",
        lambda path: SimpleNamespace(total=1000, used=900, free=100),
    )
    monkeypatch.setattr(
        sonder_storage, "classify",
        lambda path: {"mount": str(path), "filesystem": "ext4",
                      "drive_type": "local", "network": False,
                      "removable": False},
    )
    record = sonder_storage.inspect_root(tmp_path, minimum_free_bytes=101)
    assert "below required minimum" in record["warnings"][0]


def test_explicit_probe_obeys_fixed_byte_cap_and_cleans_temp(tmp_path):
    before = set(tmp_path.iterdir())
    result = sonder_storage.throughput_probe(tmp_path)
    assert result["bytes"] == sonder_storage.PROBE_BYTES
    assert result["read_bytes"] == sonder_storage.PROBE_BYTES
    assert result["timeout_seconds"] == sonder_storage.PROBE_TIMEOUT_SECONDS
    assert set(tmp_path.iterdir()) == before


def test_probe_timeout_still_cleans_temp(monkeypatch, tmp_path):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(sonder_storage.subprocess, "run", timeout)
    with pytest.raises(TimeoutError, match="second cap"):
        sonder_storage.throughput_probe(tmp_path)
    assert list(tmp_path.iterdir()) == []
