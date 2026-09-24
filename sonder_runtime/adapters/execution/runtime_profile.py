"""Host-selected lean Windows interpreter profile for the managed owner.

The workstation installer creates this profile in a separate, pinned runtime
venv. A model cannot select or seal one: the host supplies an absolute path,
and the owner checks its marker, base interpreter, installed distributions and
closure before constructing any managed child. The eventual runtime manifest
still hashes every file's contents at each launch boundary.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from hashlib import sha256
from importlib import metadata
from pathlib import Path

from ...application.ports.runtime_owner import OwnerRefused, canonical
from ..filesystem.atomic_json import write_json_atomic

PROFILE_NAME = "sonder-managed-runtime.json"
PROFILE_MAX_BYTES = 512 * 1024**2
PROFILE_MAX_FILES = 12000
PROFILE_MAX_MANIFEST = 128 * 1024
_PIN = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9._+!-]*)\s*\Z")


def _ordinary(path: Path, *, directory: bool) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise OwnerRefused(f"managed runtime profile path is missing: {path}") from exc
    if path.is_symlink() or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise OwnerRefused("managed runtime profile contains a reparse path")
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise OwnerRefused("managed runtime profile has an unexpected path type")


def _absolute_real_root(root: str | os.PathLike[str]) -> Path:
    raw = Path(root)
    if not raw.is_absolute() or any(part in (".", "..") for part in raw.parts):
        raise OwnerRefused("managed runtime profile requires an absolute canonical path")
    # Reject junction/symlink ancestors before resolving the root. The host
    # must not accidentally authenticate a different venv through an alias.
    for path in reversed((raw, *raw.parents)):
        _ordinary(path, directory=True)
    if os.path.normcase(str(raw)) != os.path.normcase(str(raw.resolve())):
        raise OwnerRefused("managed runtime profile root changes after resolution")
    return raw


def _norm_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _runtime_pins(source: Path) -> tuple[dict[str, str], str]:
    requirements = source / "requirements-runtime.txt"
    _ordinary(requirements, directory=False)
    raw = requirements.read_bytes()
    if len(raw) > 16 * 1024:
        raise OwnerRefused("managed runtime requirements exceed bounds")
    pins = {}
    for row in raw.decode("utf-8").splitlines():
        value = row.split("#", 1)[0].strip()
        if not value:
            continue
        requirement, separator, marker = value.partition(";")
        if separator:
            if marker.strip().replace("'", '"') != 'sys_platform == "win32"':
                raise OwnerRefused("unsupported managed runtime platform marker")
            if sys.platform != "win32":
                continue
        match = _PIN.fullmatch(requirement)
        if not match:
            raise OwnerRefused("managed runtime dependencies must use exact pins")
        key = _norm_name(match.group(1))
        if key in pins:
            raise OwnerRefused("duplicate managed runtime dependency pin")
        pins[key] = match.group(2)
    if not pins:
        raise OwnerRefused("managed runtime contract names no dependencies")
    # The marker binds the exact file, including its platform markers and
    # comments; a changed checkout requires explicit re-provisioning.
    return pins, sha256(raw).hexdigest()


def _installed(site: Path) -> dict[str, str]:
    result = {}
    for distribution in metadata.distributions(path=[str(site)]):
        name = distribution.metadata.get("Name", "")
        version = distribution.version
        if not name or not version or len(result) >= PROFILE_MAX_FILES:
            raise OwnerRefused("managed runtime package metadata is incomplete")
        key = _norm_name(name)
        if key in result:
            raise OwnerRefused("duplicate managed runtime distribution")
        result[key] = version
    return dict(sorted(result.items()))


def _site_summary(site: Path) -> tuple[int, int]:
    count, size = 0, 0
    for current, directories, names in os.walk(site, followlinks=False):
        current = Path(current)
        _ordinary(current, directory=True)
        for name in tuple(directories):
            _ordinary(current / name, directory=True)
        directories[:] = [name for name in directories if name != "__pycache__"]
        for name in names:
            path = current / name
            _ordinary(path, directory=False)
            count += 1
            size += path.lstat().st_size
            if count > PROFILE_MAX_FILES or size > PROFILE_MAX_BYTES:
                raise OwnerRefused("managed runtime lean profile exceeds its declared size budget")
    return count, size


def _profile_state(root: Path, *, source: Path, base: Path, executable: Path) -> dict:
    root = _absolute_real_root(root)
    if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 12):
        raise OwnerRefused("managed runtime profile requires CPython 3.12")
    base, executable = Path(base).resolve(), Path(executable).resolve()
    if executable.parent != base:
        raise OwnerRefused("managed runtime base Python identity is unsupported")
    config = root / "pyvenv.cfg"
    launcher = root / "Scripts" / "python.exe"
    site = root / "Lib" / "site-packages"
    for directory in (root / "Scripts", root / "Lib", site):
        _ordinary(directory, directory=True)
    for file in (config, launcher):
        _ordinary(file, directory=False)
    if config.stat().st_size > 8192:
        raise OwnerRefused("managed runtime venv configuration exceeds bounds")
    # Parse without invoking site.py or executable lines from .pth files.
    properties = {}
    for line in config.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            properties[key.strip().casefold()] = value.strip()
    home = Path(properties.get("home", ""))
    if (not home.is_absolute() or os.path.normcase(str(home.resolve())) != os.path.normcase(str(base))
            or properties.get("include-system-site-packages", "").casefold() != "false"):
        raise OwnerRefused("managed runtime venv inherits an untrusted interpreter or site")

    site_files, size = _site_summary(site)
    pins, contract = _runtime_pins(Path(source))
    installed = _installed(site)
    if any(installed.get(name) != version for name, version in pins.items()):
        raise OwnerRefused("managed runtime pinned dependencies are missing or stale")
    return {
        "schema": 1,
        "root": str(root),
        "base": str(base),
        "executable": str(executable),
        "python": list(sys.version_info[:2]),
        "requirements_sha256": contract,
        "packages": installed,
        "site_files": site_files,
        "site_bytes": size,
    }


def profile_files(root: str | os.PathLike[str], *, source: Path, base: Path, executable: Path) -> tuple[str, ...]:
    """Validate the installed profile and return files included in launch hashes."""
    root = _absolute_real_root(root)
    marker = root / PROFILE_NAME
    _ordinary(marker, directory=False)
    with marker.open("rb") as stream:
        raw = stream.read(PROFILE_MAX_MANIFEST + 1)
    if len(raw) > PROFILE_MAX_MANIFEST:
        raise OwnerRefused("managed runtime profile marker exceeds bounds")
    try:
        stored = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise OwnerRefused("managed runtime profile marker is invalid") from exc
    expected = _profile_state(root, source=source, base=base, executable=executable)
    if type(stored) is not dict or canonical(stored) != canonical(expected):
        raise OwnerRefused("managed runtime profile differs from provisioned interpreter")
    return (str(marker), str(root / "pyvenv.cfg"), str(root / "Scripts" / "python.exe"))


def seal_installed_profile(*, source: Path, root: Path, base: Path, executable: Path) -> Path:
    """Seal an installer-owned fresh venv; refuse to overwrite an existing seal."""
    root = _absolute_real_root(root)
    marker = root / PROFILE_NAME
    if marker.exists() or marker.is_symlink():
        raise OwnerRefused("managed runtime profile is already sealed; verify or recreate it")
    state = _profile_state(root, source=source, base=base, executable=executable)
    write_json_atomic(marker, state)
    profile_files(root, source=source, base=base, executable=executable)
    return marker


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("seal", "verify"))
    args = parser.parse_args(argv)
    if (sys.platform != "win32" or sys.implementation.name != "cpython"
            or sys.version_info[:2] != (3, 12) or sys.prefix == sys.base_prefix):
        parser.error("managed runtime profile requires a Windows CPython 3.12 venv")
    source = Path(__file__).resolve().parents[3]
    kwargs = {"source": source, "root": Path(sys.prefix), "base": Path(sys.base_prefix),
              "executable": Path(sys._base_executable)}
    try:
        if args.action == "seal":
            seal_installed_profile(**kwargs)
        else:
            profile_files(kwargs["root"], source=kwargs["source"], base=kwargs["base"], executable=kwargs["executable"])
    except OwnerRefused as exc:
        parser.error(str(exc))
    print(f"managed runtime profile {args.action}: {kwargs['root']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
