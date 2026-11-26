"""Handle-validated reads of one file below a root, for the developer-workflow tools.

``diff_files``, ``rename_symbol`` and ``extract_references`` used to check a path
(reparse points, ``resolve()``, secret names) and then reopen the same spelling,
so a Windows 8.3 short name (``CREDEN~1.JSO`` for ``.credentials.json``) or a
parent directory swapped for a junction between the check and the open slipped
through. This module reuses the no-follow open and the opened-handle path
lookup of ``security_eval.repo_reader`` and applies the caller's protection
rules to the path of the handle that was actually opened:

- a component shaped like a short name (``NAME~N.EXT``) is refused outright;
- every component below the root is lstat-ed and a symlink or junction is refused;
- the file is opened without following a link in its last component;
- the handle's final path must lie inside the root (fail closed on Windows when it
  cannot be obtained) and must pass the caller's ``is_protected`` predicate;
- the bytes are read from that same descriptor, and callers build previews and
  diffs from those bytes, never from a second open of the name.

Thread affinity: stateless; safe from any thread. The descriptor is closed before
return on every path.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Callable

from ..domain.security_eval.errors import PathEscapeRefused, SourceRefused
from .security_eval import repo_reader as _reader


class ConfinedReadRefused(PermissionError):
    """The file may not be read: a short name, a link, an escape or a protected path."""


class ConfinedReadTooLarge(OSError):
    """The file is larger than the caller's byte cap."""


_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def short_name_component(root: Path | str, path: Path | str) -> str | None:
    """The first component of ``path`` below ``root`` shaped like an 8.3 short name, else None."""
    try:
        relative = os.path.relpath(str(path), str(root))
    except ValueError:
        return None
    for part in relative.split(os.sep):
        if part not in ("", ".", "..") and _reader._looks_like_short_name(part):
            return part
    return None


