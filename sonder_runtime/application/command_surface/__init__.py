"""Typed application command surfaces.

Command entrypoints depend on these narrow application contracts rather than
on the legacy composition root.  Concrete process/runtime adapters live in
the adapters layer.
"""

from .mcp import McpCommand, McpRuntime

__all__ = ["McpCommand", "McpRuntime"]
