"""Bounded, cross-platform storage inspection for operator diagnostics.

Metadata inspection is read-only. The throughput probe is a separate,
explicit operation which writes at most :data:`PROBE_BYTES` through one
anonymous-or-delete-on-close handle and relies on handle closure for cleanup.
"""
from __future__ import annotations

from sonder_runtime.platform.runtime_threads import Thread as owned_runtime_thread

import os
import platform
import shutil
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from sonder_runtime.platform.model_paths import model_roots

PROBE_BYTES = 8 * 1024 * 1024
PROBE_TIMEOUT_SECONDS = 5.0
_MOUNTINFO_LIMIT = 1024 * 1024

_NETWORK_FILESYSTEMS = frozenset({
    "9p", "afs", "ceph", "cifs", "davfs", "fuse.sshfs", "gcsfuse",
    "glusterfs", "nfs", "nfs4", "smb3", "smbfs", "sshfs", "virtiofs",
})
_SLOW_FILESYSTEMS = frozenset({"fuseblk", "udf"})
_PROBE_RESULT = struct.Struct("!QQdd")


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
    removable = _linux_removable(fields[2])
    return {
        "mount": mount, "filesystem": filesystem.lower(),
        "drive_type": "network" if network else (
            "removable" if removable else "local"
        ),
        "network": network, "removable": removable, "source": source,
    }


def _linux_removable(
    major_minor: str, *, sys_block_root: Path = Path("/sys/dev/block")
) -> bool:
    """Read a device or parent-disk removable flag with bounded traversal."""
    try:
        node = (sys_block_root / major_minor).resolve(strict=True)
    except OSError:
        return False
    candidates = (node,) + tuple(node.parents[:4])
    for candidate in candidates:
        flag = candidate / "removable"
        try:
            with flag.open("rb") as handle:
                value = handle.read(9)
        except OSError:
            continue
        if len(value) > 8:
            continue
        stripped = value.strip()
        if stripped in (b"0", b"1"):
            return stripped == b"1"
    return False


def _posix_storage(path: Path) -> dict[str, Any]:
    # Other POSIX systems have no stable stdlib volume API. Retain useful
    # capacity metadata without guessing from machine-specific paths.
    return {"mount": str(path.anchor or "/"), "filesystem": "unknown",
            "drive_type": "local-unknown", "network": False,
            "removable": False,
            "classification_error": "native volume metadata unavailable"}


def _macos_storage(path: Path) -> dict[str, Any]:
    # There is no stable macOS stdlib volume-type API.  Automatic diagnostics
    # deliberately avoid spawning diskutil with the caller's environment or
    # buffering untrusted command output; capacity remains available via
    # shutil.disk_usage and classification is reported as unavailable.
    return _posix_storage(path)


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


_PROBE_WORKER = r"""
import os, stat, struct, sys, tempfile, time
root, cap = sys.argv[1], int(sys.argv[2])
block = b'\0' * (64 * 1024)
with tempfile.TemporaryFile(dir=root) as handle:
    fd = handle.fileno()
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_size != 0:
        raise OSError('probe did not receive a new regular file')
    identity = (before.st_dev, before.st_ino)
    write_started = time.monotonic(); written = 0
    while written < cap:
        amount = os.write(fd, block[:min(len(block), cap - written)])
        if amount <= 0: raise OSError('write made no progress')
        written += amount
    os.fsync(fd)
    write_seconds = max(time.monotonic() - write_started, 1e-9)
    current = os.fstat(fd)
    if (current.st_dev, current.st_ino) != identity or current.st_size != cap:
        raise OSError('probe file identity or size changed')
    os.lseek(fd, 0, os.SEEK_SET)
    read_started = time.monotonic(); read_bytes = 0
    while read_bytes < cap:
        chunk = os.read(fd, min(len(block), cap - read_bytes))
        if not chunk: break
        read_bytes += len(chunk)
    read_seconds = max(time.monotonic() - read_started, 1e-9)
    final = os.fstat(fd)
    if ((final.st_dev, final.st_ino) != identity or final.st_size != cap
            or read_bytes != cap):
        raise OSError('probe result identity, size, or count changed')
payload = struct.pack('!QQdd', written, read_bytes,
                      written / write_seconds / 1048576,
                      read_bytes / read_seconds / 1048576)
sys.stdout.buffer.write(payload); sys.stdout.buffer.flush()
"""


def _worker_environment() -> dict[str, str]:
    """Return only OS bootstrap variables; never forward caller secrets."""
    allowed = ("SYSTEMROOT", "WINDIR")
    env = {name: os.environ[name] for name in allowed if os.environ.get(name)}
    env["PYTHONNOUSERSITE"] = "1"
    return env


def _bounded_worker_output(pipe, buffer: bytearray) -> None:
    """Read at most one fixed result plus an overflow sentinel byte."""
    try:
        while len(buffer) <= _PROBE_RESULT.size:
            chunk = pipe.read(_PROBE_RESULT.size + 1 - len(buffer))
            if not chunk:
                break
            buffer.extend(chunk)
    finally:
        pipe.close()


def throughput_probe(path: str | os.PathLike) -> dict[str, Any]:
    """Run a killable, isolated, exact-cap probe with no pathname reopen.

    The isolated worker creates and uses its own ``TemporaryFile`` handle. It
    receives no caller environment, emits a fixed-size binary result, and is
    killed at the wall-clock deadline; process teardown closes the anonymous
    or delete-on-close file even when setup, I/O, sync, or cleanup blocks.
    """
    root = Path(path).expanduser()
    started = time.monotonic()
    command = [
        sys.executable, "-I", "-S", "-E", "-c", _PROBE_WORKER,
        str(root), str(PROBE_BYTES),
    ]
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, env=_worker_environment(),
        close_fds=True,
    )
    assert process.stdout is not None
    output = bytearray()
    reader = owned_runtime_thread(
        target=_bounded_worker_output, args=(process.stdout, output),
        name="sonder-storage-probe-result", daemon=True,
    )
    reader.start()
    remaining = max(0.0, PROBE_TIMEOUT_SECONDS - (time.monotonic() - started))
    try:
        return_code = process.wait(timeout=remaining)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
        reader.join(timeout=1.0)
        raise TimeoutError(
            "storage probe exceeded %.1f second cap"
            % PROBE_TIMEOUT_SECONDS
        ) from exc
    reader.join(timeout=1.0)
    if reader.is_alive():
        process.kill()
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
        reader.join(timeout=1.0)
        raise OSError("storage probe result pipe did not close")
    if return_code != 0:
        raise OSError("isolated storage probe failed")
    if len(output) != _PROBE_RESULT.size:
        raise OSError("storage probe returned invalid or excessive output")
    written, read_bytes, write_mib_s, read_mib_s = _PROBE_RESULT.unpack(output)
    if written != PROBE_BYTES or read_bytes != PROBE_BYTES:
        raise OSError("storage probe returned an invalid byte count")
    elapsed = time.monotonic() - started
    if elapsed > PROBE_TIMEOUT_SECONDS:
        raise TimeoutError(
            "storage probe exceeded %.1f second cap" % PROBE_TIMEOUT_SECONDS
        )
    return {
        "path": str(root), "bytes": written, "read_bytes": read_bytes,
        "write_mib_s": write_mib_s, "read_mib_s": read_mib_s,
        "elapsed_seconds": elapsed, "byte_cap": PROBE_BYTES,
        "timeout_seconds": PROBE_TIMEOUT_SECONDS,
        "temporary_file": "isolated-handle",
    }


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
