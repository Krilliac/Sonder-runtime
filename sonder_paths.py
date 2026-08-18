"""Shared filesystem locations for sonder runtime state.

Install folders can be replaced or duplicated, especially when the Flutter apps
bundle a copy of the system. Runtime state lives in one per-user home directory
unless explicitly overridden.
"""
from __future__ import annotations

import os
import ntpath
import platform
import re
import shutil
import time
import uuid
from pathlib import Path


_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:$")
_LEGACY_DB_MIGRATION_WAIT_SECONDS = 10.0
_LEGACY_DB_MIGRATION_POLL_SECONDS = 0.02
_LEGACY_DB_MIGRATION_STALE_LOCK_SECONDS = 30.0
# How long the migrator retries deleting its own lock. Waiters read the lock to
# identify its owner, and Windows refuses to unlink a file while any other
# handle is open on it, so a release that races a waiter's read fails with
# ERROR_SHARING_VIOLATION. Those reads are microseconds long; this only has to
# outlast one of them.
_LEGACY_DB_MIGRATION_LOCK_RELEASE_SECONDS = 5.0


def windows_system_drive(env=None) -> str:
    """Return the active Windows system drive, falling back only to ``C:``.

    ``SystemDrive`` is authoritative.  Some stripped-down process environments
    omit it but retain ``SystemRoot``; derive the drive from that before using
    the Windows platform default.  Never guess a data drive such as ``D:``.
    """
    values = os.environ if env is None else env
    drive = str(values.get("SystemDrive", "")).strip()
    if _WINDOWS_DRIVE_RE.fullmatch(drive):
        return drive.upper()
    root_drive = ntpath.splitdrive(str(values.get("SystemRoot", "")))[0]
    if _WINDOWS_DRIVE_RE.fullmatch(root_drive):
        return root_drive.upper()
    return "C:"


def windows_program_files(*, x86=False, env=None) -> str:
    """Resolve Program Files without embedding a particular machine's drive."""
    values = os.environ if env is None else env
    key = "ProgramFiles(x86)" if x86 else "ProgramFiles"
    configured = str(values.get(key, "")).strip()
    if configured:
        return configured
    leaf = "Program Files (x86)" if x86 else "Program Files"
    return ntpath.join(windows_system_drive(values), "\\", leaf)


def bash_executable(*, env=None, platform_name=None, which=None) -> str | None:
    """Return a Bash that accepts native paths, excluding Windows' WSL shim."""
    values = os.environ if env is None else env
    system = os.name if platform_name is None else platform_name
    finder = shutil.which if which is None else which
    if system == "nt":
        candidates = [
            Path(windows_program_files(env=values)) / "Git" / "bin" / "bash.exe",
            Path(windows_program_files(env=values)) / "Git" / "usr" / "bin" / "bash.exe",
        ]
        local = str(values.get("LOCALAPPDATA", "")).strip()
        if local:
            candidates.extend([
                Path(local) / "Programs" / "Git" / "bin" / "bash.exe",
                Path(local) / "Programs" / "Git" / "usr" / "bin" / "bash.exe",
            ])
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        resolved = finder("bash") or finder("sh")
        if not resolved:
            return None
        normalized = os.path.normcase(os.path.abspath(resolved))
        system_root = str(values.get("SystemRoot", "")).strip()
        blocked_roots = [
            os.path.normcase(os.path.abspath(os.path.join(system_root, "System32")))
            if system_root else "",
            os.path.normcase(os.path.abspath(os.path.join(local, "Microsoft", "WindowsApps")))
            if local else "",
        ]
        if any(root and os.path.commonpath([normalized, root]) == root for root in blocked_roots):
            return None
        return resolved
    return finder("bash") or finder("sh")


def default_machine_home(*, env=None, platform_name=None) -> Path:
    """Return the machine-wide Sonder root for the current operating system."""
    values = os.environ if env is None else env
    override = str(values.get("SONDER_MACHINE_HOME", "")).strip()
    if override:
        return Path(override).expanduser()
    if (os.name if platform_name is None else platform_name) == "nt":
        common = (
            str(values.get("PROGRAMDATA", "")).strip()
            or str(values.get("ALLUSERSPROFILE", "")).strip()
        )
        if common:
            return Path(common) / "Sonder"
        return Path(
            ntpath.join(windows_system_drive(values), "\\", "ProgramData", "Sonder")
        )
    return Path("/opt/sonder")


def macos_default_home(user_home: Path | None = None) -> Path:
    """Use the native macOS location without hiding an existing legacy store."""
    root = Path.home() if user_home is None else Path(user_home)
    native = root / "Library" / "Application Support" / "sonder"
    legacy = root / ".local" / "share" / "sonder"
    if legacy.exists() and not native.exists():
        return legacy
    return native


