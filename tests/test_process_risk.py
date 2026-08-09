import json
import os
import subprocess
import sys
import time

import pytest

import process_risk


def test_memory_inspection_requires_exact_opt_in(monkeypatch):
    monkeypatch.delenv(process_risk.OPT_IN_ENV, raising=False)
    result = process_risk.inspect_process_memory(12345)
    assert result["status"] == "opt_in_required"


def test_process_list_requires_exact_opt_in(monkeypatch):
    monkeypatch.delenv(process_risk.OPT_IN_ENV, raising=False)
    assert process_risk.list_processes()["status"] == "opt_in_required"
    monkeypatch.setenv(process_risk.OPT_IN_ENV, "1")
    assert process_risk.list_processes()["status"] == "opt_in_required"

    monkeypatch.setenv(process_risk.OPT_IN_ENV, "1")
    result = process_risk.inspect_process_memory(12345)
    assert result["status"] == "opt_in_required"


@pytest.mark.parametrize("pid", [0, 4, -1])
def test_memory_inspection_rejects_protected_pids(monkeypatch, pid):
    monkeypatch.setenv(process_risk.OPT_IN_ENV, process_risk.OPT_IN_VALUE)
    result = process_risk.inspect_process_memory(pid)
    assert result["status"] == "protected_pid"


def test_memory_inspection_rejects_self(monkeypatch):
    monkeypatch.setenv(process_risk.OPT_IN_ENV, process_risk.OPT_IN_VALUE)
    result = process_risk.inspect_process_memory(os.getpid())
    assert result["status"] == "protected_pid"


def test_process_list_is_bounded_and_has_no_sensitive_fields(monkeypatch):
    monkeypatch.setenv(process_risk.OPT_IN_ENV, process_risk.OPT_IN_VALUE)
    result = process_risk.list_processes(max_processes=2, max_seconds=0.25)
    if os.name != "nt":
        assert result["status"] == "unsupported_platform"
        return
    assert result["ok"] is True
    assert result["process_count"] <= 2
    serialized = json.dumps(result).lower()
    for forbidden in ("command_line", "cmdline", "executable_path", "username"):
        assert forbidden not in serialized
    assert all(
        set(item) == {"pid", "parent_pid", "name", "thread_count"}
        for item in result["processes"]
    )


def test_limits_are_clamped(monkeypatch):
    monkeypatch.setenv(process_risk.OPT_IN_ENV, process_risk.OPT_IN_VALUE)
    if os.name != "nt":
        pytest.skip("Windows memory inspection")
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        result = process_risk.inspect_process_memory(
            child.pid, max_bytes=10**12, max_regions=10**9, max_seconds=99
        )
        assert result["limits"] == {
            "max_bytes": 16 * 1024 * 1024,
            "max_regions": 512,
            "max_seconds": 3.0,
        }
    finally:
        child.terminate()
        child.wait(timeout=5)


@pytest.mark.parametrize("value", [True, 1.5, "4096", float("inf")])
def test_malformed_memory_limits_fail_closed(monkeypatch, value):
    monkeypatch.setenv(process_risk.OPT_IN_ENV, process_risk.OPT_IN_VALUE)
    with pytest.raises(ValueError):
        process_risk.inspect_process_memory(1234, max_bytes=value)

def test_harmless_synthetic_injection_markers_are_detected_without_content(
    monkeypatch,
):
    if os.name != "nt":
        pytest.skip("Windows memory inspection")
    monkeypatch.setenv(process_risk.OPT_IN_ENV, process_risk.OPT_IN_VALUE)
    child_code = (
        "import base64,sys,time;"
        "payload=bytearray(base64.b64decode(sys.stdin.buffer.readline()));"
        "print('ready', flush=True);"
        "time.sleep(20)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    # The child only allocates inert text markers.  It performs no process access,
    # executable allocation, injection, persistence, or network activity.
    import base64

    marker_blob = (
        b"SYNTHETIC_CANARY_DO_NOT_EXPOSE::"
        + b"WriteProcessMemory\x00VirtualAllocEx\x00CreateRemoteThread\x00"
    ) * 128
    try:
        assert child.stdin is not None
        child.stdin.write(base64.b64encode(marker_blob) + b"\n")
        child.stdin.flush()
        assert child.stdout is not None
        assert child.stdout.readline().strip() == b"ready"
        time.sleep(0.05)
        result = process_risk.inspect_process_memory(
            child.pid,
            max_bytes=16 * 1024 * 1024,
            max_regions=512,
            max_seconds=3.0,
        )
        assert result["ok"] is True
        assert result["risk"] == "high"
        assert {
            "cross_process_memory_write_primitive",
            "remote_thread_primitive",
            "remote_executable_allocation_primitive",
        }.issubset(result["indicators"])
        serialized = json.dumps(result)
        assert "SYNTHETIC_CANARY_DO_NOT_EXPOSE" not in serialized
        assert "WriteProcessMemory" not in serialized
        assert "VirtualAllocEx" not in serialized
        assert "CreateRemoteThread" not in serialized
        assert "0x" not in serialized
    finally:
        child.terminate()
        child.wait(timeout=5)
