import sonder_paths


def test_memory_db_uses_sonder_home(monkeypatch, tmp_path):
    home = tmp_path / "state"
    monkeypatch.setenv("SONDER_HOME", str(home))
    monkeypatch.delenv("SONDER_DB", raising=False)

    assert sonder_paths.memory_db_path() == str(home / "memory.db")
    assert home.exists()


def test_memory_db_env_override_wins(monkeypatch, tmp_path):
    explicit = tmp_path / "custom.db"
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SONDER_DB", str(explicit))

    assert sonder_paths.memory_db_path() == str(explicit)


def test_default_home_prefers_xdg_data_home(monkeypatch, tmp_path):
    monkeypatch.setattr(sonder_paths.platform, "system", lambda: "Linux")
    monkeypatch.delenv("SONDER_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    assert sonder_paths.default_home() == tmp_path / "xdg" / "sonder"


def test_default_home_uses_macos_application_support(monkeypatch, tmp_path):
    monkeypatch.delenv("SONDER_HOME", raising=False)
    monkeypatch.setattr(sonder_paths.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sonder_paths.Path, "home", lambda: tmp_path / "user")

    assert sonder_paths.default_home() == (
        tmp_path / "user" / "Library" / "Application Support" / "sonder"
    )


def test_default_home_uses_windows_profile_then_system_drive(monkeypatch, tmp_path):
    monkeypatch.delenv("SONDER_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(sonder_paths.platform, "system", lambda: "Windows")
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))

    assert sonder_paths.default_home() == (
        tmp_path / "profile" / "AppData" / "Local" / "sonder"
    )

    monkeypatch.delenv("USERPROFILE")
    monkeypatch.setenv("SystemDrive", str(tmp_path / "system-drive"))
    assert sonder_paths.default_home() == tmp_path / "system-drive" / "Sonder"
