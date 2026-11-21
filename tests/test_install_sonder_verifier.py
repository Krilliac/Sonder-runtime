"""The root installer must never execute code from the package it verifies.

install_sonder.sh used ``PYTHONPATH=$PACKAGE_SOURCE python3 ... from scripts
import package_local_system``: the verifier was imported from the untrusted
package, so its module code ran as root before any manifest check.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "packaging" / "install_sonder.sh"


def _verification_block() -> str:
    text = INSTALLER.read_text(encoding="utf-8")
    match = re.search(
        r'^mkdir "\$RELEASE_DIR"\n(.*?)^python3 -m venv "\$RELEASE_DIR/venv"$',
        text, flags=re.DOTALL | re.MULTILINE,
    )
    assert match, "installer verification block not found"
    return match.group(1)


def _posix(path: Path) -> str:
    return path.as_posix()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash unavailable")
def test_installer_does_not_import_the_package_verifier(tmp_path):
    marker = tmp_path / "package-code-ran-as-root"
    package = tmp_path / "package"
    (package / "scripts").mkdir(parents=True)
    (package / "PACKAGE-MANIFEST.json").write_text("{}", encoding="utf-8")
    (package / "scripts" / "package_local_system.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('pwned')\n"
        "def copy_verified_payload(src, dest):\n"
        "    pass\n",
        encoding="utf-8",
    )
    staging = tmp_path / "staging"
    staging.mkdir()
    script = (
        "set -euo pipefail\n"
        f'SCRIPT_DIR="{_posix(INSTALLER.parent)}"\n'
        f'PACKAGE_SOURCE="{_posix(package)}"\n'
        f'RELEASE_DIR="{_posix(staging)}"\n'
        # Stand-in for the system python3 (Git Bash's is the Store stub).
        f'python3() {{ "{_posix(Path(sys.executable))}" "$@"; }}\n'
        + _verification_block()
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(package)  # a hostile environment must not matter

    script_file = tmp_path / "verify.sh"
    script_file.write_bytes(script.encode("utf-8"))

    proc = subprocess.run(
        # The resolved path, not "bash": CreateProcess searches System32 first
        # and would pick WSL's bash.exe, which cannot see these paths.
        [shutil.which("bash"), _posix(script_file)], text=True, capture_output=True,
        timeout=120, env=env, cwd=str(tmp_path), check=False,
    )

    assert not marker.exists(), "package code executed during verification"
    # The trusted verifier rejected the bogus manifest and staged nothing.
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "schema" in proc.stderr
    assert list(staging.iterdir()) == []


def test_installer_verifier_comes_from_the_installer_tree():
    block = _verification_block()
    assert "PYTHONPATH=\"$PACKAGE_SOURCE\"" not in block
    assert "python3 -I" in block
    assert 'TRUSTED_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"' in block


def test_installer_checks_and_records_the_resolved_dependency_set():
    text = INSTALLER.read_text(encoding="utf-8")
    install = text.index('install --quiet -r "$RELEASE_DIR/requirements-runtime.txt"')
    check = text.index('"$RELEASE_DIR/venv/bin/python" -m pip check')
    freeze = text.index("INSTALLED-REQUIREMENTS.txt")
    publish = text.index('ln -sfn "$RELEASE_DIR" /opt/sonder/current')
    assert install < check < publish
    assert install < freeze < publish


def test_release_is_built_at_its_final_path_with_no_staging_rename():
    text = INSTALLER.read_text(encoding="utf-8")
    assert "STAGING" not in text
    assert 'mv "$' not in text
    assert 'python3 -m venv "$RELEASE_DIR/venv"' in text


def test_preflight_runs_before_current_symlink_and_units_are_touched():
    text = INSTALLER.read_text(encoding="utf-8")
    preflight = text.index('(cd "$RELEASE_DIR" && sudo -u sonder')
    for step in (
        'ln -sfn "$RELEASE_DIR" /opt/sonder/current',
        "/etc/systemd/system/",
        "systemctl daemon-reload",
        "RELEASE_COMMITTED=1",
    ):
        assert preflight < text.index(step), step
    # sonder_runtime is not installed in the venv; it is importable only from
    # the release directory (the systemd units use WorkingDirectory for this).
    assert '(cd "$RELEASE_DIR" && sudo -u sonder' in text
    assert "(cd / && sudo -u sonder" not in text


def test_preflight_working_directory_can_import_sonder_runtime():
    text = INSTALLER.read_text(encoding="utf-8")
    match = re.search(r"^if ! \(cd (\S+) && sudo -u sonder ", text, re.MULTILINE)
    assert match, "preflight command not found"
    workdir = match.group(1).strip('"').replace("$RELEASE_DIR", str(REPO))
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    code = (
        "import importlib.util, sys; "
        "sys.exit(0 if importlib.util.find_spec('sonder_runtime') else 1)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=workdir, env=env, check=False,
        capture_output=True,
    )
    assert proc.returncode == 0, f"{workdir!r} cannot import sonder_runtime"


def test_exit_trap_removes_an_uncommitted_release():
    text = INSTALLER.read_text(encoding="utf-8")
    assert "trap cleanup_release EXIT" in text
    assert text.index("trap cleanup_release EXIT") < text.index('mkdir "$RELEASE_DIR"')
    assert 'rm -r -- "$RELEASE_DIR"' in text
    assert text.index("RELEASE_CREATED=1") < text.index("RELEASE_COMMITTED=1")


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash unavailable")
def test_cleanup_trap_behaviour(tmp_path):
    text = INSTALLER.read_text(encoding="utf-8")
    match = re.search(r"^cleanup_release\(\) \{.*?^\}$", text, re.DOTALL | re.MULTILINE)
    assert match
    release = tmp_path / "rel"

    def run(committed: int) -> None:
        release.mkdir(exist_ok=True)
        script = "\n".join([
            f'RELEASE_DIR="{_posix(release)}"',
            "RELEASE_CREATED=1",
            f"RELEASE_COMMITTED={committed}",
            match.group(0),
            "trap cleanup_release EXIT",
            "exit 1",
            "",
        ])
        f = tmp_path / "t.sh"
        f.write_bytes(script.encode("utf-8"))
        subprocess.run([shutil.which("bash"), _posix(f)], capture_output=True, timeout=60, check=False)

    run(0)
    assert not release.exists()
    run(1)
    assert release.exists()
