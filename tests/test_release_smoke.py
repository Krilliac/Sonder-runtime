import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PS1 = ROOT / "scripts" / "release_smoke.ps1"
SH = ROOT / "scripts" / "release_smoke.sh"


def test_both_platform_variants_chain_version_policy_and_smoke_and_never_touch_ollama():
    for text in (PS1.read_text(encoding="utf-8"), SH.read_text(encoding="utf-8")):
        assert "check_release_version.py" in text
        assert "sonder_runtime smoke" in text or "sonder_runtime', 'smoke'" in text
        assert "--skip-ollama" in text
        assert "--tag" in text
        assert "--require-release" in text


def test_both_variants_fail_closed_when_either_check_fails():
    ps1 = PS1.read_text(encoding="utf-8")
    sh = SH.read_text(encoding="utf-8")

    assert "exit 1" in ps1
    assert "exit 1" in sh
    # Both checks always run (no short-circuit exit after the first failure) so
    # one release-smoke invocation reports every problem, not just the first.
    assert ps1.index("check_release_version.py") < ps1.index("sonder_runtime smoke")
    assert ps1.index("$failed") < ps1.rindex("exit 1")


@pytest.mark.skipif(os.name != "nt", reason="requires a Windows PowerShell host")
def test_ps1_smoke_check_passes_on_this_development_checkout():
    result = subprocess.run(
        ["powershell", "-NoProfile", "-File", str(PS1)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS: version policy and runtime smoke both succeeded" in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="requires a Windows PowerShell host")
def test_ps1_smoke_check_fails_closed_when_release_is_required_without_a_tag():
    result = subprocess.run(
        ["powershell", "-NoProfile", "-File", str(PS1), "-RequireRelease"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAIL: version policy" in result.stdout


def test_sh_smoke_check_passes_on_this_development_checkout_with_explicit_python():
    if os.name == "nt":
        python = os.environ.get("SONDER_PYTHON") or ""
        if not python:
            pytest.skip("no non-alias SONDER_PYTHON configured for Git Bash on Windows")
    else:
        python = ""
    environment = os.environ.copy()
    if python:
        environment["SONDER_PYTHON"] = python

    result = subprocess.run(
        ["sh", str(SH)],
        cwd=str(ROOT),
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS: version policy and runtime smoke both succeeded" in result.stdout
