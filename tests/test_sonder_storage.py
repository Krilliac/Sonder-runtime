from __future__ import annotations

import io
import inspect
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters import storage as sonder_storage


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


def test_linux_partition_inherits_parent_disk_removable_flag(tmp_path):
    sys_root = tmp_path / "sys" / "dev" / "block"
    disk = tmp_path / "sys" / "devices" / "disk0"
    partition = disk / "disk0p1"
    sys_root.mkdir(parents=True)
    partition.mkdir(parents=True)
    (disk / "removable").write_bytes(b"1\n")
    synthetic_device = "8_1"  # Windows cannot create a colon-bearing fixture.
    (sys_root / synthetic_device).symlink_to(
        partition, target_is_directory=True
    )
    assert sonder_storage._linux_removable(
        synthetic_device, sys_block_root=sys_root
    ) is True


def test_macos_fallback_is_metadata_only(tmp_path):
    result = sonder_storage.classify(tmp_path, system="Darwin")
    assert result["drive_type"] == "local-unknown"
    assert result["network"] is False
    assert result["removable"] is False


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
    assert result["temporary_file"] == "isolated-handle"
    assert set(tmp_path.iterdir()) == before


def test_probe_wall_timeout_kills_blocked_setup(monkeypatch, tmp_path):
    monkeypatch.setattr(sonder_storage, "PROBE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(
        sonder_storage, "_PROBE_WORKER", "import time; time.sleep(60)"
    )
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="second cap"):
        sonder_storage.throughput_probe(tmp_path)
    assert time.monotonic() - started < 0.5
    assert list(tmp_path.iterdir()) == []


def test_probe_wall_timeout_kills_blocked_io_and_cleans_handle(
    monkeypatch, tmp_path
):
    worker = (
        "import os,sys,tempfile,time\n"
        "with tempfile.TemporaryFile(dir=sys.argv[1]) as f:\n"
        " os.write(f.fileno(), b'x' * 4096)\n"
        " time.sleep(60)\n"
    )
    monkeypatch.setattr(sonder_storage, "PROBE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(sonder_storage, "_PROBE_WORKER", worker)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="second cap"):
        sonder_storage.throughput_probe(tmp_path)
    assert time.monotonic() - started < 0.5
    assert list(tmp_path.iterdir()) == []


def test_probe_never_reopens_path_or_unlinks_hardlink_competitor(tmp_path):
    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"valuable")
    competitor = tmp_path / ".sonder-storage-probe-competitor.tmp"
    try:
        os.link(victim, competitor)
    except OSError as exc:
        pytest.skip("hardlinks unavailable on this test filesystem: %s" % exc)
    result = sonder_storage.throughput_probe(tmp_path)
    assert result["bytes"] == sonder_storage.PROBE_BYTES
    assert competitor.read_bytes() == b"valuable"
    assert victim.read_bytes() == b"valuable"


def test_probe_through_symlink_never_touches_existing_files(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    victim = target / "victim.bin"
    victim.write_bytes(b"valuable")
    link = tmp_path / "storage-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip("directory symlinks unavailable: %s" % exc)
    result = sonder_storage.throughput_probe(link)
    assert result["bytes"] == sonder_storage.PROBE_BYTES
    assert victim.read_bytes() == b"valuable"


def test_probe_ignores_hostile_python_environment(monkeypatch, tmp_path):
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    marker = tmp_path / "sitecustomize-ran"
    (hostile / "sitecustomize.py").write_text(
        "from pathlib import Path\nPath(%r).write_text('ran')\n" % str(marker),
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(hostile))
    monkeypatch.setenv("SONDER_API_KEY", "must-not-reach-a-child")
    seen_env = {}
    real_popen = sonder_storage.subprocess.Popen

    def capture_env(*args, **kwargs):
        seen_env.update(kwargs["env"])
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(sonder_storage.subprocess, "Popen", capture_env)
    sonder_storage.throughput_probe(tmp_path)
    assert not marker.exists()
    assert "PYTHONPATH" not in seen_env
    assert "SONDER_API_KEY" not in seen_env


def test_probe_rejects_excessive_worker_output_with_bounded_reader(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        sonder_storage, "_PROBE_WORKER",
        "import sys; sys.stdout.buffer.write(b'x' * 10000000)",
    )
    started = time.monotonic()
    with pytest.raises(OSError, match="failed|invalid|excessive"):
        sonder_storage.throughput_probe(tmp_path)
    assert time.monotonic() - started < 1.0


def test_probe_child_is_isolated_and_output_is_strictly_bounded():
    source = inspect.getsource(sonder_storage.throughput_probe)
    assert "capture_output" not in source
    assert "NamedTemporaryFile" not in source
    assert '"-I", "-S", "-E"' in source
    assert "stderr=subprocess.DEVNULL" in source
    reader = inspect.getsource(sonder_storage._bounded_worker_output)
    assert "_PROBE_RESULT.size + 1" in reader
