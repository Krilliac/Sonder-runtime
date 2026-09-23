import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "run-nightly.ps1"


def _powershell():
    return shutil.which("pwsh") or shutil.which("powershell")


@pytest.mark.skipif(_powershell() is None, reason="PowerShell is required")
def test_nightly_wrapper_preflight_is_synchronous_and_logs(tmp_path):
    command = [
        _powershell(), "-NoProfile", "-File", str(WRAPPER),
        "-Python", sys.executable, "-LogDirectory", str(tmp_path), "-Preflight",
    ]
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    logs = sorted(tmp_path.glob("wrapper-*.log"))
    assert len(logs) == 1
    text = logs[0].read_text(encoding="utf-8")
    assert "nightly preflight" in text
    assert "finished" in text and "exit=0" in text


@pytest.mark.skipif(_powershell() is None, reason="PowerShell is required")
def test_nightly_wrapper_propagates_failed_preflight(tmp_path):
    bad_config = tmp_path / "broken.toml"
    bad_config.write_text("[ollama\n", encoding="utf-8")
    environment = os.environ.copy()
    environment["SONDER_CONFIG"] = str(bad_config)
    result = subprocess.run(
        [
            _powershell(), "-NoProfile", "-File", str(WRAPPER),
            "-Python", sys.executable, "-LogDirectory", str(tmp_path), "-Preflight",
        ],
        cwd=ROOT, text=True, capture_output=True, timeout=60, env=environment,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    logs = sorted(tmp_path.glob("wrapper-*.log"))
    assert len(logs) == 1
    text = logs[0].read_text(encoding="utf-8")
    assert "preflight FAILED" in text
    assert "exit=1" in text
