"""Ports for bounded grounded execution at an interface boundary."""
from __future__ import annotations

from typing import Any, Protocol


class GroundingProvider(Protocol):
    def __getattr__(self, name: str) -> Any: ...


class CodeRunnerProvider(Protocol):
    def __getattr__(self, name: str) -> Any: ...


__all__ = ["CodeRunnerProvider", "GroundingProvider"]
