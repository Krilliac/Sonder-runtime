from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import sonder_runtime.__main__ as entrypoint
from sonder_runtime.platform import paths as runtime_paths


def test_entrypoint_uses_canonical_platform_paths_for_default_backup_target(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(entrypoint, "_load_config", lambda _args: None)
    monkeypatch.setattr(runtime_paths, "default_home", lambda: tmp_path)

    assert entrypoint._backup_target(SimpleNamespace(target=None)) == str(
        tmp_path / "backups"
    )


def test_entrypoint_preserves_explicit_backup_target_precedence(tmp_path):
    target = tmp_path / "explicit-backups"

    assert entrypoint._backup_target(SimpleNamespace(target=str(target))) == str(target)


def test_entrypoint_has_no_dynamic_root_path_import():
    source = entrypoint.__file__
    assert source is not None
    text = Path(source).read_text(encoding="utf-8")
    assert "import sonder_paths" not in text
