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

Windows: POSIX modes do not exist, and the old assumption that the profile's
inherited ACL grants only the user, SYSTEM and Administrators does not hold
(sandbox groups and other tools add inherited read grants to
``%LOCALAPPDATA%``). ``tighten_existing_stores`` -- called for the state home
only -- therefore gives the state home directory and its secret stores (see
``state_secret_files``) an explicit *protected* DACL granting only the
current user, SYSTEM and Administrators, and labels those stores medium
integrity with no-read-up, so a low-integrity process running under the same
user (the Windows selfmod candidate) cannot read them either. The directory
ACL is set without propagation: files created later inherit it, while
existing subtrees such as selfmod candidate workspaces keep theirs. The same
ownership rule applies (only paths the current user owns), reparse points
are skipped, and failures are logged at debug level and reported by
``low_integrity_readable_state_files`` rather than raised.
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


# --- Windows ------------------------------------------------------------------

# Files directly in the state home that hold credentials, sessions or
# conversations, and subdirectories whose every file is secret.
_SECRET_NAMES = frozenset({"fleet-principal.json"})
_SECRET_SUFFIXES = (".env", ".key", ".pem", ".pfx", ".p12")
_SECRET_DIRECTORIES = ("secrets", "certs", "audit")
_MAX_SECRET_FILES = 4096
# Mandatory-label SIDs at or above medium integrity (SDDL aliases).
_MEDIUM_OR_HIGHER_LABELS = frozenset({"ME", "MP", "HI", "SI"})
_OWNER_SECURITY_INFORMATION = 0x1
_DACL_SECURITY_INFORMATION = 0x4
_LABEL_SECURITY_INFORMATION = 0x10
_SDDL_REVISION_1 = 1
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1


def _windows_api():
    """ctypes bindings for the few advapi32 calls used here, or None.

    Plain ctypes (no pywin32) keeps the platform layer free of third-party
    imports; SDDL strings keep the descriptors short and reviewable.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return None
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.ULONG),
    ]
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.ULONG),
    ]
    advapi32.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    advapi32.GetFileSecurityW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return ctypes, wintypes, advapi32, kernel32


def _windows_sddl(path: str, information: int) -> str:
    """The SDDL form of *path*'s security information; raises OSError."""
    ctypes, wintypes, advapi32, kernel32 = _windows_api()
    needed = wintypes.DWORD(0)
    advapi32.GetFileSecurityW(path, information, None, 0, ctypes.byref(needed))
    if not needed.value:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_string_buffer(needed.value)
    if not advapi32.GetFileSecurityW(path, information, buffer, needed, ctypes.byref(needed)):
        raise ctypes.WinError(ctypes.get_last_error())
    text = wintypes.LPWSTR()
    if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
        buffer, _SDDL_REVISION_1, information, ctypes.byref(text), None,
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return text.value or ""
    finally:
        kernel32.LocalFree(text)


