from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import sonder_paths


def test_bash_executable_rejects_windows_wsl_shims(tmp_path):
    system_root = tmp_path / "Windows"
    shim = system_root / "System32" / "bash.exe"
    shim.parent.mkdir(parents=True)
    shim.write_bytes(b"")

    assert sonder_paths.bash_executable(
        env={
            "SystemRoot": str(system_root),
            "SystemDrive": "C:",
            "ProgramFiles": str(tmp_path / "ProgramFiles"),
        },
        platform_name="nt",
        which=lambda _name: str(shim),
    ) is None


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


def test_memory_db_legacy_migration_is_safe_for_simultaneous_startup(monkeypatch, tmp_path):
    home = tmp_path / "state"
    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    legacy_db = legacy_root / "memory.db"
    legacy_db.write_bytes(b"legacy database")
    (legacy_root / "memory.db-wal").write_bytes(b"legacy wal")
    (legacy_root / "memory.db-shm").write_bytes(b"legacy shm")
    # ``memory_db_path`` resolves the legacy store beside its module.
    module_path = legacy_root / "sonder_paths.py"
    module_path.write_text("# test legacy location\n", encoding="utf-8")
    monkeypatch.setattr(sonder_paths, "__file__", str(module_path))
    monkeypatch.setenv("SONDER_HOME", str(home))
    monkeypatch.delenv("SONDER_DB", raising=False)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _unused: sonder_paths.memory_db_path(), range(16)))

    assert results == [str(home / "memory.db")] * 16
    assert (home / "memory.db").read_bytes() == b"legacy database"
    assert (home / "memory.db-wal").read_bytes() == b"legacy wal"
    assert (home / "memory.db-shm").read_bytes() == b"legacy shm"
    assert not list(home.glob("*.legacy-migrate.lock"))
    assert not list(home.glob("*.tmp"))


def test_memory_db_migration_reclaims_a_dead_stale_lock(monkeypatch, tmp_path):
    home = tmp_path / "state"
    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    legacy_db = legacy_root / "memory.db"
    legacy_db.write_bytes(b"legacy database")
    module_path = legacy_root / "sonder_paths.py"
    module_path.write_text("# test legacy location\n", encoding="utf-8")
    lock = home / ".memory.db.legacy-migrate.lock"
    home.mkdir()
    lock.write_text("999999\n1.0\nstale\n", encoding="ascii")
    monkeypatch.setattr(sonder_paths, "__file__", str(module_path))
    monkeypatch.setenv("SONDER_HOME", str(home))
    monkeypatch.delenv("SONDER_DB", raising=False)
    monkeypatch.setattr(sonder_paths, "_migration_owner_alive", lambda _pid: False)
    monkeypatch.setattr(sonder_paths.time, "time", lambda: 100.0)

    assert sonder_paths.memory_db_path() == str(home / "memory.db")
    assert (home / "memory.db").read_bytes() == b"legacy database"
    assert not lock.exists()


def test_memory_db_migration_waits_for_a_live_owner_without_a_fixed_timeout(monkeypatch, tmp_path):
    lock = tmp_path / ".memory.db.legacy-migrate.lock"
    lock.write_text("123\n1.0\nlive\n", encoding="ascii")
    monkeypatch.setattr(sonder_paths, "_migration_owner_alive", lambda _pid: True)

    assert sonder_paths._reclaim_abandoned_migration_lock(lock) is False
    assert lock.exists()


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
