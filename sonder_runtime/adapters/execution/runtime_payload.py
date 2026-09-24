"""Bounded private Python payload and explicit external dependency closure.

The supported threat boundary excludes changes by another trusted same-user
host administrator during execution. Hashes and directory anchors do not deny
such writes. Model-writable roots must be disjoint from every closure root.
"""
import json
import os
import shutil
import stat
import sys
from hashlib import sha256
from pathlib import Path

from ...application.compute_fabric.artifact_spool import PrivateDirectoryAnchor
from ...application.ports.runtime_owner import OwnerRefused, canonical
from ..filesystem.atomic_json import write_json_atomic

# The existing supported size/count envelope remains unchanged. Preflight
# refuses out-of-bounds closures before reading their contents. Each admission
# still hashes all bytes, including once in the child; metadata cannot prove
# content integrity. A host can select the installer-owned lean runtime venv.
MAX_FILES = 50000
MAX_BYTES = 4 * 1024**3
MAX_MANIFEST = 32 * 1024**2
MAX_PTH_BYTES = 1024 * 1024
MAX_PTH_ENTRIES = 256


def disjoint(paths, writable_roots):
    for path in paths:
        for private in (Path(path).absolute(), Path(path).resolve()):
            for root in writable_roots:
                for writable in (Path(root).absolute(), Path(root).resolve()):
                    if private == writable or private.is_relative_to(writable) or writable.is_relative_to(private):
                        raise OwnerRefused("runtime artifact overlaps model-writable roots")


def plain(path):
    metadata = path.lstat()
    if path.is_symlink() or getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise OwnerRefused("runtime artifact contains a reparse path")
    return metadata


def files(root, *, exclude_site=False):
    plain(root)
    if root.is_file():
        yield root
        return
    for current, directories, names in os.walk(root, followlinks=False):
        current = Path(current)
        for name in tuple(directories):
            plain(current / name)
        directories[:] = sorted(name for name in directories if name != "__pycache__" and not (exclude_site and current == root and name == "site-packages"))
        for name in sorted(names):
            path = current / name
            if not stat.S_ISREG(plain(path).st_mode):
                raise OwnerRefused("runtime artifact is not an ordinary file")
            yield path


def _declared_site_package_paths(site_packages):
    """Return site paths from data-only entries in the venv's ``.pth`` files.

    CPython's normal site initialization executes import statements found in
    ``.pth`` files.  A managed child deliberately runs with ``-S`` so that
    executable path-file content cannot extend its dependency closure.  Keep
    only relative, existing entries beneath the declared site-packages root;
    executable lines and entries outside that root are never admitted.
    """
    root = Path(site_packages).resolve()
    plain(root)
    if not root.is_dir():
        raise OwnerRefused("declared site-packages root is not a directory")
    paths = [root]
    seen = {os.path.normcase(str(root))}
    entry_count = 0
    for path_file in sorted(root.glob("*.pth")):
        metadata = plain(path_file)
        if not stat.S_ISREG(metadata.st_mode):
            raise OwnerRefused("dependency path file is not an ordinary file")
        if metadata.st_size > MAX_PTH_BYTES:
            raise OwnerRefused("dependency path file exceeds bounds")
        try:
            lines = path_file.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise OwnerRefused("dependency path file is unreadable") from exc
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith(("#", "import ", "import\t")):
                continue
            entry_count += 1
            if entry_count > MAX_PTH_ENTRIES:
                raise OwnerRefused("dependency path entries exceed bounds")
            candidate = Path(line.replace("\\", os.sep))
            if candidate.is_absolute() or candidate.drive or candidate.root:
                raise OwnerRefused("dependency path escapes site-packages")
            try:
                resolved = (root / candidate).resolve()
                resolved.relative_to(root)
            except (OSError, RuntimeError, ValueError) as exc:
                raise OwnerRefused("dependency path escapes site-packages") from exc
            if not resolved.exists():
                continue
            if not (resolved.is_dir() or resolved.is_file()):
                raise OwnerRefused("dependency path is not an ordinary file or directory")
            key = os.path.normcase(str(resolved))
            if key not in seen:
                seen.add(key)
                paths.append(resolved)
    return tuple(paths)


