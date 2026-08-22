"""Application orchestration for the MCP command.

The command has deliberately small dependencies: the startup safety fence and
the already-fenced runtime are supplied by a typed port.  This keeps the
application layer independent of the historical ``server`` module while
preserving the entrypoint's ordering guarantees.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol


class McpRuntime(Protocol):
    """Runtime hooks required by the MCP command surface."""

    def require_startup_safety(self) -> None:
        """Apply the process-level safety fence."""

    def run(self, *, safety_checked: bool) -> None:
        """Run the MCP transport after the safety fence succeeds."""


@dataclass(frozen=True)
class McpCommand:
    """Coordinate MCP startup without knowing its concrete runtime."""

    runtime: McpRuntime

    def execute(self, configure: Callable[[], None]) -> None:
        """Fence startup, apply configuration, then run the MCP runtime.

        ``configure`` is intentionally called between the two runtime hooks.
        In particular, a configuration failure cannot start the adapter, and
        the safety fence remains ahead of configuration reads as required by
        the legacy command contract.
        """
        self.runtime.require_startup_safety()
        configure()
        self.runtime.run(safety_checked=True)


__all__ = ["McpCommand", "McpRuntime"]
