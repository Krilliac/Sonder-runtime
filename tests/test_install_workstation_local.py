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


def test_installer_provisions_a_separate_opt_in_managed_runtime_profile():
    text = _text()

    assert "[switch] $ManagedRuntime" in text
    assert "sys.version_info[:2] == (3, 12)" in text
    assert "Join-Path $repo 'venv-managed'" in text
    assert "'-r', $requirementsFile" in text
    assert "'sonder_runtime.adapters.execution.runtime_profile', 'seal'" in text
    assert "'sonder_runtime.adapters.execution.runtime_profile', 'verify'" in text
    assert "ManagedRuntimeOwner.workstation_local" in text


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


def _fake_checkout(root: Path) -> tuple[Path, Path]:
    """A minimal checkout the installer accepts, holding a sentinel file."""
    packaging = root / "packaging"
    packaging.mkdir(parents=True)
    script_copy = packaging / "install_workstation_local.ps1"
    script_copy.write_text(_text(), encoding="utf-8")
    (root / "requirements-runtime.txt").write_text("", encoding="utf-8")
    (root / "sonder_version.py").write_text("", encoding="utf-8")
    sentinel = root / "irreplaceable.txt"
    sentinel.write_text("keep", encoding="utf-8")
    return script_copy, sentinel


def _run_force(script_copy: Path, venv_path: str, cwd: Path):
    import sys

    environment = os.environ.copy()
    # Any accidental dependency install must fail fast and offline.
    environment["PIP_INDEX_URL"] = "http://127.0.0.1:1/simple"
    environment["PIP_NO_INPUT"] = "1"
    return subprocess.run(
        [
            "powershell", "-NoProfile", "-File", str(script_copy),
            "-Python", sys.executable, "-VenvPath", venv_path,
            "-Force", "-SkipModelAlias",
        ],
        cwd=str(cwd), env=environment, capture_output=True, text=True, timeout=300,
    )


@pytest.mark.skipif(os.name != "nt", reason="PowerShell installer for Windows checkouts")
def test_force_refuses_a_venv_path_that_is_not_a_virtual_environment(tmp_path):
    script_copy, _ = _fake_checkout(tmp_path / "repo")
    victim = tmp_path / "documents"
    victim.mkdir()
    (victim / "thesis.docx").write_text("years of work", encoding="utf-8")

    result = _run_force(script_copy, str(victim), tmp_path)

    assert result.returncode != 0
    assert "pyvenv.cfg" in (result.stdout + result.stderr)
    assert (victim / "thesis.docx").read_text(encoding="utf-8") == "years of work"


@pytest.mark.skipif(os.name != "nt", reason="PowerShell installer for Windows checkouts")
@pytest.mark.parametrize("which", ["dot", "repo", "parent"])
def test_force_refuses_the_checkout_or_a_folder_containing_it(tmp_path, which):
    repo = tmp_path / "repo"
    script_copy, sentinel = _fake_checkout(repo)
    target = {"dot": ".", "repo": str(repo), "parent": str(tmp_path)}[which]
    # Even a planted venv marker must not make the checkout deletable.
    marker_dir = tmp_path if which == "parent" else repo
    (marker_dir / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")

    result = _run_force(script_copy, target, tmp_path)

    assert result.returncode != 0
    assert "refusing to delete" in (result.stdout + result.stderr)
    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(os.name != "nt", reason="PowerShell installer for Windows checkouts")
def test_force_refuses_a_junction_even_to_a_venv(tmp_path):
    script_copy, _ = _fake_checkout(tmp_path / "repo")
    real = tmp_path / "real-venv"
    real.mkdir()
    (real / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
    link = tmp_path / "link-venv"
    made = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(real)],
        capture_output=True, text=True,
    )
    if made.returncode != 0:
        pytest.skip("junctions unavailable")

    result = _run_force(script_copy, str(link), tmp_path)

    assert result.returncode != 0
    assert "junction" in (result.stdout + result.stderr)
    assert (real / "pyvenv.cfg").is_file()


def test_installers_verify_the_resolved_dependency_set():
    text = _text()
    assert "'-m', 'pip', 'check'" in text
    check = text.index("'verifying managed runtime dependencies'")
    assert check < text.index("'sonder_runtime.adapters.execution.runtime_profile', 'seal'")