def preflight(roots):
    """Reject an oversized closure before reading any dependency content.

    The returned metadata is used only to detect changes during the ensuing
    digest pass. Every validation still hashes every file's bytes afresh.
    """
    planned, total = [], 0
    for root, exclude_site in roots:
        root = Path(root)
        for path in files(root, exclude_site=exclude_site):
            metadata = plain(path)
            if not stat.S_ISREG(metadata.st_mode):
                raise OwnerRefused("runtime artifact is not an ordinary file")
            total += metadata.st_size
            if len(planned) >= MAX_FILES or total > MAX_BYTES:
                raise OwnerRefused(
                    "runtime dependency closure exceeds verification budget "
                    f"({MAX_FILES} files / {MAX_BYTES // 1024**2} MiB); "
                    "use a dedicated runtime environment with requirements-runtime.txt"
                )
            planned.append((path, metadata))
    return planned


def inventory(roots):
    planned = preflight(roots)
    rows = []
    for path, metadata in planned:
        before = plain(path)
        if (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns) != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
            raise OwnerRefused("runtime artifact changed during inspection")
        digest = sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        after = plain(path)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise OwnerRefused("runtime artifact changed during inspection")
        rows.append([str(path), before.st_dev, before.st_ino, before.st_size, digest.hexdigest()])
    return rows


def _runtime_layout(runtime_venv=None):
    """Base interpreter and selected dependency closure (private test seam)."""
    source = Path(__file__).resolve().parents[3]
    base = Path(sys.base_prefix).resolve()
    executable = Path(sys._base_executable).resolve()
    if runtime_venv is None:
        return (source, base, executable,
                Path(sys.prefix).resolve() / "Lib" / "site-packages", ())
    from .runtime_profile import profile_files

    profile = profile_files(runtime_venv, source=source, base=base, executable=executable)
    return (source, base, executable,
            Path(runtime_venv) / "Lib" / "site-packages", profile)


def base_runtime_files(base):
    """Inventory base binaries, omitting only known aliases of inventoried files.

    setup-python's Windows CPython exposes python3.exe as a reparse alias of
    python.exe. The owner launches the ordinary interpreter directly; hashing
    the target covers those bytes. An unrelated alias must not widen the
    trusted runtime closure or silently escape it.
    """
    base = Path(base)
    result = []
    aliases = {"python3.exe": "python.exe", "python3w.exe": "pythonw.exe"}
    for path in sorted(base.iterdir()):
        if path.suffix.lower() not in (".dll", ".exe", ".zip"):
            continue
        info = path.lstat()
        if path.is_symlink() or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            target_name = aliases.get(path.name.lower())
            target = base / target_name if target_name else None
            try:
                recognized = (target is not None
                              and path.resolve(strict=True) == target.resolve(strict=True))
            except (OSError, RuntimeError):
                recognized = False
            if not recognized:
                raise OwnerRefused("runtime artifact contains an unrecognized reparse path")
            if not stat.S_ISREG(plain(target).st_mode):
                raise OwnerRefused("runtime interpreter alias target is not an ordinary file")
            continue
        if stat.S_ISREG(info.st_mode):
            result.append(path)
        else:
            raise OwnerRefused("runtime base binary is not an ordinary file")
    return tuple(result)


