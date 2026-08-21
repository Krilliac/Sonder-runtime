import json
import os
import subprocess
import sys
import time

import pytest

import sonder_runtime.adapters.process_risk as process_risk


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


@pytest.mark.parametrize("field", ["max_bytes", "max_regions", "max_seconds"])
@pytest.mark.parametrize("value", [True, "4096", float("inf")])
def test_malformed_memory_limits_fail_closed(monkeypatch, field, value):
    monkeypatch.setenv(process_risk.OPT_IN_ENV, process_risk.OPT_IN_VALUE)
    with pytest.raises(ValueError):
        process_risk.inspect_process_memory(1234, **{field: value})


@pytest.mark.parametrize("field", ["max_bytes", "max_regions"])
def test_fractional_integer_memory_limits_fail_closed(monkeypatch, field):
    monkeypatch.setenv(process_risk.OPT_IN_ENV, process_risk.OPT_IN_VALUE)
    with pytest.raises(ValueError):
        process_risk.inspect_process_memory(1234, **{field: 1.5})


def _scan_inert_child(blob, *, executable=False):
    import base64

    child_code = (
        "import base64,ctypes,os,sys,time;"
        "payload=bytearray(base64.b64decode(sys.stdin.buffer.readline()));"
        "k=ctypes.windll.kernel32;"
        "k.VirtualAlloc.argtypes=(ctypes.c_void_p,ctypes.c_size_t,ctypes.c_ulong,ctypes.c_ulong);"
        "k.VirtualAlloc.restype=ctypes.c_void_p;"
        "protection=int(sys.argv[1]);"
        "address=k.VirtualAlloc(None,len(payload),0x3000,protection);"
        "ctypes.memmove(address,bytes(payload),len(payload));"
        "print('ready:'+str(os.getpid()), flush=True);"
        "time.sleep(20)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_code, "64" if executable else "4"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert child.stdin is not None
        child.stdin.write(base64.b64encode(blob) + b"\n")
        child.stdin.flush()
        assert child.stdout is not None
        ready = child.stdout.readline().strip()
        assert ready.startswith(b"ready:")
        target_pid = int(ready.split(b":", 1)[1])
        time.sleep(0.05)
        return process_risk.inspect_process_memory(
            target_pid, max_bytes=16 * 1024 * 1024,
            max_regions=512, max_seconds=3.0,
        )
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_benign_control_does_not_match_runtime_image_exports(monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows memory inspection")
    monkeypatch.setenv(process_risk.OPT_IN_ENV, process_risk.OPT_IN_VALUE)
    result = _scan_inert_child(b"BENIGN_CONTROL" * 128)
    assert result["ok"] is True
    assert result["risk"] in {"none", "unknown"}
    assert result["indicators"] == []


def test_overlap_counter_counts_only_matches_touching_new_chunk():
    marker = b"short"
    tail = b"xxshortyysho"
    chunk = b"rtzzshort"
    assert process_risk._count_new_matches(tail + chunk, marker, len(tail)) == 2

def test_harmless_synthetic_injection_markers_are_detected_without_content(
    monkeypatch,
):
    if os.name != "nt":
        pytest.skip("Windows memory inspection")
    monkeypatch.setenv(process_risk.OPT_IN_ENV, process_risk.OPT_IN_VALUE)
    # The child only allocates inert text markers.  It performs no process access,
    # executable allocation, injection, persistence, or network activity.
    marker_blob = (
        b"SYNTHETIC_CANARY_DO_NOT_EXPOSE::"
        + b"WriteProcessMemory\x00VirtualAllocEx\x00CreateRemoteThread\x00"
    ) * 128
    result = _scan_inert_child(marker_blob, executable=True)
    assert result["ok"] is True
    assert result["risk"] == "high"
    assert "private_writable_executable_region" in result["indicators"]
    serialized = json.dumps(result)
    assert "SYNTHETIC_CANARY_DO_NOT_EXPOSE" not in serialized
    assert "WriteProcessMemory" not in serialized
    assert "VirtualAllocEx" not in serialized
    assert "CreateRemoteThread" not in serialized
    assert "0x" not in serialized
