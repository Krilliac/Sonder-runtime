"""install_sonder.sh must leave sonder.env owned by sonder, in a mode load_config accepts."""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from sonder_runtime.platform.config import ConfigError, load_config

REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "packaging" / "install_sonder.sh"
ENV_PATH = r"/etc/sonder/sonder\.env"


def _installer_mode() -> int:
    text = INSTALLER.read_text(encoding="utf-8")
    match = re.search(rf"^chmod ([0-7]{{3,4}}) {ENV_PATH}$", text, re.MULTILINE)
    assert match, "installer must chmod sonder.env"
    return int(match.group(1), 8)


def test_secrets_file_owned_by_sonder_and_mode_has_no_group_world_bits():
    text = INSTALLER.read_text(encoding="utf-8")
    assert re.search(rf"^chown sonder:sonder {ENV_PATH}$", text, re.MULTILINE)
    assert _installer_mode() & 0o077 == 0
    # Both must precede the preflight that runs as `sudo -u sonder`.
    assert text.index("chown sonder:sonder") < text.index('(cd "$RELEASE_DIR" && sudo -u sonder')
    assert text.index("chmod 0600") < text.index('(cd "$RELEASE_DIR" && sudo -u sonder')


@pytest.mark.skipif(os.name != "posix", reason="mode check is POSIX-only")
def test_load_config_accepts_the_mode_the_installer_sets(tmp_path):
    secrets = tmp_path / "sonder.env"
    secrets.write_text("SONDER_API_KEY=x\nSONDER_AUTH_SECRET=y\n", encoding="utf-8")
    secrets.chmod(_installer_mode())
    try:
        load_config(secrets_path=secrets)
    except ConfigError as exc:
        assert "group/world" not in str(exc)
