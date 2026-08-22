"""Bounded discovery of project and personal instruction files."""

from .registry import (
    InstructionRecord,
    InstructionSource,
    InstructionRegistry,
    InstructionDiscoveryError,
)

__all__ = [
    "InstructionRecord",
    "InstructionSource",
    "InstructionRegistry",
    "InstructionDiscoveryError",
]