def default_home() -> Path:
    override = os.environ.get("SONDER_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    system = platform.system()
    if system == "Windows" or (not system and os.name == "nt"):
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if root:
            return Path(root) / "sonder"
        profile = os.environ.get("USERPROFILE", "").strip()
        if profile:
            return Path(profile) / "AppData" / "Local" / "sonder"
        # Service/minimal Windows environments may have no user-profile
        # variables. Honor SystemDrive and use C: only as the final fallback.
        drive = os.environ.get("SystemDrive", "").strip() or "C:"
        suffix = "" if drive.endswith(("\\", "/")) else os.sep
        return Path(drive + suffix) / "Sonder"
    if system == "Darwin":
        return macos_default_home()
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg:
        return Path(xdg).expanduser() / "sonder"
    return Path.home() / ".local" / "share" / "sonder"


def ensure_home() -> Path:
    home = default_home()
    home.mkdir(parents=True, exist_ok=True)
    return home


def state_path(name: str, env_var: str = "") -> str:
    if env_var:
        override = os.environ.get(env_var, "").strip()
        if override:
            return str(Path(override).expanduser())
    return str(ensure_home() / name)


def memory_db_path() -> str:
    override = os.environ.get("SONDER_DB", "").strip()
    if override:
        return str(Path(override).expanduser())

    target = ensure_home() / "memory.db"
    legacy = Path(__file__).resolve().with_name("memory.db")
    if not target.exists() and legacy.exists() and legacy.resolve() != target.resolve():
        _migrate_legacy_memory_db(legacy, target)
    return str(target)


def _migration_lock_owner(lock: Path) -> tuple[int | None, float | None]:
    """Read the advisory owner record without trusting a partial crash write."""
    try:
        lines = lock.read_text(encoding="ascii").splitlines()
        return int(lines[0]), float(lines[1])
    except (OSError, ValueError, IndexError):
        return None, None


def _migration_owner_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        # A process we cannot inspect is still a live owner; never steal it.
        return True
    except OSError:
        return False


def _release_migration_lock(lock: Path) -> bool:
    """Delete a migration lock this process owns. True once it is gone.

    A single ``unlink`` is not enough on Windows. Every waiter reads the lock to
    decide whether its owner is still alive, and Windows refuses to delete a
    file that any other handle has open, so the owner's release loses a race it
    is guaranteed to keep re-entering while waiters exist. Swallowing that error
    orphans the lock: the migration itself still succeeds, but the leftover file
    then costs the next migration a full ``_LEGACY_DB_MIGRATION_STALE_LOCK_SECONDS``
    wait before it can be reclaimed. Retrying for a moment costs nothing and
    keeps the lock's lifetime tied to the work it guards.
    """
    deadline = time.time() + _LEGACY_DB_MIGRATION_LOCK_RELEASE_SECONDS
    while True:
        try:
            lock.unlink()
            return True
        except FileNotFoundError:
            # Already gone -- a reclaim or an earlier attempt won. Still success.
            return True
        except OSError:
            if time.time() >= deadline:
                # Give up rather than block startup: a stale lock is recoverable
                # through the reclaim path, a hung process is not.
                return False
            time.sleep(_LEGACY_DB_MIGRATION_POLL_SECONDS)


def _reclaim_abandoned_migration_lock(lock: Path) -> bool:
    """Remove a dead or long-abandoned migration lock, never a live owner."""
    pid, started = _migration_lock_owner(lock)
    if _migration_owner_alive(pid):
        return False
    try:
        age = max(0.0, time.time() - (started or lock.stat().st_mtime))
    except OSError:
        return False
    if age < _LEGACY_DB_MIGRATION_STALE_LOCK_SECONDS:
        return False
    try:
        # Verify the record once more before unlinking so a writer that won the
        # race and published a live PID is never reclaimed as the old owner.
        verify_pid, _ = _migration_lock_owner(lock)
        if _migration_owner_alive(verify_pid):
            return False
        lock.unlink()
        return True
    except OSError:
        return False


def _migrate_legacy_memory_db(legacy: Path, target: Path) -> None:
    """Copy one legacy SQLite store without racing another startup process.

    The REPL and loopback server can start at the same time.  Publishing a
    direct ``copy2`` to the final destination lets both writers collide on
    Windows and can expose a database before its WAL sidecars.  An exclusive
    lock elects one migrator; it stages all files in the destination directory,
    publishes sidecars first, then atomically publishes the database last.
    """
    lock = target.with_name(".%s.legacy-migrate.lock" % target.name)
    stage_paths: list[Path] = []
    while not target.exists():
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            # A live owner is allowed to complete at its own pace: a large WAL
            # on a slow disk is still a healthy migration. A crash/power loss
            # leaves a dead owner record, which this path reclaims safely.
            if _reclaim_abandoned_migration_lock(lock):
                continue
            time.sleep(_LEGACY_DB_MIGRATION_POLL_SECONDS)
            continue
        try:
            record = "%d\n%.6f\n%s\n" % (os.getpid(), time.time(), uuid.uuid4().hex)
            os.write(fd, record.encode("ascii"))
            os.fsync(fd)
            if target.exists():
                return
            nonce = uuid.uuid4().hex
            staged_db = target.with_name(".%s.%s.tmp" % (target.name, nonce))
            shutil.copy2(legacy, staged_db)
            stage_paths.append(staged_db)
            staged_sidecars: list[tuple[Path, Path]] = []
            for suffix in ("-shm", "-wal"):
                source = Path(str(legacy) + suffix)
                if source.exists():
                    staged = target.with_name(".%s%s.%s.tmp" % (target.name, suffix, nonce))
                    shutil.copy2(source, staged)
                    stage_paths.append(staged)
                    staged_sidecars.append((staged, Path(str(target) + suffix)))
            # A consumer only sees the database after all available SQLite
            # sidecars have reached their final paths.
            for staged, destination in staged_sidecars:
                os.replace(staged, destination)
                stage_paths.remove(staged)
            os.replace(staged_db, target)
            stage_paths.remove(staged_db)
            return
        finally:
            for staged in stage_paths:
                try:
                    staged.unlink()
                except OSError:
                    pass
            os.close(fd)
            _release_migration_lock(lock)
