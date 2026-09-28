"""The Windows low-integrity candidate must not read Sonder's state secrets.

The candidate token keeps the user's SID, and Windows files default to a
medium label with no-write-up only, so a candidate test could open
``<SONDER_HOME>/secrets.env``, ``fleet-principal.json`` or ``memory.db`` by
absolute path. The supervisor now labels the state home's secret stores
no-read-up before every run and refuses to run when one stays readable.
Every path here is a pytest temp directory.
"""
from __future__ import annotations

import os
import sys

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows MIC boundary")


@pytest.fixture()
def state_home(tmp_path, monkeypatch):
    home = tmp_path / "state"
    home.mkdir()
    for name in ("secrets.env", "fleet-principal.json", "memory.db"):
        (home / name).write_text("SONDER_API_KEY=sk-test\n", encoding="utf-8")
    monkeypatch.setenv("SONDER_HOME", str(home))
    return home


@pytest.mark.parametrize("name", ["secrets.env", "fleet-principal.json", "memory.db"])
def test_low_candidate_cannot_read_state_secret(tmp_path, state_home, name):
    from scripts.selfmod_low_integrity import run_isolated

    target = state_home / name
    work = tmp_path / "cand"
    work.mkdir()
    command = [
        sys.executable, "-c",
        "from pathlib import Path; p=Path(%r)\n"
        "try:\n    p.read_bytes(); raise SystemExit(3)\n"
        "except PermissionError:\n    pass" % str(target),
    ]
    result = run_isolated(command, cwd=work, timeout=20)
    assert result["passed"] is True, result["output"]


def test_supervisor_refuses_when_a_state_secret_stays_readable(tmp_path, state_home, monkeypatch):
    import scripts.selfmod_low_integrity as supervisor
    from sonder_runtime.platform import private_files

    monkeypatch.setattr(
        private_files, "protect_state_from_low_integrity",
        lambda home: [str(state_home / "secrets.env")],
    )
    with pytest.raises(RuntimeError, match="readable at low integrity"):
        supervisor.run_isolated([sys.executable, "-c", "pass"], cwd=tmp_path, timeout=10)
