"""Fail-closed filesystem race-resistance contracts.

This module is deliberately small and side-effect free.  It does not turn a
checked pathname into a safe file operation: callers receive an open intent
that must be executed by a platform adapter using directory handles and
no-follow flags.  When the running platform cannot make that guarantee, a
destructive intent is rejected rather than downgraded to a pathname check.
"""

from __future__ import annotations

import errno
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

from .path_archive_safety import UnsafePathError


class RaceResistanceError(UnsafePathError):
    """Raised when a path or operation cannot be proven race resistant."""


class PlatformCapabilityError(RaceResistanceError):
    """Raised when the host cannot provide the requested filesystem guarantee."""


Operation = Literal["read", "create", "replace", "delete"]


@dataclass(frozen=True)
class PlatformPathCapabilities:
    """Facts used to decide whether an operation may cross the boundary."""

    platform: str
    directory_handles: bool
    no_follow: bool
    reparse_detection: bool
    race_resistant_destructive_ops: bool
    reason: str

    @property
    def fail_closed(self) -> bool:
        return not self.race_resistant_destructive_ops


def platform_path_capabilities() -> PlatformPathCapabilities:
    """Report only capabilities available from the current Python runtime.

    POSIX support requires both ``openat``-style directory descriptors and
    ``O_NOFOLLOW``.  Windows is reported as unsupported here: reparse-point
    safe opens require a native handle adapter with ``CreateFileW`` flags, and
    silently using ordinary ``open`` would create a check/use race.
    """

    platform = os.name
    directory_handles = hasattr(os, "open") and hasattr(os, "supports_dir_fd") and os.open in os.supports_dir_fd
    no_follow = hasattr(os, "O_NOFOLLOW")
    reparse_detection = platform == "nt" or hasattr(os, "lstat")
    destructive = platform == "posix" and directory_handles and no_follow
    if destructive:
        reason = "directory-descriptor and O_NOFOLLOW primitives are available"
    elif platform == "nt":
        reason = "native reparse-safe handle adapter is required"
    else:
        reason = "required directory-descriptor and no-follow primitives are unavailable"
    return PlatformPathCapabilities(
        platform=platform,
        directory_handles=directory_handles,
        no_follow=no_follow,
        reparse_detection=reparse_detection,
        race_resistant_destructive_ops=destructive,
        reason=reason,
    )


@dataclass(frozen=True)
class AuthorizedResolution:
    root: Path
    path: Path
    relative_parts: tuple[str, ...]
    capabilities: PlatformPathCapabilities


@dataclass(frozen=True)
class OpenIntent:
    resolution: AuthorizedResolution
    operation: Operation
    flags: int
    no_follow: bool
    directory_handle_required: bool
    destructive: bool


@dataclass(frozen=True)
class DestructiveTargetLimits:
    max_targets: int = 128
    max_path_length: int = 4_096
    max_depth: int = 64
    allow_root: bool = False
    require_existing: bool = True

    def __post_init__(self) -> None:
        if min(self.max_targets, self.max_path_length, self.max_depth) < 1:
            raise ValueError("destructive-target limits must be positive")


@dataclass(frozen=True)
class DestructiveTarget:
    path: Path
    root: Path
    relative_parts: tuple[str, ...]


def _reparse_or_symlink(path: Path) -> bool:
    try:
        info = path.lstat()
    except (OSError, ValueError) as exc:
        raise RaceResistanceError("path component could not be inspected") from exc
    if stat.S_ISLNK(info.st_mode):
        return True
    # FILE_ATTRIBUTE_REPARSE_POINT.  ``st_file_attributes`` is exposed by
    # Windows stat results; using getattr keeps this code portable and makes
    # the check explicit instead of trusting Path.resolve on Windows.
    return bool(getattr(info, "st_file_attributes", 0) & 0x0400)


def _raw_components_are_safe(raw: Path) -> None:
    if not raw.is_absolute() or len(str(raw)) > 32_768:
        raise RaceResistanceError("path must be an absolute, bounded path")
    current = Path(raw.anchor)
    for component in raw.parts[1:]:
        current /= component
        if os.path.lexists(str(current)) and _reparse_or_symlink(current):
            raise RaceResistanceError("symlink or reparse-point component")


