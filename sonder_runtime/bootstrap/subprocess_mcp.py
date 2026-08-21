"""Typed composition for bounded, one-shot MCP subprocess providers."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from ..adapters.mcp_subprocess import (
    LifecycleObserver,
    McpProviderCancelled,
    McpProviderLifecycleEvent,
    McpSubprocessProvider,
)
from ..interfaces.mcp.transport import BoundedMcpProviderExchange, McpTransportLimits
from ..application.protocol.mcp_compatibility import LegacyMcpContract
from ..application.jobs.durable_registry import ProcessTreeCleanupContract


@dataclass(frozen=True)
class McpSubprocessProviderConfig:
    """Explicit, non-secret provider launch settings owned by composition."""

    argv: tuple[str, ...]
    cwd: str | Path | None = None
    env: Mapping[str, str] | None = None
    timeout_seconds: float = 30.0
    shutdown_timeout_seconds: float = 2.0
    declaration: LegacyMcpContract | None = None
    cleanup: ProcessTreeCleanupContract | None = None

    def __post_init__(self) -> None:
        if not self.argv or any(not isinstance(item, str) or not item for item in self.argv):
            raise ValueError("MCP provider argv must be non-empty strings")
        if self.env is not None and any(
            not isinstance(key, str) or not key or not isinstance(value, str)
            for key, value in self.env.items()
        ):
            raise ValueError("MCP provider environment must contain non-empty string pairs")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or self.timeout_seconds <= 0
            or isinstance(self.shutdown_timeout_seconds, bool)
            or not isinstance(self.shutdown_timeout_seconds, (int, float))
            or self.shutdown_timeout_seconds <= 0
        ):
            raise ValueError("MCP provider timeouts must be positive numbers")


def build_mcp_subprocess_exchange(
    config: McpSubprocessProviderConfig,
    *,
    limits: McpTransportLimits | None = None,
    observer: LifecycleObserver | None = None,
    popen=None,
) -> BoundedMcpProviderExchange:
    """Compose a bounded exchange without exposing process details to callers.

    ``popen`` is an explicit test seam only; normal composition uses the
    provider's production process launcher.  Credentials remain in the
    injected environment and are never copied into diagnostics or events.
    """
    provider_kwargs = {
        "cwd": config.cwd,
        "env": config.env,
        "timeout_seconds": config.timeout_seconds,
        "shutdown_timeout_seconds": config.shutdown_timeout_seconds,
        "declaration": config.declaration,
        "cleanup": config.cleanup,
        "observer": observer,
    }
    if popen is not None:
        provider_kwargs["popen"] = popen
    provider = McpSubprocessProvider(config.argv, **provider_kwargs)
    return BoundedMcpProviderExchange(provider, limits=limits)


__all__ = [
    "McpProviderLifecycleEvent",
    "McpProviderCancelled",
    "McpSubprocessProviderConfig",
    "build_mcp_subprocess_exchange",
]
