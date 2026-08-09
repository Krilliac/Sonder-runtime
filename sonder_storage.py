"""Bounded, cross-platform storage inspection for operator diagnostics.

Metadata inspection is read-only.  The throughput probe is a separate,
explicit operation which writes at most :data:`PROBE_BYTES` to one temporary
file, runs in a killable child process, and always attempts cleanup.
"""
from __future__ import annotations

import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PROBE_BYTES = 8 * 1024 * 1024
PROBE_TIMEOUT_SECONDS = 5.0
_MOUNTINFO_LIMIT = 1024 * 1024
_VOLUME_METADATA_LIMIT = 256 * 1024

_NETWORK_FILESYSTEMS = frozenset({
    "9p", "afs", "ceph", "cifs", "davfs", "fuse.sshfs", "gcsfuse",
    "glusterfs", "nfs", "nfs4", "smb3", "smbfs", "sshfs", "virtiofs",
})
_SLOW_FILESYSTEMS = frozenset({"fuseblk", "udf"})


def model_roots(env: dict[str, str] | None = None) -> tuple[Path, ...]:
    """Return configured/default Ollama model roots without creating them."""
    values = os.environ if env is None else env
    configured = str(values.get("OLLAMA_MODELS", "")).strip()
    candidates = (
        [Path(configured).expanduser()]
        if configured else [Path.home() / ".ollama" / "models"]
    )
    return _unique_paths(candidates)


def _unique_paths(paths) -> tuple[Path, ...]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        candidate = Path(path).expanduser().resolve(strict=False)
        key = os.path.normcase(str(candidate))
        if key not in seen:
            seen.add(key)
            result.append(candidate)
    return tuple(result)


def _existing_ancestor(path: Path) -> Path:
    candidate = path.resolve(strict=False)
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _windows_storage(path: Path) -> dict[str, Any]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_volume_path = kernel32.GetVolumePathNameW
    get_volume_path.argtypes = (
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
    )
    get_volume_path.restype = wintypes.BOOL
    get_drive_type = kernel32.GetDriveTypeW
    get_drive_type.argtypes = (wintypes.LPCWSTR,)
    get_drive_type.restype = wintypes.UINT
    get_volume_info = kernel32.GetVolumeInformationW
    get_volume_info.argtypes = (
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD,
    )
    get_volume_info.restype = wintypes.BOOL

    root_buffer = ctypes.create_unicode_buffer(32768)
    if not get_volume_path(
        str(path), root_buffer, len(root_buffer)
    ):
        raise OSError(ctypes.get_last_error(), "GetVolumePathNameW failed")
    root = root_buffer.value
    drive_type = int(get_drive_type(root))
    types = {
        0: "unknown", 1: "invalid", 2: "removable", 3: "fixed",
        4: "network", 5: "optical", 6: "ramdisk",
    }
    fs_buffer = ctypes.create_unicode_buffer(256)
    ok = get_volume_info(
        root, None, 0, None, None, None,
        fs_buffer, len(fs_buffer),
    )
    return {
        "mount": root,
        "filesystem": fs_buffer.value.lower() if ok else "unknown",
        "drive_type": types.get(drive_type, "unknown"),
        "network": drive_type == 4,
        "removable": drive_type in (2, 5),
    }


def _unescape_mountinfo(value: str) -> str:
    for encoded, decoded in (("\\040", " "), ("\\011", "\t"),
                             ("\\012", "\n"), ("\\134", "\\")):
        value = value.replace(encoded, decoded)
    return value


