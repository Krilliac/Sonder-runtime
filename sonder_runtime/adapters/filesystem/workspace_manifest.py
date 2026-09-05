"""Bounded observed filesystem manifests; this is not an OS snapshot or lock."""

from dataclasses import dataclass, asdict
import hashlib
import json
import os
from pathlib import Path
import stat
import time


@dataclass(frozen=True)
class ManifestLimits:
    max_roots: int = 16
    max_entries: int = 10000
    max_file_bytes: int = 64 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024
    max_seconds: float = 30
    excluded_directories: tuple[str, ...] = (".git", "__pycache__", ".pytest_cache")

    def __post_init__(self):
        if any(
            isinstance(v, bool) or v <= 0
            for v in (
                self.max_roots,
                self.max_entries,
                self.max_file_bytes,
                self.max_total_bytes,
                self.max_seconds,
            )
        ):
            raise ValueError("manifest bounds must be positive")
        if any(
            not n or "/" in n or "\\" in n or n in {".", ".."}
            for n in self.excluded_directories
        ):
            raise ValueError("manifest exclusions must be directory names")


@dataclass(frozen=True)
class WorkspaceManifest:
    digest: str
    roots: tuple[str, ...]
    root_identities: tuple[tuple[int, int], ...]
    entries: tuple[tuple[str, str, int, str], ...]
    policy_json: str


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _regular_path(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("manifest refuses links or reparse points")
    return info


class WorkspaceSnapshotter:
    def __init__(self, limits=ManifestLimits()):
        self.limits = limits

    def capture(self, roots):
        roots = tuple(Path(p) for p in roots)
        if (
            not roots
            or len(roots) > self.limits.max_roots
            or len(set(roots)) != len(roots)
        ):
            raise ValueError("manifest root bound or identity invalid")
        deadline = time.monotonic() + self.limits.max_seconds
        first = self._pass(roots, deadline)
        second = self._pass(roots, deadline)
        if first != second:
            raise ValueError("workspace changed during manifest capture")
        return first

    def _pass(self, roots, deadline):
        entries, identities = [], []
        total = 0
        for root in roots:
            if not root.is_absolute() or root.resolve() != root:
                raise ValueError("manifest root alias is not canonical")
            for component in (root, *root.parents):
                _regular_path(component)
            info = _regular_path(root)
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("manifest root is not directory")
            identities.append((info.st_dev, info.st_ino))
            pending, seen, count = [(root, "")], set(), 0
            while pending:
                directory, relative = pending.pop()
                before = _identity(_regular_path(directory))
                with os.scandir(directory) as scan:
                    children = []
                    for entry in scan:
                        count += 1
                        if count > self.limits.max_entries:
                            raise ValueError("manifest entry bound exceeded")
                        children.append(entry.name)
                for name in sorted(children):
                    if time.monotonic() >= deadline:
                        raise ValueError("manifest time bound exceeded")
                    path = directory / name
                    rel = relative + name
                    key = rel.casefold()
                    if key in seen:
                        raise ValueError("ambiguous manifest path")
                    seen.add(key)
                    info = _regular_path(path)
                    if stat.S_ISDIR(info.st_mode):
                        if name not in self.limits.excluded_directories:
                            entries.append((str(root), rel + "/", 0, "directory"))
                            pending.append((path, rel + "/"))
                        continue
                    if not stat.S_ISREG(info.st_mode):
                        raise ValueError("manifest refuses nonregular file")
                    if info.st_size > self.limits.max_file_bytes:
                        raise ValueError("manifest file bound exceeded")
                    total += info.st_size
                    if total > self.limits.max_total_bytes:
                        raise ValueError("manifest total byte bound exceeded")
                    digest, length = hashlib.sha256(), 0
                    with path.open("rb") as handle:
                        if _identity(os.fstat(handle.fileno())) != _identity(info):
                            raise ValueError("workspace changed before file read")
                        while True:
                            data = handle.read(
                                min(
                                    1024 * 1024, self.limits.max_file_bytes - length + 1
                                )
                            )
                            if not data:
                                break
                            length += len(data)
                            if (
                                length > self.limits.max_file_bytes
                                or time.monotonic() >= deadline
                            ):
                                raise ValueError("manifest read bound exceeded")
                            digest.update(data)
                        if _identity(os.fstat(handle.fileno())) != _identity(info):
                            raise ValueError("workspace changed during file read")
                    if length != info.st_size or _identity(
                        _regular_path(path)
                    ) != _identity(info):
                        raise ValueError("workspace changed after file read")
                    entries.append((str(root), rel, length, digest.hexdigest()))
                if _identity(_regular_path(directory)) != before:
                    raise ValueError("workspace directory changed during enumeration")
        policy = json.dumps(asdict(self.limits), sort_keys=True, separators=(",", ":"))
        ordered = tuple(sorted(entries))
        payload = (tuple(str(p) for p in roots), identities, ordered, policy)
        digest = hashlib.sha256(
            json.dumps(payload, separators=(",", ":")).encode()
        ).hexdigest()
        return WorkspaceManifest(
            digest, tuple(str(p) for p in roots), tuple(identities), ordered, policy
        )
