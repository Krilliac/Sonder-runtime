"""Owner-only permissions for Sonder's private state on POSIX hosts.

``memory.db`` holds password hashes and salts, account sessions, every stored
conversation and fact; ``sessions.db`` and the audit JSONL files hold prompts
and tool traffic. Created under the default ``umask 022`` they were ``0644``
inside a ``0755`` state home, readable by every local account on a shared host.

The rules, applied at the state-home and store-open choke points:

* the state home directory is created ``0700``; an existing one owned by the
  current user has its group/other bits removed. One narrow exception: on a
  host that runs uid-separated self-modification candidates
  (``SONDER_SELFMOD_CANDIDATE_UID`` set), the candidate uid must traverse the
  home to reach its workspace under ``selfmod/workspaces``, so the home is
  ``0711`` there -- traverse only, no listing, no read -- and every store
  opened through the SQLite factory is still ``0600``. An existing home whose
  group/other bits are already traverse-only is left as it is by every
  process, so the operator's ``chmod 0711`` is not undone by a runtime started
  without that variable;
* SQLite databases (and their ``-wal``/``-shm``/``-journal`` sidecars) and the
  JSONL audit stores are created ``0600``; existing ones owned by the current
  user are tightened to owner-only when they are opened. SQLite creates new
  sidecars with the database file's own mode, so a ``0600`` database keeps its
  WAL and SHM private too.

Every operation only ever *removes* group/other permission bits. It never
widens a mode, never follows a symlink, never touches a path owned by another
account, and never tightens a sticky shared directory (``/tmp``), the
filesystem root, or the user's own home directory -- a mis-set ``SONDER_HOME``
must not lock other users out of a shared location.

Hardening is best-effort by design: a store that cannot be tightened (for
example on a filesystem without POSIX modes) still opens, and the failure is
logged at debug level. The confidentiality floor is the ``0700`` directory.

Windows: these functions are no-ops. The default state home lives under the
per-user profile (``%LOCALAPPDATA%``) whose inherited ACL already grants only
the user, SYSTEM and Administrators; a custom ``SONDER_HOME`` on Windows must
be given an equivalent ACL by the operator.
"""
from __future__ import annotations

import logging
import os
import stat
import threading
from pathlib import Path

PRIVATE_DIR_MODE = 0o700
TRAVERSE_DIR_MODE = 0o711
PRIVATE_FILE_MODE = 0o600
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")

_GROUP_OTHER_BITS = 0o077
_GROUP_OTHER_READ_WRITE_BITS = 0o066
_logger = logging.getLogger(__name__)
_SECURED_DIRS: set[str] = set()
_SECURED_DIRS_LOCK = threading.Lock()


def supported() -> bool:
    """True where POSIX permission bits carry the confidentiality rule."""
    return os.name == "posix" and hasattr(os, "geteuid")


def _never_tighten(path: str, info: os.stat_result) -> bool:
    if info.st_mode & stat.S_ISVTX:
        return True
    real = os.path.realpath(path)
    if real == os.path.realpath(os.sep):
        return True
    try:
        home = os.path.realpath(os.path.expanduser("~"))
    except (OSError, RuntimeError):
        home = ""
    return bool(home) and real == home


def restrict_to_owner(path: str | os.PathLike[str], *, keep_traverse: bool = False) -> bool:
    """Remove group/other permission bits from an existing path we own.

    ``keep_traverse`` leaves the group/other execute (traverse) bits of a
    directory in place and removes only read/write. Returns True when the
    mode was changed. Symlinks, paths owned by another account,
    already-private paths, and shared directories are left alone.
    """
    if not supported():
        return False
    text = os.fspath(path)
    try:
        info = os.lstat(text)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid():
        return False
    current = stat.S_IMODE(info.st_mode)
    bits = (
        _GROUP_OTHER_READ_WRITE_BITS
        if keep_traverse and stat.S_ISDIR(info.st_mode) else _GROUP_OTHER_BITS
    )
    if not current & bits:
        return False
    if stat.S_ISDIR(info.st_mode) and _never_tighten(text, info):
        return False
    try:
        os.chmod(text, current & ~bits)
    except OSError as error:
        _logger.debug("could not restrict private path mode: %s", type(error).__name__)
        return False
    return True


