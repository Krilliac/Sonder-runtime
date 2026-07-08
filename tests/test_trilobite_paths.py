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


def test_default_home_prefers_xdg_data_home(monkeypatch, tmp_path):
    monkeypatch.delenv("TRILOBITE_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    assert trilobite_paths.default_home() == tmp_path / "xdg" / "trilobite"
