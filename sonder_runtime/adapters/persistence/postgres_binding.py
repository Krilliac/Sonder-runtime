"""Host-owned PostgreSQL binding and private credential closure."""

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
from threading import RLock

from ...application.compute_fabric.artifact_spool import PrivateDirectoryAnchor
from ...application.ports.continuation_mutations import ContinuationStorageFailure


def _private_file(stream):
    metadata = os.fstat(stream.fileno())
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError("private regular file required")
    if os.name != "nt":
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ValueError("private file permissions required")
        return
    import ctypes
    import msvcrt
    from ...application.compute_fabric.artifact_spool import (
        _windows_current_user_sid_string,
    )

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    info = advapi.GetSecurityInfo
    info.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        *([ctypes.POINTER(ctypes.c_void_p)] * 5),
    ]
    info.restype = ctypes.c_uint32
    get_ace = advapi.GetAce
    get_ace.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_ace.restype = ctypes.c_int
    stringify = advapi.ConvertSidToStringSidW
    stringify.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    stringify.restype = ctypes.c_int
    free = kernel.LocalFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_void_p
    owner, acl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()

    def sid_text(pointer):
        text = ctypes.c_wchar_p()
        if not stringify(pointer, ctypes.byref(text)):
            raise ValueError("file ACL unavailable")
        try:
            return text.value
        finally:
            free(ctypes.cast(text, ctypes.c_void_p))

    if info(
        msvcrt.get_osfhandle(stream.fileno()),
        1,
        5,
        ctypes.byref(owner),
        None,
        ctypes.byref(acl),
        None,
        ctypes.byref(descriptor),
    ):
        raise ValueError("file ACL unavailable")
    try:
        expected = _windows_current_user_sid_string()
        if not owner or sid_text(owner) != expected or not acl:
            raise ValueError("private file owner required")
        count = ctypes.c_uint16.from_address(acl.value + 4).value
        if not 1 <= count <= 8:
            raise ValueError("private file ACL required")
        for index in range(count):
            ace = ctypes.c_void_p()
            if not get_ace(acl, index, ctypes.byref(ace)):
                raise ValueError("file ACL unavailable")
            ace_type = ctypes.c_ubyte.from_address(ace.value).value
            flags = ctypes.c_ubyte.from_address(ace.value + 1).value
            size = ctypes.c_uint16.from_address(ace.value + 2).value
            if (
                ace_type != 0
                or flags & 8
                or size < 12
                or sid_text(ace.value + 8) not in (expected, "S-1-5-18")
            ):
                raise ValueError("private file ACL required")
    finally:
        free(descriptor)


class PostgresPrivateBinding:
    """One immutable private bundle; no libpq services or ambient credentials."""

    def __init__(self, path, *, writable_roots):
        self._lock = RLock()
        self._writable_roots = writable_roots
        self._files = {}
        self._anchor = None
        try:
            path = Path(path)
            if not path.is_absolute():
                raise ValueError("absolute binding required")
            self._root = path.parent
            self._check_roots()
            self._anchor = PrivateDirectoryAnchor(self._root)

            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate binding field")
                    result[key] = value
                return result

            value = json.loads(self._read(path, 16384), object_pairs_hook=unique)
            allowed = {
                "host",
                "port",
                "database",
                "user",
                "passfile",
                "sslmode",
                "sslrootcert",
            }
            if (
                not isinstance(value, dict)
                or set(value) - allowed
                or not (allowed - {"sslrootcert"}).issubset(value)
            ):
                raise ValueError("invalid binding fields")
            for key in ("database", "user"):
                if not isinstance(value[key], str) or not re.fullmatch(
                    r"[A-Za-z0-9_-]{1,63}", value[key]
                ):
                    raise ValueError("invalid database binding")
            host = value["host"]
            if (
                not isinstance(host, str)
                or len(host) > 253
                or not re.fullmatch(r"[A-Za-z0-9.:-]+", host)
            ):
                raise ValueError("single fixed host required")
            if type(value["port"]) is not int or not 1 <= value["port"] <= 65535:
                raise ValueError("invalid port")
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = False
            if value["sslmode"] not in ("disable", "verify-full") or (
                not loopback and value["sslmode"] != "verify-full"
            ):
                raise ValueError("verified TLS required outside numeric loopback")
            self._read(Path(value["passfile"]), 16384)
            if value["sslmode"] == "verify-full":
                self._read(Path(value["sslrootcert"]), 1024 * 1024)
            elif "sslrootcert" in value:
                raise ValueError("unused TLS binding rejected")
            self._value = value
            self.validate()
        except Exception:
            self.close()
            raise ContinuationStorageFailure(
                "PostgreSQL private binding invalid or unavailable"
            ) from None

    def _check_roots(self):
        root = self._root.resolve()
        for value in self._writable_roots():
            writable = Path(value).resolve()
            if (
                root == writable
                or root.is_relative_to(writable)
                or writable.is_relative_to(root)
            ):
                raise ValueError("PostgreSQL binding overlaps writable roots")
        if any(key.upper().startswith("PG") for key in os.environ):
            raise ValueError("ambient libpq configuration is not accepted")

    def _read(self, path, maximum):
        if not path.is_absolute() or path.parent != self._root or not path.name:
            raise ValueError("binding files must be in the same private bundle")
        with self._anchor.open_read(path.name) as stream:
            _private_file(stream)
            metadata = os.fstat(stream.fileno())
            value = stream.read(maximum + 1)
        if len(value) > maximum or not value:
            raise ValueError("binding file exceeds bounds")
        self._files[path.name] = (
            maximum,
            metadata.st_dev,
            metadata.st_ino,
            hashlib.sha256(value).digest(),
        )
        return value

    def validate(self):
        with self._lock:
            try:
                self._check_roots()
                self._anchor.validate()
                for name, expected in self._files.items():
                    with self._anchor.open_read(name) as stream:
                        _private_file(stream)
                        metadata = os.fstat(stream.fileno())
                        digest = hashlib.sha256(stream.read(expected[0] + 1)).digest()
                    if (metadata.st_dev, metadata.st_ino, digest) != expected[1:]:
                        raise ValueError("binding changed")
            except Exception:
                raise ContinuationStorageFailure(
                    "PostgreSQL private binding no longer current"
                ) from None

    def connection_kwargs(self, config):
        self.validate()
        value = self._value
        result = dict(
            autocommit=True,  # Repository owns explicit BEGIN/COMMIT boundaries.
            host=value["host"],
            port=value["port"],
            dbname=value["database"],
            user=value["user"],
            passfile=value["passfile"],
            sslmode=value["sslmode"],
            sslcertmode="disable",
            gssencmode="disable",
            require_auth="scram-sha-256",
            connect_timeout=2,
            application_name="sonder-child-storage",
            options="-c statement_timeout=2000 -c lock_timeout=1000",
        )
        if "sslrootcert" in value:
            result["sslrootcert"] = value["sslrootcert"]
        return result

    def close(self):
        if self._anchor is not None:
            self._anchor.close()
