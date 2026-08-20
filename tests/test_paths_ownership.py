import ast
from pathlib import Path

import sonder_paths
import sonder_runtime.platform.paths as packaged_paths


PUBLIC_PATH_SYMBOLS = (
    "bash_executable",
    "default_machine_home",
    "default_home",
    "ensure_home",
    "macos_default_home",
    "memory_db_path",
    "state_path",
    "windows_program_files",
    "windows_system_drive",
)


def test_root_paths_is_the_packaged_module_identity():
    assert sonder_paths is packaged_paths
    assert Path(packaged_paths.__file__).match("sonder_runtime/platform/paths.py")
    for name in PUBLIC_PATH_SYMBOLS:
        assert getattr(sonder_paths, name) is getattr(packaged_paths, name)


def test_root_paths_is_only_a_compatibility_alias():
    tree = ast.parse(Path("sonder_paths.py").read_text(encoding="utf-8"))
    assert not [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]


def test_packaged_paths_preserves_environment_overrides(monkeypatch, tmp_path):
    home = tmp_path / "home"
    db = tmp_path / "custom.db"
    state = tmp_path / "custom-state"
    monkeypatch.setenv("SONDER_HOME", str(home))
    monkeypatch.setenv("SONDER_DB", str(db))
    monkeypatch.setenv("SONDER_MACHINE_HOME", str(tmp_path / "machine"))
    monkeypatch.setenv("SONDER_STATE", str(state))

    assert packaged_paths.default_home() == home
    assert packaged_paths.memory_db_path() == str(db)
    assert packaged_paths.state_path("ignored", "SONDER_STATE") == str(state)
    assert packaged_paths.default_machine_home() == tmp_path / "machine"
