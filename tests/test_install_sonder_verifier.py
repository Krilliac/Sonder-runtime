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
        r'^mkdir -p "\$STAGING"\n(.*?)^python3 -m venv "\$STAGING/venv"$',
        text, flags=re.S | re.M,
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
        f'STAGING="{_posix(staging)}"\n'
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
        timeout=120, env=env, cwd=str(tmp_path),
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
    install = text.index('install --quiet -r "$STAGING/requirements-runtime.txt"')
    check = text.index('"$STAGING/venv/bin/python" -m pip check')
    freeze = text.index("INSTALLED-REQUIREMENTS.txt")
    publish = text.index('mv "$STAGING" "$RELEASE_DIR"')
    assert install < check < publish
    assert install < freeze < publish
