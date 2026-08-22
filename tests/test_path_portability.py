from pathlib import Path

import sonder_runtime.adapters.updates.engine as sonder_update_engine
import sonder_runtime.platform.paths as sonder_paths


def test_update_defaults_follow_platform_machine_home(monkeypatch, tmp_path):
    machine_home = tmp_path / "machine-state"
    monkeypatch.delenv("SONDER_RELEASES_DIR", raising=False)
    monkeypatch.delenv("SONDER_CURRENT_LINK", raising=False)
    monkeypatch.setattr(sonder_paths, "default_machine_home", lambda: machine_home)

    assert sonder_update_engine.default_releases_dir() == machine_home / "releases"
    assert sonder_update_engine.default_current_link() == machine_home / "current"


def test_update_path_overrides_expand_user(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RELEASES_DIR", str(tmp_path / "custom-releases"))
    monkeypatch.setenv("SONDER_CURRENT_LINK", str(tmp_path / "custom-current"))

    assert sonder_update_engine.default_releases_dir() == Path(
        tmp_path / "custom-releases"
    )
    assert sonder_update_engine.default_current_link() == Path(
        tmp_path / "custom-current"
    )
