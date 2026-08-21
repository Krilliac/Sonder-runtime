"""Port marker for optional REPL helper services."""
from __future__ import annotations

from typing import Any, Protocol


class ReplService(Protocol):
    def __getattr__(self, name: str) -> Any: ...


__all__ = ["ReplService"]
