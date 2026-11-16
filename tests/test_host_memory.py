"""Host memory readers: parsers on every platform, live reads on this one."""
import os
import sys

import pytest

from sonder_runtime.platform import host_memory

MEMINFO = """MemTotal:       32768000 kB
MemFree:         1000000 kB
MemAvailable:   20000000 kB
CommitLimit:    40000000 kB
Committed_AS:   25000000 kB
"""


def test_meminfo_reports_available_and_commit_headroom():
    reading = host_memory.parse_meminfo(MEMINFO, overcommit_mode=2)
    assert reading.available_physical_bytes == 20_000_000 * 1024
    assert reading.total_physical_bytes == 32_768_000 * 1024
    assert reading.commit_limit_bytes == 40_000_000 * 1024
    assert reading.commit_available_bytes == 15_000_000 * 1024
    assert reading.commit_enforced is True


@pytest.mark.parametrize("mode", [0, 1, None])
def test_commit_is_not_enforced_unless_overcommit_mode_is_two(mode):
    assert host_memory.parse_meminfo(MEMINFO, overcommit_mode=mode).commit_enforced is False


def test_commit_headroom_never_goes_negative():
    text = MEMINFO.replace("25000000", "50000000")
    assert host_memory.parse_meminfo(text, 2).commit_available_bytes == 0


def test_meminfo_without_memavailable_is_unreadable():
    assert host_memory.parse_meminfo("MemTotal: 1 kB\n", 2) is None


def test_process_status_and_smaps_parsers():
    assert host_memory.parse_proc_status_rss("Name:\tx\nVmRSS:\t   2048 kB\n") == 2048 * 1024
    assert host_memory.parse_proc_status_rss("Name:\tx\n") is None
    rollup = "Rss: 10 kB\nPrivate_Clean: 100 kB\nPrivate_Dirty: 28 kB\n"
    assert host_memory.parse_smaps_private(rollup) == 128 * 1024
    assert host_memory.parse_smaps_private("Rss: 10 kB\n") is None


@pytest.mark.skipif(not (sys.platform == "win32" or sys.platform.startswith("linux")),
                    reason="no host memory reader on this platform")
def test_live_host_memory_is_plausible():
    reading = host_memory.read_host_memory()
    assert reading is not None
    assert 0 < reading.available_physical_bytes <= reading.total_physical_bytes
    assert reading.commit_limit_bytes >= 0


@pytest.mark.skipif(not (sys.platform == "win32" or sys.platform.startswith("linux")),
                    reason="no process memory reader on this platform")
def test_live_process_memory_of_this_process():
    own = host_memory.process_memory(os.getpid())
    assert own is not None
    assert own.working_set_bytes > 1_000_000
    assert own.private_bytes > 0


@pytest.mark.parametrize("pid", [0, -1, True, "12", 2 ** 31 - 2])
def test_unreadable_or_invalid_pids_return_none(pid):
    assert host_memory.process_memory(pid) is None
