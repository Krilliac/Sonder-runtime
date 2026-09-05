"""Race-resistant private storage for immutable compute artifact snapshots."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
from typing import BinaryIO


class ArtifactSpoolError(RuntimeError):
    """The private artifact spool cannot be proven safe."""


class ArtifactSpoolConflict(ArtifactSpoolError):
    """A durable spool name is already bound to different content."""


def _is_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))


def _windows_current_user_sid_string() -> str:
    """Return the process token owner SID without shelling out to account tools."""
    import ctypes

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("sid", ctypes.c_void_p), ("attributes", ctypes.c_uint32)]

    class TokenUser(ctypes.Structure):
        _fields_ = [("user", SidAndAttributes)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = []
    get_current_process.restype = ctypes.c_void_p
    open_token = advapi32.OpenProcessToken
    open_token.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
    open_token.restype = ctypes.c_int
    get_token_information = advapi32.GetTokenInformation
    get_token_information.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    get_token_information.restype = ctypes.c_int
    convert_sid = advapi32.ConvertSidToStringSidW
    convert_sid.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    convert_sid.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p

    token = ctypes.c_void_p()
    if not open_token(get_current_process(), 0x0008, ctypes.byref(token)):
        raise OSError(ctypes.get_last_error(), "could not inspect artifact spool owner")
    try:
        needed = ctypes.c_uint32()
        get_token_information(token, 1, None, 0, ctypes.byref(needed))
        if not needed.value:
            raise OSError(ctypes.get_last_error(), "could not inspect artifact spool owner")
        buffer = ctypes.create_string_buffer(needed.value)
        if not get_token_information(
            token, 1, buffer, needed.value, ctypes.byref(needed)
        ):
            raise OSError(ctypes.get_last_error(), "could not inspect artifact spool owner")
        token_user = ctypes.cast(buffer, ctypes.POINTER(TokenUser)).contents
        value = ctypes.c_wchar_p()
        if not convert_sid(token_user.user.sid, ctypes.byref(value)):
            raise OSError(ctypes.get_last_error(), "could not inspect artifact spool owner")
        try:
            return str(value.value)
        finally:
            local_free(ctypes.cast(value, ctypes.c_void_p))
    finally:
        close_handle(token)


def _windows_create_private_directory(path: Path) -> bool:
    """Create a directory with a protected user-and-SYSTEM full-control DACL."""
    import ctypes

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_uint32),
            ("security_descriptor", ctypes.c_void_p),
            ("inherit_handle", ctypes.c_int),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    convert.restype = ctypes.c_int
    create_directory = kernel32.CreateDirectoryW
    create_directory.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(SecurityAttributes)]
    create_directory.restype = ctypes.c_int
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p

    user_sid = _windows_current_user_sid_string()
    descriptor = ctypes.c_void_p()
    sddl = f"O:{user_sid}D:P(A;OICI;FA;;;{user_sid})(A;OICI;FA;;;SY)"
    if not convert(sddl, 1, ctypes.byref(descriptor), None):
        raise OSError(ctypes.get_last_error(), "could not prepare private artifact spool ACL")
    try:
        attributes = SecurityAttributes(
            ctypes.sizeof(SecurityAttributes), descriptor, False,
        )
        if create_directory(str(path), ctypes.byref(attributes)):
            return True
        error = ctypes.get_last_error()
        if error == 183:  # ERROR_ALREADY_EXISTS
            return False
        raise OSError(error, "could not create private artifact spool")
    finally:
        local_free(descriptor)


def _windows_validate_private_directory(handle) -> None:
    """Validate owner and protected DACL through the already-held directory handle."""
    import ctypes

    class AclSizeInformation(ctypes.Structure):
        _fields_ = [
            ("ace_count", ctypes.c_uint32),
            ("acl_bytes_in_use", ctypes.c_uint32),
            ("acl_bytes_free", ctypes.c_uint32),
        ]

    class AceHeader(ctypes.Structure):
        _fields_ = [
            ("ace_type", ctypes.c_ubyte),
            ("ace_flags", ctypes.c_ubyte),
            ("ace_size", ctypes.c_uint16),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    get_security_info = advapi32.GetSecurityInfo
    get_security_info.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_security_info.restype = ctypes.c_uint32
    get_control = advapi32.GetSecurityDescriptorControl
    get_control.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint16),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    get_control.restype = ctypes.c_int
    convert_sid = advapi32.ConvertStringSidToSidW
    convert_sid.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)]
    convert_sid.restype = ctypes.c_int
    equal_sid = advapi32.EqualSid
    equal_sid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    equal_sid.restype = ctypes.c_int
    get_acl_information = advapi32.GetAclInformation
    get_acl_information.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    get_acl_information.restype = ctypes.c_int
    get_ace = advapi32.GetAce
    get_ace.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_ace.restype = ctypes.c_int
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p

    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    error = get_security_info(
        handle, 1, 0x00000001 | 0x00000004,
        ctypes.byref(owner), None, ctypes.byref(dacl), None,
        ctypes.byref(descriptor),
    )
    if error:
        raise OSError(error, "could not inspect artifact spool permissions")
    user_sid = ctypes.c_void_p()
    system_sid = ctypes.c_void_p()
    try:
        if not convert_sid(_windows_current_user_sid_string(), ctypes.byref(user_sid)):
            raise OSError(ctypes.get_last_error(), "could not inspect artifact spool owner")
        if not convert_sid("S-1-5-18", ctypes.byref(system_sid)):
            raise OSError(ctypes.get_last_error(), "could not inspect artifact spool ACL")
        if not owner or not equal_sid(owner, user_sid):
            raise ArtifactSpoolError("private artifact spool has a foreign owner")
        control = ctypes.c_uint16()
        revision = ctypes.c_uint32()
        if not get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise OSError(ctypes.get_last_error(), "could not inspect artifact spool ACL")
        if not dacl or not control.value & 0x0004 or not control.value & 0x1000:
            raise ArtifactSpoolError("private artifact spool permissions are not protected")
        information = AclSizeInformation()
        if not get_acl_information(
            dacl, ctypes.byref(information), ctypes.sizeof(information), 2,
        ):
            raise OSError(ctypes.get_last_error(), "could not inspect artifact spool ACL")
        user_full_control_aces = 0
        system_full_control_aces = 0
        if information.ace_count != 2:
            raise ArtifactSpoolError("private artifact spool ACL has unexpected entries")
        for index in range(information.ace_count):
            ace = ctypes.c_void_p()
            if not get_ace(dacl, index, ctypes.byref(ace)):
                raise OSError(ctypes.get_last_error(), "could not inspect artifact spool ACL")
            header = ctypes.cast(ace, ctypes.POINTER(AceHeader)).contents
            if (
                header.ace_type != 0
                or header.ace_size < 12
                or header.ace_flags != 0x03
            ):
                raise ArtifactSpoolError("private artifact spool ACL is not exclusive")
            access_mask = ctypes.c_uint32.from_address(ace.value + 4).value
            ace_sid = ctypes.c_void_p(ace.value + 8)
            is_user = bool(equal_sid(ace_sid, user_sid))
            is_system = bool(equal_sid(ace_sid, system_sid))
            if not is_user and not is_system:
                raise ArtifactSpoolError("private artifact spool permissions are not private")
            if access_mask != 0x001F01FF:
                raise ArtifactSpoolError("private artifact spool ACL is not full control")
            if is_user:
                user_full_control_aces += 1
            if is_system:
                system_full_control_aces += 1
        if not user_full_control_aces or not system_full_control_aces:
            raise ArtifactSpoolError(
                "private artifact spool requires owner and SYSTEM full control"
            )
    finally:
        if system_sid:
            local_free(system_sid)
        if user_sid:
            local_free(user_sid)
        if descriptor:
            local_free(descriptor)


def _windows_handle_path(handle) -> Path:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.GetFinalPathNameByHandleW
    function.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
    ]
    function.restype = ctypes.c_uint32
    needed = function(handle, None, 0, 0)
    if not needed:
        raise OSError(ctypes.get_last_error(), "could not resolve artifact spool handle")
    buffer = ctypes.create_unicode_buffer(needed + 1)
    if not function(handle, buffer, len(buffer), 0):
        raise OSError(ctypes.get_last_error(), "could not resolve artifact spool handle")
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def _opened_fd_path(fd: int) -> Path:
    if os.name == "nt":
        import msvcrt

        return _windows_handle_path(msvcrt.get_osfhandle(fd))
    for link in (f"/proc/self/fd/{fd}", f"/dev/fd/{fd}"):
        try:
            value = os.readlink(link)
        except OSError:
            continue
        if value.endswith(" (deleted)"):
            raise ArtifactSpoolError("artifact spool entry was unlinked while open")
        return Path(value)
    raise ArtifactSpoolError("platform cannot resolve an opened artifact spool entry")


def _open_file_no_follow(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    if os.name != "nt":
        return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0))

    import ctypes
    import msvcrt

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [("attributes", ctypes.c_uint32), ("reparse_tag", ctypes.c_uint32)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    create.restype = ctypes.c_void_p
    raw_handle = create(
        str(path), 0x80000000, 0x00000001 | 0x00000002 | 0x00000004,
        None, 3, 0x00200000 | 0x08000000, None,
    )
    invalid = ctypes.c_void_p(-1).value
    if raw_handle in (None, invalid):
        raise OSError(ctypes.get_last_error(), "could not open artifact spool entry")
    info = FileAttributeTagInfo()
    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    get_info.restype = ctypes.c_int
    if not get_info(raw_handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        kernel32.CloseHandle(raw_handle)
        raise OSError(ctypes.get_last_error(), "could not inspect artifact spool entry")
    if info.attributes & 0x00000400:
        kernel32.CloseHandle(raw_handle)
        raise ArtifactSpoolError("artifact spool entry became a symlink or junction")
    try:
        return msvcrt.open_osfhandle(raw_handle, flags)
    except Exception:
        kernel32.CloseHandle(raw_handle)
        raise


class PrivateDirectoryAnchor:
    """Hold a private directory by identity and address children beneath it."""

    def __init__(self, path: Path, *, create: bool = False, require_new: bool = False) -> None:
        raw = Path(os.path.abspath(str(path)))
        parent = raw.parent
        if _normalized(parent) != _normalized(Path(os.path.realpath(parent))):
            raise ArtifactSpoolError("private artifact spool traverses a symlink or junction")
        if create:
            if os.name == "nt":
                created = _windows_create_private_directory(raw)
                if require_new and not created:
                    raise FileExistsError("required-new anchored directory already exists")
            else:
                try:
                    raw.mkdir(mode=0o700, parents=False, exist_ok=False)
                except FileExistsError:
                    if require_new:
                        raise
        elif require_new:
            raise ValueError("require_new needs directory creation")
        self.path = raw
        self.fd: int | None = None
        self.handle = None
        self._open()

    @classmethod
    def open_base(cls, path: Path, *, require_new: bool = False) -> "PrivateDirectoryAnchor":
        raw = Path(os.path.abspath(str(path)))
        raw.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return cls(raw, create=True, require_new=require_new)

    def _open(self) -> None:
        if _is_reparse(self.path):
            raise ArtifactSpoolError("private artifact spool may not be a symlink or junction")
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create = kernel32.CreateFileW
            create.argtypes = [
                ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                ctypes.c_void_p,
            ]
            create.restype = ctypes.c_void_p
            # FILE_LIST_DIRECTORY makes share checking apply: metadata-only
            # READ_ATTRIBUTES/READ_CONTROL handles do not prevent rename.
            # Keep delete sharing disabled while path-based operations use it.
            self.handle = create(
                str(self.path), 0x00000001 | 0x00000080 | 0x00020000,
                0x00000001 | 0x00000002,
                None, 3, 0x02000000 | 0x00200000, None,
            )
            invalid = ctypes.c_void_p(-1).value
            if self.handle in (None, invalid):
                self.handle = None
                raise OSError(ctypes.get_last_error(), "could not anchor artifact spool")
        else:
            flags = (
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            )
            self.fd = os.open(self.path, flags)
        try:
            self.validate()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.handle is not None:
            import ctypes

            close = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
            close.argtypes = [ctypes.c_void_p]
            close.restype = ctypes.c_int
            close(self.handle)
            self.handle = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def __enter__(self) -> "PrivateDirectoryAnchor":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def validate(self) -> None:
        if _is_reparse(self.path):
            raise ArtifactSpoolError("private artifact spool identity changed")
        metadata = self.path.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise ArtifactSpoolError("private artifact spool is not a directory")
        if os.name == "nt":
            opened = _windows_handle_path(self.handle)
            if _normalized(opened) != _normalized(self.path):
                raise ArtifactSpoolError("private artifact spool identity changed")
            _windows_validate_private_directory(self.handle)
        else:
            opened = os.fstat(self.fd)
            if not os.path.samestat(opened, metadata):
                raise ArtifactSpoolError("private artifact spool identity changed")
            geteuid = getattr(os, "geteuid", None)
            if callable(geteuid) and metadata.st_uid != geteuid():
                raise ArtifactSpoolError("private artifact spool has a foreign owner")
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise ArtifactSpoolError("private artifact spool permissions are not private")

    def child(self, name: str) -> tuple["PrivateDirectoryAnchor", bool]:
        if not name or any(character not in "0123456789abcdef" for character in name):
            raise ArtifactSpoolError("artifact spool child name is invalid")
        self.validate()
        created = False
        if os.name == "nt":
            child = self.path / name
            created = _windows_create_private_directory(child)
        else:
            try:
                os.mkdir(name, 0o700, dir_fd=self.fd)
                created = True
            except FileExistsError:
                pass
            metadata = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode):
                raise ArtifactSpoolError("artifact spool job entry is not a directory")
            child = self.path / name
        anchor = PrivateDirectoryAnchor(child)
        self.validate()
        return anchor, created

    def exists(self, name: str) -> bool:
        self.validate()
        try:
            if os.name == "nt":
                (self.path / name).lstat()
            else:
                os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def open_read(self, name: str) -> BinaryIO:
        self.validate()
        if os.name == "nt":
            fd = _open_file_no_follow(self.path / name)
        else:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(name, flags, dir_fd=self.fd)
        try:
            actual = _opened_fd_path(fd)
            if _normalized(actual.parent) != _normalized(self.path):
                raise ArtifactSpoolError(
                    "artifact spool entry escaped its anchored directory"
                )
            return os.fdopen(fd, "rb")
        except Exception:
            os.close(fd)
            raise

    def read_bytes(self, name: str, *, max_bytes: int) -> bytes:
        with self.open_read(name) as stream:
            value = stream.read(max_bytes + 1)
        if len(value) > max_bytes:
            raise ArtifactSpoolError("artifact spool metadata exceeds its bound")
        return value

    def create_temporary(self) -> tuple[int, str]:
        self.validate()
        if os.name == "nt":
            fd, path = tempfile.mkstemp(prefix="snapshot-", suffix=".part", dir=self.path)
            try:
                actual = _opened_fd_path(fd)
                if _normalized(actual.parent) != _normalized(self.path):
                    raise ArtifactSpoolError(
                        "artifact spool temporary escaped its directory"
                    )
                return fd, Path(path).name
            except Exception:
                os.close(fd)
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    pass
                raise
        flags = (
            os.O_RDWR | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        for _attempt in range(128):
            name = f"snapshot-{secrets.token_hex(12)}.part"
            try:
                return os.open(name, flags, 0o600, dir_fd=self.fd), name
            except FileExistsError:
                continue
        raise FileExistsError("could not allocate artifact spool temporary")

    def unlink(self, name: str) -> None:
        if os.name == "nt":
            (self.path / name).unlink()
        else:
            os.unlink(name, dir_fd=self.fd)

    def publish(self, temporary: str, destination: str) -> None:
        self.validate()
        if os.name == "nt":
            os.link(
                self.path / temporary,
                self.path / destination,
                follow_symlinks=False,
            )
        else:
            os.link(
                temporary,
                destination,
                src_dir_fd=self.fd,
                dst_dir_fd=self.fd,
                follow_symlinks=False,
            )
        self.unlink(temporary)
        if os.name != "nt":
            os.chmod(destination, 0o400, dir_fd=self.fd, follow_symlinks=False)
        self.validate()

    def write_json_once(self, name: str, value: dict) -> None:
        encoded = (
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        descriptor, temporary = self.create_temporary()
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            self.publish(temporary, name)
        except Exception:
            try:
                if self.exists(temporary):
                    self.unlink(temporary)
            except OSError:
                pass
            raise