def _windows_set_sddl(path: str, information: int, sddl: str) -> None:
    ctypes, _wintypes, advapi32, kernel32 = _windows_api()
    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), None,
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        # SetFileSecurity does not propagate to existing children; the "P"
        # flag in the SDDL sets SE_DACL_PROTECTED, which blocks inheritance.
        if not advapi32.SetFileSecurityW(path, information, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.LocalFree(descriptor)


def _windows_user_sid() -> str:
    ctypes, wintypes, advapi32, kernel32 = _windows_api()
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(
            token, _TOKEN_USER, buffer, needed, ctypes.byref(needed),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return text.value
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.CloseHandle(token)


def _windows_owned_by_me(path: str) -> bool:
    try:
        owner = _windows_sddl(path, _OWNER_SECURITY_INFORMATION)
        return owner.startswith("O:") and owner[2:] == _windows_user_sid()
    except OSError:
        return False


def _is_reparse_point(path: str) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _windows_restrict(path: str, *, directory: bool, label: bool) -> bool:
    """Protected owner/SYSTEM/Administrators DACL, then a no-read-up label."""
    if _windows_api() is None or _is_reparse_point(path) or not _windows_owned_by_me(path):
        return False
    try:
        inherit = "OICI" if directory else ""
        user = _windows_user_sid()
        _windows_set_sddl(
            path, _DACL_SECURITY_INFORMATION,
            "D:P(A;%s;FA;;;%s)(A;%s;FA;;;SY)(A;%s;FA;;;BA)" % (inherit, user, inherit, inherit),
        )
        if label:
            # Medium integrity, no-read-up and no-write-up. Setting a label
            # needs WRITE_OWNER, which the DACL just written grants this user.
            _windows_set_sddl(path, _LABEL_SECURITY_INFORMATION, "S:(ML;;NRNW;;;ME)")
    except OSError as error:
        _logger.debug("could not restrict private path ACL: %s", type(error).__name__)
        return False
    return True


def low_integrity_readable(path: str | os.PathLike[str]) -> bool:
    """Whether a low-integrity process of this user could read *path*.

    True unless the object carries a mandatory label of at least medium
    integrity with no-read-up. Unreadable security information counts as
    readable, so callers fail closed.
    """
    if _windows_api() is None:
        return True
    try:
        sddl = _windows_sddl(os.fspath(path), _LABEL_SECURITY_INFORMATION)
    except OSError:
        return True
    for ace in sddl.split("(")[1:]:
        fields = ace.rstrip(")").split(";")
        if len(fields) < 6 or fields[0] != "ML":
            continue
        return not ("NR" in fields[2] and fields[5] in _MEDIUM_OR_HIGHER_LABELS)
    return True


def _is_regular(path: str) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def state_secret_files(home: str | os.PathLike[str]) -> list[str]:
    """Secret-bearing files of a state home (bounded; never follows links).

    Store files (``_STORE_SUFFIXES``) and credential files directly in the
    home, plus every regular file below its ``secrets``/``certs``/``audit``
    directories. Candidate workspaces, logs and other state are not listed.
    """
    directory = os.fspath(home)
    found: list[str] = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return found
    for name in names:
        lowered = name.lower()
        path = os.path.join(directory, name)
        if (
            lowered.endswith(_STORE_SUFFIXES)
            or lowered.endswith(_SECRET_SUFFIXES)
            or lowered in _SECRET_NAMES
            or lowered == ".env"
        ) and _is_regular(path):
            found.append(path)
    for child in _SECRET_DIRECTORIES:
        root = os.path.join(directory, child)
        if _is_reparse_point(root) or not os.path.isdir(root):
            continue
        for current, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if not _is_reparse_point(os.path.join(current, d))]
            for name in sorted(files):
                path = os.path.join(current, name)
                if _is_regular(path):
                    found.append(path)
                if len(found) >= _MAX_SECRET_FILES:
                    return found
    return found


def protect_state_from_low_integrity(home: str | os.PathLike[str]) -> list[str]:
    """Restrict and label every state secret; return those still readable.

    Called before a low-integrity candidate runs. An empty result is the only
    passing outcome: anything returned is still readable at low integrity
    (another account's file, a failed ACL write) and the caller must refuse.
    """
    remaining: list[str] = []
    for path in state_secret_files(home):
        if low_integrity_readable(path):
            _windows_restrict(path, directory=False, label=True)
        if low_integrity_readable(path):
            remaining.append(path)
    return remaining


def low_integrity_readable_state_files(home: str | os.PathLike[str]) -> list[str]:
    """State secrets a low-integrity process of this user could read."""
    return [path for path in state_secret_files(home) if low_integrity_readable(path)]


def _windows_tighten_state_home(directory: str) -> int:
    changed = 0
    if os.path.isdir(directory) and not _never_tighten_windows(directory):
        changed += int(_windows_restrict(directory, directory=True, label=False))
    for root in _SECRET_DIRECTORIES:
        path = os.path.join(directory, root)
        if os.path.isdir(path) and not _is_reparse_point(path):
            changed += int(_windows_restrict(path, directory=True, label=False))
    for path in state_secret_files(directory):
        changed += int(_windows_restrict(path, directory=False, label=True))
    return changed


def _never_tighten_windows(directory: str) -> bool:
    real = os.path.normcase(os.path.realpath(directory))
    anchor = os.path.normcase(os.path.splitdrive(real)[0] + os.sep)
    if real == anchor:
        return True
    try:
        home = os.path.normcase(os.path.realpath(os.path.expanduser("~")))
    except (OSError, RuntimeError):
        home = ""
    return bool(home) and (real == home or home.startswith(real + os.sep))


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

    On Windows the state home and its secret stores get the protected ACL and
    no-read-up label described in the module docstring instead.
    """
    windows = _windows_api() is not None
    if not supported() and not windows:
        return 0
    directory = os.fspath(home)
    with _SECURED_DIRS_LOCK:
        if directory in _SWEPT_HOMES:
            return 0
        _SWEPT_HOMES.add(directory)
    if windows:
        return _windows_tighten_state_home(directory)
    changed = 0
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    for name in names:
        if not name.lower().endswith(_STORE_SUFFIXES):
            continue
        path = os.path.join(directory, name)
        try:
            if not stat.S_ISREG(os.lstat(path).st_mode):
                continue
        except OSError:
            continue
        if restrict_to_owner(path):
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
    "low_integrity_readable",
    "low_integrity_readable_state_files",
    "prepare_private_file",
    "protect_state_from_low_integrity",
    "prepare_private_sqlite",
    "restrict_to_owner",
    "state_secret_files",
    "supported",
]
