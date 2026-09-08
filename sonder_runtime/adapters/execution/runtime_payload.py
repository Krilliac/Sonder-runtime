"""Bounded private Python payload and explicit external dependency closure.

The supported threat boundary excludes changes by another trusted same-user
host administrator during execution. Hashes and directory anchors do not deny
such writes. Model-writable roots must be disjoint from every closure root.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import stat
import sys

from ...application.ports.runtime_owner import OwnerRefused, canonical
from ...application.compute_fabric.artifact_spool import PrivateDirectoryAnchor
from ..filesystem.atomic_json import write_json_atomic

MAX_FILES = 50000
MAX_BYTES = 4 * 1024**3
MAX_MANIFEST = 32 * 1024**2


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


def inventory(roots):
    rows, total = [], 0
    for root, exclude_site in roots:
        root = Path(root)
        for path in files(root, exclude_site=exclude_site):
            metadata = plain(path)
            total += metadata.st_size
            if len(rows) >= MAX_FILES or total > MAX_BYTES:
                raise OwnerRefused("runtime dependency closure exceeds bounds")
            digest = sha256()
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            after = plain(path)
            if (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise OwnerRefused("runtime artifact changed during inspection")
            rows.append([str(path), metadata.st_dev, metadata.st_ino, metadata.st_size, digest.hexdigest()])
    return rows


class RuntimePayload:
    def __init__(self, root, *, create=False, writable_roots=()):
        self.root = Path(root).absolute()
        self.path = self.root / "runtime-payload"
        self.anchor = None
        if create:
            self._create(tuple(writable_roots))
        else:
            try:
                self.anchor = PrivateDirectoryAnchor(self.path)
                with (self.root / "runtime-artifacts.json").open("rb") as stream:
                    raw = stream.read(MAX_MANIFEST + 1)
                if len(raw) > MAX_MANIFEST:
                    raise OwnerRefused("runtime artifact manifest exceeds bounds")
                self.manifest = json.loads(raw)
                if type(self.manifest) is not dict or set(self.manifest) != {"schema", "payload", "executable", "paths", "dll_paths", "source", "roots", "files", "python"}:
                    raise OwnerRefused("exact runtime artifact manifest required")
            except BaseException:
                self.close()
                raise
        self.digest = sha256(canonical(self.manifest)).hexdigest()

    def _create(self, writable_roots):
        if os.name != "nt" or sys.version_info[:2] != (3, 12):
            raise OwnerRefused("managed artifact profile requires Windows CPython 3.12")
        source = Path(__file__).resolve().parents[3]
        base = Path(sys.base_prefix).resolve()
        executable = Path(sys._base_executable).resolve()
        dependencies = Path(sys.prefix).resolve() / "Lib" / "site-packages"
        if executable.parent != base or any(base.glob("*._pth")):
            raise OwnerRefused("unknown Python runtime path configuration")
        external = [(str(base / "Lib"), True), (str(base / "DLLs"), False), (str(dependencies), False)]
        external += [(str(path), False) for path in sorted(base.iterdir()) if path.is_file() and path.suffix.lower() in (".dll", ".exe", ".zip")]
        if not any(Path(path) == executable for path, _ in external):
            raise OwnerRefused("interpreter is outside declared closure")
        disjoint((source, base, dependencies), writable_roots)
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
            roots = [(str(self.path), False), *external]
            self.manifest = dict(schema=1, payload=str(self.path), executable=str(executable),
                paths=[str(self.path), str(base / "Lib"), str(base / "DLLs"), str(dependencies)],
                dll_paths=[str(base), str(base / "DLLs")], source=str(source), roots=roots,
                files=inventory(roots), python=[3, 12])
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
        if value["schema"] != 1 or value["python"] != [3, 12] or value["payload"] != str(self.path):
            raise OwnerRefused("runtime artifact profile changed")
        disjoint((value["source"], *value["paths"], *value["dll_paths"], value["executable"]), tuple(writable_roots))
        if inventory(value["roots"]) != value["files"]:
            raise OwnerRefused("runtime artifact content or identity changed")

    def close(self):
        if self.anchor is not None:
            self.anchor.close()