def _is_traverse_only(path: Path) -> bool:
    """True for an existing directory whose group/other bits are execute only.

    That mode (``0711``/``0701``/``0710``) is never a build default; it is the
    operator's deliberate choice for uid-separated selfmod candidates. A
    process started without ``SONDER_SELFMOD_CANDIDATE_UID`` (the served
    runtime, the REPL) must not undo it, or the next nightly candidate could
    no longer reach its workspace. It lists and reads nothing, so leaving it
    is not a widening.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode):
        return False
    group_other = stat.S_IMODE(info.st_mode) & _GROUP_OTHER_BITS
    return bool(group_other) and not group_other & _GROUP_OTHER_READ_WRITE_BITS


def ensure_private_dir(path: str | os.PathLike[str], *, traverse: bool = False) -> Path:
    """Create *path* (and parents) and make the leaf directory owner-only.

    ``traverse=True`` makes it ``0711`` instead of ``0700`` (see the module
    docstring for the one caller that needs it). Intermediate directories keep
    the process default: only the state directory itself is Sonder's to
    restrict. The result is cached per path so the per-lookup cost after the
    first call is one set membership test.
    """
    directory = Path(path)
    key = "%s\0%d" % (os.fspath(directory), int(traverse))
    with _SECURED_DIRS_LOCK:
        if key in _SECURED_DIRS and directory.is_dir():
            return directory
    directory.mkdir(
        parents=True, exist_ok=True,
        mode=TRAVERSE_DIR_MODE if traverse else PRIVATE_DIR_MODE,
    )
    restrict_to_owner(
        directory, keep_traverse=traverse or _is_traverse_only(directory),
    )
    with _SECURED_DIRS_LOCK:
        _SECURED_DIRS.add(key)
    return directory


_STORE_SUFFIXES = (".db", ".jsonl", ".sqlite", ".sqlite3") + tuple(
    ".db" + suffix for suffix in SQLITE_SIDECAR_SUFFIXES
)
_SWEPT_HOMES: set[str] = set()


def tighten_existing_stores(home: str | os.PathLike[str]) -> int:
    """Make the store files directly inside *home* owner-only, once per process.

    Stores opened through ``prepare_private_sqlite``/``prepare_private_file``
    are tightened at open, but several stores are created by code that does
    not use those choke points, and homes created before this hardening keep
    their old ``0644`` files. The ``0700`` home already blocks other accounts;
    this removes the remaining group/other bits so the files are private on
    their own too. Only regular files we own whose name ends in a store suffix
    are touched, with the same rules as ``restrict_to_owner`` (never a
    symlink, never another account's file, only ever removing bits). Returns
    the number of files changed.
    """
    if not supported():
        return 0
    directory = os.fspath(home)
    with _SECURED_DIRS_LOCK:
        if directory in _SWEPT_HOMES:
            return 0
        _SWEPT_HOMES.add(directory)
    changed = 0
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return 0
    for entry in entries:
        name = entry.name.lower()
        if not name.endswith(_STORE_SUFFIXES):
            continue
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
        except OSError:
            continue
        if restrict_to_owner(entry.path):
            changed += 1
    return changed


def prepare_private_file(path: str | os.PathLike[str], *, create: bool = True) -> None:
    """Create *path* ``0600`` if missing, or tighten it if it already exists.

    ``create=False`` only tightens (for read-only opens that must not conjure
    a file). A missing parent directory is not created here; the caller's own
    open reports that as it always has.
    """
    if not supported():
        return
    text = os.fspath(path)
    if create:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            os.close(os.open(text, flags, PRIVATE_FILE_MODE))
            return
        except FileExistsError:
            pass
        except OSError as error:
            _logger.debug("could not pre-create private file: %s", type(error).__name__)
            return
    restrict_to_owner(text)


def prepare_private_sqlite(path: str | os.PathLike[str], *, create: bool = True) -> None:
    """Private modes for a SQLite database file and any existing sidecars."""
    if not supported():
        return
    text = os.fspath(path)
    if not text or text == ":memory:" or text.startswith("file:"):
        # In-memory databases have no file; a URI is not a filesystem path
        # (``owned_sqlite`` decodes URIs before calling here).
        return
    prepare_private_file(text, create=create)
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        restrict_to_owner(text + suffix)


__all__ = [
    "PRIVATE_DIR_MODE",
    "PRIVATE_FILE_MODE",
    "TRAVERSE_DIR_MODE",
    "SQLITE_SIDECAR_SUFFIXES",
    "ensure_private_dir",
    "prepare_private_file",
    "prepare_private_sqlite",
    "restrict_to_owner",
    "supported",
]
