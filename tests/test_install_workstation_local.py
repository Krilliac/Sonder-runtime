import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packaging" / "install_workstation_local.ps1"


def _text():
    return SCRIPT.read_text(encoding="utf-8")


def test_installer_never_bypasses_execution_policy_or_touches_git_history():
    text = _text()

    assert "-ExecutionPolicy Bypass" not in text
    assert "ExecutionPolicy" not in text
    for destructive in ("git reset", "git clean", "git checkout .", "rm -rf", "Remove-Item -Recurse -Force $repo"):
        assert destructive not in text


def test_installer_reuses_an_existing_venv_unless_force_is_passed():
    text = _text()

    assert "[switch] $Force" in text
    assert "reusing existing venv" in text
    reuse_index = text.index("reusing existing venv")
    force_removal_index = text.index("removing existing venv")
    assert "if ($Force)" in text[force_removal_index - 40 : force_removal_index]
    assert reuse_index > force_removal_index


def test_installer_upgrades_pip_via_python_module_not_the_pip_exe_directly():
    # pip.exe cannot replace its own running executable on Windows; only
    # `python -m pip install --upgrade pip` works there. Regression coverage
    # for that exact failure, found by actually running this script.
    text = _text()

    assert "'-m', 'pip', 'install', '--quiet', '--upgrade', 'pip'" in text
    assert "-FilePath $venvPip " not in text


def test_installer_refuses_to_run_outside_a_sonder_checkout():
    text = _text()

    assert "requirements-runtime.txt" in text
    assert "sonder_version.py" in text
    assert "must run from packaging" in text


def test_installer_validates_minimum_python_version_without_embedded_quote_bug():
    # A quoted "%d.%d" % ... snippet passed through `py.exe -3 -c "..."` loses
    # its inner double quotes to the launcher's own reparsing (a real bug hit
    # while testing this script). The check must not depend on embedded quotes.
    text = _text()

    assert "sys.version_info[:2] >= (3, 11)" in text
    assert '"%d' not in text


@pytest.mark.skipif(os.name != "nt", reason="PowerShell installer for Windows checkouts")
def test_installer_runs_end_to_end_and_reuses_the_venv_on_a_second_run(tmp_path):
    venv_path = tmp_path / "venv"
    sonder_home = tmp_path / "sonder-home"
    environment = os.environ.copy()
    environment["SONDER_HOME"] = str(sonder_home)

    first = subprocess.run(
        [
            "powershell", "-NoProfile", "-File", str(SCRIPT),
            "-VenvPath", str(venv_path), "-SkipModelAlias",
        ],
        cwd=str(ROOT),
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert first.returncode == 0, first.stdout + first.stderr
    assert "Install complete" in first.stdout
    assert (venv_path / "Scripts" / "python.exe").is_file()
    assert sonder_home.is_dir()

    second = subprocess.run(
        [
            "powershell", "-NoProfile", "-File", str(SCRIPT),
            "-VenvPath", str(venv_path), "-SkipModelAlias",
        ],
        cwd=str(ROOT),
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert second.returncode == 0, second.stdout + second.stderr
    assert "reusing existing venv" in second.stdout


@pytest.mark.skipif(os.name != "nt", reason="PowerShell installer for Windows checkouts")
def test_installer_rejects_a_directory_that_is_not_a_sonder_checkout(tmp_path):
    fake_packaging = tmp_path / "packaging"
    fake_packaging.mkdir()
    script_copy = fake_packaging / "install_workstation_local.ps1"
    script_copy.write_text(_text(), encoding="utf-8")

    result = subprocess.run(
        [
            "powershell", "-NoProfile", "-File", str(script_copy),
            "-VenvPath", str(tmp_path / "venv"),
        ],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert "must run from packaging" in (result.stdout + result.stderr)
    assert not (tmp_path / "venv").exists()
