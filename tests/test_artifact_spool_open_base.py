from __future__ import annotations

import os
import subprocess

import pytest

from sonder_runtime.application.compute_fabric.artifact_spool import (
    ArtifactSpoolError,
    PrivateDirectoryAnchor,
)


def _link_dir(link, target) -> None:
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip("cannot create junction")
    else:
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError:
            pytest.skip("cannot create symlink")


def test_open_base_accepts_plain_directory(tmp_path):
    anchor = PrivateDirectoryAnchor.open_base(tmp_path / "plain" / "spool")
    try:
        assert anchor.path.name == "spool"
    finally:
        anchor.close()


def test_open_base_rejects_linked_parent(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    _link_dir(link, real)
    with pytest.raises(ArtifactSpoolError):
        PrivateDirectoryAnchor.open_base(link / "spool")
    assert not (real / "spool").exists()


def test_open_base_rejects_linked_grandparent(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    _link_dir(link, real)
    with pytest.raises(ArtifactSpoolError):
        PrivateDirectoryAnchor.open_base(link / "sub" / "spool")
    assert not (real / "sub").exists()
