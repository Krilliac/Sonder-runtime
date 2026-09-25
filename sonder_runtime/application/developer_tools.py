"""The developer-tool services one runtime composes: host tool inventory,
structured test runs and the output digest.

The three services are owned by their own packages; this aggregate is what
the typed tools, the native MCP surface, the REPL and the HTTP facade reach
through ``Application.developer_tools``. It is ``None`` when the runtime did
not compose them, and every surface reports that instead of failing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .testing.service import TestRunService

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from .diagnostics.service import OutputDigestService
    from .host_tools.service import HostToolInventoryService


@dataclass(frozen=True)
class DeveloperToolServices:
    inventory: "HostToolInventoryService | Any"
    test_runs: TestRunService
    digest: "OutputDigestService | Any"


__all__ = ["DeveloperToolServices"]
