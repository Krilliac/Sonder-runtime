"""Startup capabilities — parsed once at bootstrap, frozen forever after.

SPEC-5 §4: --unrestricted-tools and --unrestricted-selfmod are independent
boolean flags parsed at process start.  Nothing — HTTP, MCP, model output,
agent, automation, or runtime configuration — may toggle them afterward.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeCapabilities:
    unrestricted_tools: bool = False
    unrestricted_selfmod: bool = False

    @property
    def full_autonomy(self) -> bool:
        return self.unrestricted_tools and self.unrestricted_selfmod

    def status_dict(self) -> dict:
        return {
            "unrestricted_tools": self.unrestricted_tools,
            "unrestricted_selfmod": self.unrestricted_selfmod,
            "full_autonomy": self.full_autonomy,
        }


_FROZEN: RuntimeCapabilities | None = None


def freeze(caps: RuntimeCapabilities) -> RuntimeCapabilities:
    """Called exactly once by bootstrap.  Raises on a second call."""
    global _FROZEN
    if _FROZEN is not None:
        raise RuntimeError(
            "RuntimeCapabilities already frozen — capabilities are immutable "
            "after startup (SPEC-5 M22)"
        )
    _FROZEN = caps
    return _FROZEN


def current() -> RuntimeCapabilities:
    """Read the frozen capabilities.  Raises before freeze()."""
    if _FROZEN is None:
        raise RuntimeError("RuntimeCapabilities not yet frozen")
    return _FROZEN


def _reset_for_tests() -> None:
    global _FROZEN
    _FROZEN = None
