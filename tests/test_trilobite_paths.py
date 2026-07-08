import os

import trilobite_paths


def test_memory_db_uses_trilobite_home(monkeypatch, tmp_path):
    home = tmp_path / "state"
    monkeypatch.setenv("TRILOBITE_HOME", str(home))
    monkeypatch.delenv("TRILOBITE_DB", raising=False)

    assert trilobite_paths.memory_db_path() == str(home / "memory.db")
    assert home.exists()


def test_memory_db_env_override_wins(monkeypatch, tmp_path):
    explicit = tmp_path / "custom.db"
    monkeypatch.setenv("TRILOBITE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("TRILOBITE_DB", str(explicit))

    assert trilobite_paths.memory_db_path() == str(explicit)


def test_default_home_prefers_localappdata_on_windows(monkeypatch, tmp_path):
    monkeypatch.delenv("TRILOBITE_HOME", raising=False)
    monkeypatch.setattr(trilobite_paths.os, "name", "nt", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.delenv("APPDATA", raising=False)

    assert trilobite_paths.default_home() == tmp_path / "local" / "trilobite"