class RuntimePayload:
    def __init__(self, root, *, create=False, writable_roots=(), runtime_venv=None):
        self.root = Path(root).absolute()
        self.path = self.root / "runtime-payload"
        self.anchor = None
        if create:
            self._create(tuple(writable_roots), runtime_venv=runtime_venv)
        else:
            try:
                self.anchor = PrivateDirectoryAnchor(self.path)
                with (self.root / "runtime-artifacts.json").open("rb") as stream:
                    raw = stream.read(MAX_MANIFEST + 1)
                if len(raw) > MAX_MANIFEST:
                    raise OwnerRefused("runtime artifact manifest exceeds bounds")
                self.manifest = json.loads(raw)
                if type(self.manifest) is not dict or set(self.manifest) != {"schema", "payload", "executable", "paths", "dll_paths", "source", "roots", "files", "python", "profile"}:
                    raise OwnerRefused("exact runtime artifact manifest required")
            except BaseException:
                self.close()
                raise
        self.digest = sha256(canonical(self.manifest)).hexdigest()

    def _create(self, writable_roots, *, runtime_venv=None):
        if os.name != "nt" or sys.version_info[:2] != (3, 12):
            raise OwnerRefused("managed artifact profile requires Windows CPython 3.12")
        source, base, executable, dependencies, profile_roots = _runtime_layout(runtime_venv)
        if executable.parent != base or any(base.glob("*._pth")):
            raise OwnerRefused("unknown Python runtime path configuration")
        site_paths = _declared_site_package_paths(dependencies)
        system_dll = dependencies / "pywin32_system32"
        dll_paths = [str(base), str(base / "DLLs")]
        if system_dll.exists():
            plain(system_dll)
            if not system_dll.is_dir():
                raise OwnerRefused("pywin32 system directory is not a directory")
            dll_paths.append(str(system_dll.resolve()))
        external = [(str(base / "Lib"), True), (str(base / "DLLs"), False), (str(dependencies), False)]
        external += [(str(path), False) for path in base_runtime_files(base)]
        if not any(Path(path) == executable for path, _ in external):
            raise OwnerRefused("interpreter is outside declared closure")
        disjoint((source, base, dependencies, *((runtime_venv,) if runtime_venv else ())), writable_roots)
        # In particular, detect training stacks that would exceed the
        # supported closure before copying the private application payload.
        preflight(external)
        self.anchor = PrivateDirectoryAnchor.open_base(self.path, require_new=True)
        try:
            sources = [source / "sonder_runtime", source / "migrations", source / "seed"]
            sources += sorted(path for path in source.iterdir() if path.is_file() and path.suffix in (".py", ".toml"))
            copied, total = 0, 0
            for item in sources:
                if not item.exists():
                    raise OwnerRefused("declared Sonder package data is missing")
                for original in files(item):
                    copied += 1
                    total += original.stat().st_size
                    if copied > 10000 or total > 256 * 1024**2:
                        raise OwnerRefused("Sonder payload exceeds bounds")
                    destination = self.path / original.relative_to(source)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(original, destination)
            (self.root / "python-cache").mkdir()
            roots = [(str(self.path), False), *external,
                     *((path, False) for path in profile_roots)]
            self.manifest = {
                "schema": 2, "payload": str(self.path), "executable": str(executable),
                "paths": [str(self.path), str(base / "Lib"), str(base / "DLLs"),
                          *(str(path) for path in site_paths)],
                "dll_paths": dll_paths, "source": str(source), "roots": roots,
                "files": inventory(roots), "python": [3, 12],
                "profile": str(runtime_venv) if runtime_venv else "",
            }
            if len(canonical(self.manifest)) > MAX_MANIFEST:
                raise OwnerRefused("runtime artifact manifest exceeds bounds")
            write_json_atomic(self.root / "runtime-artifacts.json", self.manifest)
        except BaseException:
            self.close()
            raise

    def validate(self, writable_roots, *, expected=None):
        self.anchor.validate()
        value = self.manifest
        if expected is not None and self.digest != expected:
            raise OwnerRefused("runtime artifact digest changed")
        if value["schema"] != 2 or value["python"] != [3, 12] or value["payload"] != str(self.path):
            raise OwnerRefused("runtime artifact profile changed")
        disjoint((value["source"], *value["paths"], *value["dll_paths"], value["executable"],
                  *((value["profile"],) if value["profile"] else ())), tuple(writable_roots))
        if inventory(value["roots"]) != value["files"]:
            raise OwnerRefused("runtime artifact content or identity changed")

    def close(self):
        if self.anchor is not None:
            self.anchor.close()
