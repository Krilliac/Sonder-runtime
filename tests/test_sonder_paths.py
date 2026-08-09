from pathlib import Path

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


def test_default_home_preserves_existing_macos_legacy_store(monkeypatch, tmp_path):
    monkeypatch.delenv("SONDER_HOME", raising=False)
    monkeypatch.setattr(sonder_paths.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sonder_paths.Path, "home", lambda: tmp_path / "user")
    legacy = tmp_path / "user" / ".local" / "share" / "sonder"
    legacy.mkdir(parents=True)

    assert sonder_paths.default_home() == legacy

    native = tmp_path / "user" / "Library" / "Application Support" / "sonder"
    native.mkdir(parents=True)
    assert sonder_paths.default_home() == native


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


def test_windows_system_drive_uses_environment_then_system_root_then_c():
    assert sonder_paths.windows_system_drive({"SystemDrive": "e:"}) == "E:"
    assert sonder_paths.windows_system_drive({"SystemRoot": r"F:\Windows"}) == "F:"
    assert sonder_paths.windows_system_drive({}) == "C:"
    assert sonder_paths.windows_system_drive({"SystemDrive": "not-a-drive"}) == "C:"


def test_windows_program_files_fallback_tracks_current_system_drive():
    assert sonder_paths.windows_program_files(env={"SystemDrive": "E:"}) == (
        r"E:\Program Files"
    )
    assert sonder_paths.windows_program_files(
        x86=True, env={"SystemDrive": "E:"}
    ) == r"E:\Program Files (x86)"
    assert sonder_paths.windows_program_files(
        env={"ProgramFiles": r"Q:\Tools\Programs", "SystemDrive": "E:"}
    ) == r"Q:\Tools\Programs"


def test_windows_machine_home_uses_program_data_or_system_drive():
    configured = sonder_paths.default_machine_home(
        platform_name="nt", env={"PROGRAMDATA": r"Q:\SharedData"}
    )
    fallback = sonder_paths.default_machine_home(
        platform_name="nt", env={"SystemDrive": "E:"}
    )

    assert str(configured) == str(Path(r"Q:\SharedData") / "Sonder")
    assert str(fallback) == r"E:\ProgramData\Sonder"