def resolve_authorized_root(
    path: str | os.PathLike[str],
    roots: Iterable[str | os.PathLike[str]],
) -> AuthorizedResolution:
    """Resolve ``path`` under a non-link authorized root, twice.

    The repeated resolution catches ordinary replacement during validation;
    the open intent still requires a platform adapter to perform the final
    no-follow, directory-handle operation.
    """

    raw = Path(path)
    _raw_components_are_safe(raw)
    root_candidates = [Path(root) for root in roots]
    if not root_candidates:
        raise RaceResistanceError("at least one authorized root is required")
    for root_raw in root_candidates:
        if not root_raw.is_absolute() or not root_raw.exists():
            continue
        try:
            if _reparse_or_symlink(root_raw):
                continue
            root = root_raw.resolve(strict=True)
            first = raw.resolve(strict=False)
            second = raw.resolve(strict=False)
            if first != second:
                raise RaceResistanceError("path changed during validation")
            first.relative_to(root)
            parts = first.relative_to(root).parts
            return AuthorizedResolution(root, first, tuple(parts), platform_path_capabilities())
        except (OSError, RuntimeError, ValueError):
            continue
    raise RaceResistanceError("path is outside authorized roots or root is unsafe")


def build_open_intent(
    path: str | os.PathLike[str],
    roots: Iterable[str | os.PathLike[str]],
    operation: Operation = "read",
) -> OpenIntent:
    """Create a bounded, explicit open intent without opening or mutating files."""

    if operation not in {"read", "create", "replace", "delete"}:
        raise ValueError("unsupported filesystem operation")
    resolution = resolve_authorized_root(path, roots)
    destructive = operation != "read"
    capabilities = resolution.capabilities
    if destructive and not capabilities.race_resistant_destructive_ops:
        raise PlatformCapabilityError(
            f"{operation} requires race-resistant platform support: {capabilities.reason}"
        )
    flags = os.O_RDONLY if operation == "read" else os.O_WRONLY
    if operation in {"create", "replace"}:
        flags |= os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return OpenIntent(
        resolution=resolution,
        operation=operation,
        flags=flags,
        no_follow=bool(flags & getattr(os, "O_NOFOLLOW", 0)),
        directory_handle_required=destructive,
        destructive=destructive,
    )


def check_destructive_targets(
    targets: Iterable[str | os.PathLike[str]],
    roots: Iterable[str | os.PathLike[str]],
    limits: DestructiveTargetLimits = DestructiveTargetLimits(),
) -> tuple[DestructiveTarget, ...]:
    """Validate a bounded set of destructive targets and reject duplicates."""

    checked: list[DestructiveTarget] = []
    seen: set[tuple[str, str]] = set()
    for index, target in enumerate(targets, 1):
        if index > limits.max_targets:
            raise RaceResistanceError("destructive target count exceeds bound")
        raw = Path(target)
        if len(str(raw)) > limits.max_path_length:
            raise RaceResistanceError("destructive target path exceeds bound")
        resolution = resolve_authorized_root(raw, roots)
        if len(resolution.relative_parts) > limits.max_depth:
            raise RaceResistanceError("destructive target depth exceeds bound")
        if not resolution.relative_parts and not limits.allow_root:
            raise RaceResistanceError("destructive root deletion is not permitted")
        if limits.require_existing and not resolution.path.exists():
            raise RaceResistanceError("destructive target does not exist")
        key = (str(resolution.root), str(resolution.path))
        if key in seen:
            raise RaceResistanceError("duplicate destructive target")
        seen.add(key)
        checked.append(DestructiveTarget(resolution.path, resolution.root, resolution.relative_parts))
    if not checked:
        raise RaceResistanceError("at least one destructive target is required")
    # Capability is checked once for the batch, before any caller can act.
    capabilities = platform_path_capabilities()
    if not capabilities.race_resistant_destructive_ops:
        raise PlatformCapabilityError(
            f"destructive targets require race-resistant platform support: {capabilities.reason}"
        )
    return tuple(checked)