def _linux_storage(path: Path) -> dict[str, Any]:
    target = str(path.resolve(strict=False))
    with open("/proc/self/mountinfo", "rb") as handle:
        raw = handle.read(_MOUNTINFO_LIMIT + 1)
    if len(raw) > _MOUNTINFO_LIMIT:
        raise OSError("mountinfo exceeds bounded inspection limit")
    best: tuple[int, list[str], list[str]] | None = None
    for line in raw.decode("utf-8", "replace").splitlines():
        left, separator, right = line.partition(" - ")
        if not separator:
            continue
        fields, after = left.split(), right.split()
        if len(fields) < 6 or len(after) < 2:
            continue
        mount = _unescape_mountinfo(fields[4])
        try:
            common = os.path.commonpath((target, mount))
        except ValueError:
            continue
        if common == mount and (best is None or len(mount) > best[0]):
            best = (len(mount), fields, after)
    if best is None:
        return {"mount": "unknown", "filesystem": "unknown",
                "drive_type": "unknown", "network": False,
                "removable": False}
    fields, after = best[1], best[2]
    mount, filesystem, source = _unescape_mountinfo(fields[4]), after[0], after[1]
    network = filesystem.lower() in _NETWORK_FILESYSTEMS
    removable = False
    major_minor = fields[2]
    removable_path = Path("/sys/dev/block") / major_minor / "removable"
    try:
        removable = removable_path.read_text(encoding="ascii").strip() == "1"
    except OSError:
        pass
    return {
        "mount": mount, "filesystem": filesystem.lower(),
        "drive_type": "network" if network else (
            "removable" if removable else "local"
        ),
        "network": network, "removable": removable, "source": source,
    }


def _posix_storage(path: Path) -> dict[str, Any]:
    # Other POSIX systems have no stable stdlib volume API. Retain useful
    # capacity metadata without guessing from machine-specific paths.
    return {"mount": str(path.anchor or "/"), "filesystem": "unknown",
            "drive_type": "local-unknown", "network": False,
            "removable": False,
            "classification_error": "native volume metadata unavailable"}


def _macos_storage(path: Path) -> dict[str, Any]:
    diskutil = shutil.which("diskutil")
    if not diskutil:
        return _posix_storage(path)
    result = subprocess.run(
        [diskutil, "info", "-plist", str(path)], capture_output=True,
        timeout=2.0, check=False,
    )
    if result.returncode != 0:
        raise OSError("diskutil could not classify volume")
    if len(result.stdout) > _VOLUME_METADATA_LIMIT:
        raise OSError("diskutil metadata exceeds bounded inspection limit")
    info = plistlib.loads(result.stdout)
    filesystem = str(
        info.get("FilesystemType") or info.get("Type (Bundle)") or "unknown"
    ).lower()
    protocol = str(info.get("BusProtocol") or info.get("Protocol") or "").lower()
    removable = bool(info.get("Removable", False) or info.get("Ejectable", False))
    network = filesystem in _NETWORK_FILESYSTEMS or protocol in {
        "afp", "nfs", "smb", "webdav"
    }
    return {
        "mount": str(info.get("MountPoint") or path.anchor or "/"),
        "filesystem": filesystem,
        "drive_type": "network" if network else (
            "removable" if removable else "local"
        ),
        "network": network, "removable": removable,
        "protocol": protocol or "unknown",
    }


def classify(path: Path, *, system: str | None = None) -> dict[str, Any]:
    """Classify the volume containing *path* without writing to it."""
    existing = _existing_ancestor(path)
    name = platform.system() if system is None else system
    if name == "Windows":
        return _windows_storage(existing)
    if name == "Linux":
        return _linux_storage(existing)
    if name == "Darwin":
        return _macos_storage(existing)
    return _posix_storage(existing)


