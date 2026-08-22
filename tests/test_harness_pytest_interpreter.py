"""The test-discovery tool must survive a minimal embedded runtime venv."""
from __future__ import annotations

import sys

import harness_tools


def test_pytest_command_is_runnable():
    command = harness_tools._pytest_cmd()
    result = harness_tools._run(
        [command, "-m", "pytest", "--version"],
        cwd=".", timeout=10,
    )
    assert result["ok"], result
    assert "pytest" in (result["stdout"] + result["stderr"]).lower()