def read_confined_bytes(
    root: Path | str,
    path: Path | str,
    *,
    max_bytes: int,
    is_protected: Callable[[Path], bool],
) -> bytes:
    """Read the regular file ``path`` (below ``root``) through a validated, already-open handle.

    Raises ``FileNotFoundError`` when it is missing or not a regular file,
    ``ConfinedReadTooLarge`` over ``max_bytes`` and ``ConfinedReadRefused`` for
    every confinement failure.
    """
    root_text = os.path.abspath(str(root))
    current = os.path.abspath(str(path))
    if not _reader._inside(current, root_text) or _reader._norm(current) == _reader._norm(root_text):
        raise ConfinedReadRefused("the path resolves outside the root")
    short = short_name_component(root_text, current)
    if short is not None:
        raise ConfinedReadRefused(
            "component %r is shaped like a Windows 8.3 short name (NAME~N.EXT) and is never "
            "read; give the long name" % short
        )
    relative_parts = os.path.relpath(current, root_text).split(os.sep)
    walk = root_text
    for position, part in enumerate(relative_parts):
        walk = os.path.join(walk, part)
        try:
            info = os.lstat(walk)
        except OSError:
            raise FileNotFoundError("file does not exist") from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE:
            raise ConfinedReadRefused("the path passes through a symlink or junction")
        if position < len(relative_parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise FileNotFoundError("file does not exist")
    try:
        descriptor = _reader._open_no_follow(current)
    except (PathEscapeRefused, SourceRefused) as exc:
        raise ConfinedReadRefused(str(exc)) from None
    except OSError:
        raise FileNotFoundError("file could not be opened") from None
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise FileNotFoundError("not a regular file")
        _validate_handle(descriptor, opened, root_text, current, is_protected)
        if opened.st_size > max_bytes:
            raise ConfinedReadTooLarge("file is over the %d byte cap" % max_bytes)
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ConfinedReadTooLarge("file is over the %d byte cap" % max_bytes)
        return data
    finally:
        os.close(descriptor)


def _validate_handle(
    descriptor: int,
    opened: os.stat_result,
    root: str,
    current: str,
    is_protected: Callable[[Path], bool],
) -> None:
    try:
        actual = _reader._opened_handle_path(descriptor)
    except PathEscapeRefused as exc:
        raise ConfinedReadRefused(str(exc)) from None
    except OSError:
        raise ConfinedReadRefused("the opened path could not be confirmed, so it is not read") from None
    if actual is None:
        if os.name == "nt":
            raise ConfinedReadRefused("the opened path could not be confirmed, so it is not read")
        if not os.path.samestat(opened, os.stat(current, follow_symlinks=False)):
            raise ConfinedReadRefused("the file changed while it was validated")
        actual = current
    if not _reader._inside(actual, root):
        raise ConfinedReadRefused("the opened file resolves outside the root")
    if _reader._handle_relative(actual, root) is None:
        raise ConfinedReadRefused("the opened path could not be placed below the root, so it is not read")
    if is_protected(Path(actual)):
        raise ConfinedReadRefused("the opened file is a secret or control-state path and is never read")
    if os.name != "nt" and not os.path.samestat(opened, os.stat(current, follow_symlinks=False)):
        raise ConfinedReadRefused("the file changed while it was validated")


class PinnedDirectory:
    """A directory below a root held open while files in it are replaced.

    ``rename_symbol`` used to validate a path and then ``mkstemp``/``os.replace`` through
    the same spelling, so a parent folder swapped for a junction after validation sent the
    write outside the root. Here the directory is opened first (no link followed) and its
    final handle path must lie inside the root:

    - Windows: the handle is opened without ``FILE_SHARE_DELETE``, so the folder (and every
      ancestor) cannot be renamed, deleted or replaced by a junction while it is held;
      temp files and the replace use the handle's final path, never the caller's spelling.
    - POSIX: temp creation, the target check and ``os.replace`` go through the directory
      descriptor (``dir_fd``), so a swapped parent path is never consulted.

    Every temp file's own handle path and the target's must also sit directly in that
    directory. Thread affinity: one instance per thread; ``close`` releases the handle.
    """

    def __init__(self, root: Path | str, directory: Path | str) -> None:
        self._root = os.path.abspath(str(root))
        self._requested = os.path.abspath(str(directory))
        self._fd = -1
        self._path = ""

    def __enter__(self) -> "PinnedDirectory":
        self._open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._fd >= 0:
            try:
                os.close(self._fd)
            finally:
                self._fd = -1

    def _open(self) -> None:
        if not _reader._inside(self._requested, self._root):
            raise ConfinedReadRefused("the directory resolves outside the root")
        if os.name == "nt":
            self._fd = _open_directory_windows(self._requested)
        else:
            flags = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_CLOEXEC", 0))
            try:
                self._fd = os.open(self._requested, flags)
            except OSError as exc:
                raise ConfinedReadRefused(
                    "the directory could not be opened without following a link: %s" % exc) from None
        try:
            try:
                actual = _reader._opened_handle_path(self._fd)
            except (PathEscapeRefused, OSError) as exc:
                raise ConfinedReadRefused("the opened directory path could not be confirmed: %s" % exc) from None
            if actual is None:
                if os.name == "nt" or not os.path.samestat(
                        os.fstat(self._fd), os.stat(self._requested, follow_symlinks=False)):
                    raise ConfinedReadRefused("the opened directory path could not be confirmed")
                actual = self._requested
            if not _reader._inside(actual, self._root) or _reader._handle_relative(actual, self._root) is None:
                raise ConfinedReadRefused("the opened directory resolves outside the root")
            self._path = actual
        except BaseException:
            self.close()
            raise

    def _same_directory(self, actual: str, name: str) -> bool:
        return _reader._norm(actual) == _reader._norm(os.path.join(self._path, name))

    def stage(self, name: str, text: str) -> str:
        """Write ``text`` to a new temp file in the directory; returns the temp name.

        The mode of the existing ``name`` (if any) is copied. Raises ``ConfinedReadRefused``
        when the temp file's own handle path is not directly in the pinned directory.
        """
        import secrets

        if os.sep in name or (os.altsep and os.altsep in name) or name in ("", ".", ".."):
            raise ConfinedReadRefused("not a plain file name")
        temp = "." + name + "." + secrets.token_hex(6) + ".rename"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        if os.name == "nt":
            descriptor = os.open(os.path.join(self._path, temp), flags, 0o600)
        else:
            descriptor = os.open(temp, flags | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=self._fd)
        try:
            try:
                actual = _reader._opened_handle_path(descriptor)
            except (PathEscapeRefused, OSError):
                actual = None
            if actual is not None and not self._same_directory(actual, temp):
                raise ConfinedReadRefused("the temporary file is not in the validated directory")
            if actual is None and os.name == "nt":
                raise ConfinedReadRefused("the temporary file path could not be confirmed")
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(text.encode("utf-8"))
            try:
                if sys.platform == "win32":  # not os.name: the type checker narrows on sys.platform only
                    mode = os.stat(os.path.join(self._path, name)).st_mode
                    os.chmod(os.path.join(self._path, temp), stat.S_IMODE(mode))
                else:
                    os.fchmod(descriptor, stat.S_IMODE(os.stat(name, dir_fd=self._fd, follow_symlinks=False).st_mode))
            except OSError:
                pass
        except BaseException:
            os.close(descriptor)
            self.discard(temp)
            raise
        os.close(descriptor)
        return temp

    def commit(self, temp: str, name: str) -> None:
        """Atomically replace ``name`` with the staged ``temp`` after confirming the target."""
        target = os.path.join(self._path, name)
        if os.name == "nt":
            try:
                descriptor = _reader._open_no_follow(target)
            except (PathEscapeRefused, SourceRefused) as exc:
                raise ConfinedReadRefused(str(exc)) from None
            except FileNotFoundError:
                descriptor = -1
            try:
                if descriptor >= 0:
                    actual = _reader._opened_handle_path(descriptor)
                    if actual is None or not self._same_directory(actual, name):
                        raise ConfinedReadRefused("the target is not directly in the validated directory")
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            os.replace(os.path.join(self._path, temp), target)
            return
        try:
            info = os.stat(name, dir_fd=self._fd, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is not None and not stat.S_ISREG(info.st_mode):
            raise ConfinedReadRefused("the target is a link or not a regular file")
        os.replace(temp, name, src_dir_fd=self._fd, dst_dir_fd=self._fd)

    def discard(self, temp: str) -> None:
        try:
            if os.name == "nt":
                os.unlink(os.path.join(self._path, temp))
            else:
                os.unlink(temp, dir_fd=self._fd)
        except OSError:
            pass

    def rewrite(self, name: str, text: str) -> None:
        """Stage and commit ``text`` as the new content of ``name``."""
        temp = self.stage(name, text)
        try:
            self.commit(temp, name)
        except BaseException:
            self.discard(temp)
            raise


def _open_directory_windows(path: str) -> int:
    """Open a directory without following a link and without ``FILE_SHARE_DELETE``."""
    if sys.platform != "win32":
        raise ConfinedReadRefused("Windows directory handles are only available on Windows")
    import ctypes
    import msvcrt

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [("attributes", ctypes.c_uint32), ("reparse_tag", ctypes.c_uint32)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close = kernel32.CloseHandle
    close.argtypes = [ctypes.c_void_p]
    close.restype = ctypes.c_int
    create = kernel32.CreateFileW
    create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                       ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    create.restype = ctypes.c_void_p
    # GENERIC_READ, share read|write (not delete), OPEN_EXISTING, BACKUP_SEMANTICS | OPEN_REPARSE_POINT.
    raw = create(path, 0x80000000, 0x3, None, 3, 0x02000000 | 0x00200000, None)
    if raw == ctypes.c_void_p(-1).value or raw is None:
        raise ConfinedReadRefused("the directory could not be opened (error %d)" % ctypes.get_last_error())
    info = FileAttributeTagInfo()
    inspect = kernel32.GetFileInformationByHandleEx
    inspect.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    inspect.restype = ctypes.c_int
    if not inspect(raw, 9, ctypes.byref(info), ctypes.sizeof(info)) or info.attributes & 0x400:
        close(raw)
        raise ConfinedReadRefused("the directory is a symlink or junction, or could not be inspected")
    try:
        return msvcrt.open_osfhandle(raw, os.O_RDONLY)
    except Exception:
        close(raw)
        raise
