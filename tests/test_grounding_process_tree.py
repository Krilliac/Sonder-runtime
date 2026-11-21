"""Timeouts bound execution even when generated code leaves a grandchild
holding the output pipes (previously communicate() blocked unbounded)."""
import os
import sys
import time

import pytest

import grounding

_GRANDCHILD = (
    "import subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "print('parent-done', flush=True)\n"
    "time.sleep(60)\n"
)


def test_run_code_detail_timeout_bounded_with_grandchild():
    start = time.monotonic()
    result = grounding.run_code_detail(_GRANDCHILD, timeout=2)
    elapsed = time.monotonic() - start
    assert result["timed_out"] is True, result
    assert result["stdout"] == "parent-done"
    assert elapsed < 20, elapsed


def test_run_code_detail_orphaned_grandchild_cannot_stall():
    code = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print('exited')\n"
    )
    start = time.monotonic()
    result = grounding.run_code_detail(code, timeout=2)
    assert time.monotonic() - start < 20
    assert result["stdout"] == "exited"


def test_run_cmd_timeout_bounded_with_grandchild():
    start = time.monotonic()
    ok, output = grounding._run_cmd([sys.executable, "-c", _GRANDCHILD], 2)
    assert ok is False
    assert "timed out" in output
    assert time.monotonic() - start < 20


@pytest.mark.skipif(os.name != "nt", reason="Windows pipe reader threads")
def test_windows_taskkill_fallback_orphan_cannot_stall(monkeypatch):
    # Job assignment fails, so taskkill /T is used and cannot reach an orphan
    # whose parent already exited. Closing the pipes from the caller thread
    # would block until the grandchild exits (25s here).
    monkeypatch.setattr(grounding._WindowsJob, "assign", lambda self, proc: False)
    code = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(25)'])\n"
        "print('exited', flush=True)\n"
    )
    start = time.monotonic()
    result = grounding.run_code_detail(code, timeout=2)
    elapsed = time.monotonic() - start
    assert result["timed_out"] is True, result
    assert elapsed < 2 + grounding._DRAIN_GRACE + 6, elapsed
