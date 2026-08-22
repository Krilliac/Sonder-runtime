"""Provider-neutral port for bounded archive creation.

The port describes the capability without importing the root-owned
implementation.  Limit normalization and filesystem safety remain owned by
the native implementation and must not be reimplemented by adapters.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ArchiveCreateRequest:
    """One archive creation request crossing the application boundary."""

    root: str
    inputs_json: Any
    destination: str
    archive_format: str = "zip"
    deterministic: bool = True
    max_files: int | None = None
    max_entries: int | None = None
    max_file_bytes: int | None = None
    max_total_bytes: int | None = None
    max_depth: int | None = None
    max_results: int | None = None
    extra_roots: str = ""
    developer_authorized: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.root, str) or not self.root.strip():
            raise ValueError("root must be a non-empty path")
        if not isinstance(self.destination, str) or not self.destination.strip():
            raise ValueError("destination must be a non-empty path")
        if not isinstance(self.archive_format, str):
            raise TypeError("archive_format must be a string")
        if self.archive_format.casefold() not in {"zip", "tar"}:
            raise ValueError("archive_format must be zip or tar")
        if type(self.deterministic) is not bool:
            raise TypeError("deterministic must be bool")
        if not isinstance(self.extra_roots, str):
            raise TypeError("extra_roots must be a string")
        if type(self.developer_authorized) is not bool:
            raise TypeError("developer_authorized must be bool")
        for name in (
            "max_files", "max_entries", "max_file_bytes", "max_total_bytes",
            "max_depth", "max_results",
        ):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(f"{name} must be a positive integer or None")


class ArchiveCreateGateway(Protocol):
    """Create a bounded archive while retaining native safety enforcement."""

    def create_archive(self, request: ArchiveCreateRequest) -> dict: ...


__all__ = ["ArchiveCreateGateway", "ArchiveCreateRequest"]
