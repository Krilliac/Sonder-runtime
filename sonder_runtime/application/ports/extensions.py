"""Persistence port for durable extension installation state."""
from __future__ import annotations

from typing import Protocol, Sequence

from ..extensions.registry import ExtensionInstallRecord


class ExtensionStateRepository(Protocol):
    """Store the last validated registry snapshot; never executes extensions."""

    def load(self) -> Sequence[ExtensionInstallRecord]: ...
    def save(self, records: Sequence[ExtensionInstallRecord]) -> None: ...


__all__ = ["ExtensionStateRepository"]