def inspect_root(path: str | os.PathLike, *, minimum_free_bytes: int = 0,
                 role: str = "storage") -> dict[str, Any]:
    """Return capacity and best-effort volume metadata for a logical root."""
    requested = Path(path).expanduser().resolve(strict=False)
    target = _existing_ancestor(requested)
    usage = shutil.disk_usage(target)
    try:
        volume = classify(target)
    except (OSError, ValueError) as exc:
        volume = {"mount": "unknown", "filesystem": "unknown",
                  "drive_type": "unknown", "network": False,
                  "removable": False, "classification_error": str(exc)}
    warnings: list[str] = []
    filesystem = str(volume.get("filesystem", "unknown")).lower()
    if volume.get("network"):
        warnings.append("network storage may add latency and disconnect risk")
    if volume.get("removable"):
        warnings.append("removable storage may be slow or disconnected")
    if filesystem in _SLOW_FILESYSTEMS:
        warnings.append("filesystem may be slow for model/state workloads")
    if usage.free < minimum_free_bytes:
        warnings.append(
            "free space below required minimum (%d < %d bytes)"
            % (usage.free, minimum_free_bytes)
        )
    if volume.get("classification_error"):
        warnings.append(
            "volume classification unavailable: %s"
            % volume["classification_error"]
        )
    return {
        "role": role, "path": str(requested), "exists": requested.exists(),
        "capacity_path": str(target), "free_bytes": usage.free,
        "total_bytes": usage.total, "minimum_free_bytes": minimum_free_bytes,
        **volume, "warnings": warnings,
    }


_PROBE_CHILD = r"""
import json, os, sys, time
path, size = sys.argv[1], int(sys.argv[2])
block = b'\0' * min(size, 1024 * 1024)
start = time.monotonic()
with open(path, 'wb', buffering=0) as f:
    remaining = size
    while remaining:
        chunk = block[:min(len(block), remaining)]
        f.write(chunk); remaining -= len(chunk)
    f.flush(); os.fsync(f.fileno())
write_seconds = max(time.monotonic() - start, 1e-9)
start = time.monotonic(); read_bytes = 0
with open(path, 'rb', buffering=0) as f:
    while read_bytes < size:
        chunk = f.read(min(1024 * 1024, size - read_bytes))
        if not chunk: break
        read_bytes += len(chunk)
read_seconds = max(time.monotonic() - start, 1e-9)
print(json.dumps({'bytes': size, 'read_bytes': read_bytes,
 'write_mib_s': size / write_seconds / 1048576,
 'read_mib_s': read_bytes / read_seconds / 1048576}))
"""


def throughput_probe(path: str | os.PathLike) -> dict[str, Any]:
    """Run the fixed-cap explicit probe and remove its sole temporary file."""
    root = Path(path).expanduser().resolve(strict=False)
    if not root.is_dir():
        raise OSError("probe root must be an existing directory: %s" % root)
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".sonder-storage-probe-", suffix=".tmp", dir=root,
            delete=False,
        ) as handle:
            temp_path = handle.name
        started = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-c", _PROBE_CHILD, temp_path, str(PROBE_BYTES)],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
        elapsed = time.monotonic() - started
        if result.returncode != 0:
            raise OSError("storage probe failed: %s" % result.stderr.strip()[:300])
        payload = json.loads(result.stdout)
        if payload.get("bytes") != PROBE_BYTES or payload.get("read_bytes") != PROBE_BYTES:
            raise OSError("storage probe returned an invalid byte count")
        payload.update({"path": str(root), "elapsed_seconds": elapsed,
                        "byte_cap": PROBE_BYTES,
                        "timeout_seconds": PROBE_TIMEOUT_SECONDS})
        return payload
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            "storage probe exceeded %.1f second cap" % PROBE_TIMEOUT_SECONDS
        ) from exc
    finally:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)


def summarize(record: dict[str, Any]) -> str:
    gib = record["free_bytes"] / (1024 ** 3)
    detail = "%s: %.1f GiB free, %s/%s" % (
        record["path"], gib, record.get("drive_type", "unknown"),
        record.get("filesystem", "unknown"),
    )
    if not record["exists"]:
        detail += " (root not created; measured nearest existing parent)"
    if record["warnings"]:
        detail += "; " + "; ".join(record["warnings"])
    return detail


def inspect_config(config) -> dict[str, Any]:
    """Inspect configured state and discovered Ollama roots, read-only."""
    minimum = int(config.state.minimum_free_disk_bytes)
    state = inspect_root(config.state.home, minimum_free_bytes=minimum,
                         role="state")
    models = [
        inspect_root(root, minimum_free_bytes=minimum, role="models")
        for root in model_roots()
    ]
    return {"state": state, "models": models}
