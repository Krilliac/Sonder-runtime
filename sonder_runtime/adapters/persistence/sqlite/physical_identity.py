"""Local physical-host identity for the worker capacity fence.

Only an opaque digest is persisted.  Windows and Linux/WSL deliberately use
different source labels, so a shared database cannot silently combine a host
and its WSL environment.  This is an identity fence, not an attestation that
the operating system or hardware has not been cloned.
"""
from __future__ import annotations

import hashlib
import platform
from pathlib import Path

from sonder_runtime.domain.worker_capacity import PhysicalHostIdentity


_MAX_ID_BYTES = 512


def _read_machine_id(path: str) -> bytes | None:
    try:
        value = Path(path).read_bytes()[:_MAX_ID_BYTES].strip()
    except (OSError, ValueError):
        return None
    return value or None


def _windows_machine_guid() -> bytes | None:
    if platform.system().casefold() != "windows":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
        ) as key:
            value, _kind = winreg.QueryValueEx(key, "MachineGuid")
    except (ImportError, OSError, TypeError, ValueError):
        return None
    if not isinstance(value, str):
        return None
    encoded = value.strip().encode("utf-8", "strict")[:_MAX_ID_BYTES]
    return encoded or None


def physical_host_identity(authority_id: str) -> PhysicalHostIdentity:
    """Return a stable opaque identity for this OS instance.

    The machine-id sources are preferred.  A bounded platform/hostname
    fallback keeps development systems usable when those sources are absent;
    callers must still treat the resulting digest as a conservative fence,
    not as proof of unique hardware ownership.
    """
    system = platform.system().strip().casefold() or "unknown"
    machine = platform.machine().strip().casefold() or "unknown"
    source = "windows-machine-guid" if system == "windows" else "linux-machine-id"
    machine_id = _windows_machine_guid() if system == "windows" else (
        _read_machine_id("/etc/machine-id")
        or _read_machine_id("/var/lib/dbus/machine-id")
    )
    if machine_id is None:
        source = "platform-fallback"
        machine_id = (
            (platform.node().strip() or "unknown").encode("utf-8", "replace")[:_MAX_ID_BYTES]
            + b"\0"
            + (platform.processor().strip() or "unknown").encode("utf-8", "replace")[:_MAX_ID_BYTES]
        )
    material = b"sonder-physical-host-v1\0" + source.encode("ascii") + b"\0" + system.encode("utf-8") + b"\0" + machine.encode("utf-8") + b"\0" + machine_id
    fingerprint = hashlib.sha256(material).hexdigest()
    return PhysicalHostIdentity(authority_id, fingerprint, source)


__all__ = ["physical_host_identity"]
