"""Composition for the host developer-tool inventory.

Nothing is discovered or launched here: the service discovers lazily on the
first snapshot request, and host probes (environment, PATH) are rebuilt for
every discovery so a refresh sees the current PATH.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

import sonder_runtime.adapters.host_tools.guards as guards
from ..adapters.host_tools.discovery import HostToolDiscovery
from ..adapters.host_tools.probes import HostProbes, default_host_probes
from ..adapters.host_tools.snapshot_store import JsonSnapshotStore, default_snapshot_path
from ..application.host_tools.service import DEFAULT_TTL_SECONDS, HostToolInventoryService
from ..domain.host_tools.model import InventorySnapshot
import sonder_runtime.platform.environment_probe as environment_probe
from ..platform.config import SonderConfig
from ..platform.logging import Redactor

INVENTORY_TTL_SECONDS = DEFAULT_TTL_SECONDS  # 24 hours


class _LazyDiscovery:
    """Build host probes at discovery time, never at composition time."""

    def __init__(self, probes: HostProbes | None) -> None:
        self._probes = probes

    def discover(self, *, previous: InventorySnapshot | None, full: bool) -> InventorySnapshot:
        probes = self._probes if self._probes is not None else default_host_probes()
        return HostToolDiscovery(probes).discover(previous=previous, full=full)


class _LazyStore:
    """Resolve the default state-home path on first use."""

    def __init__(self, path: str | None) -> None:
        self._path = path
        self._store: JsonSnapshotStore | None = None

    def _resolve(self) -> JsonSnapshotStore:
        if self._store is None:
            self._store = JsonSnapshotStore(self._path or default_snapshot_path())
        return self._store

    def load(self) -> InventorySnapshot | None:
        try:
            store = self._resolve()
        except Exception:
            return None
        return store.load()

    def save(self, snapshot: InventorySnapshot) -> None:
        self._resolve().save(snapshot)


def _display_redactor(redactor: Redactor | None) -> Callable[[str], str]:
    state: dict[str, Callable[[str], str]] = {}

    def redact(text: str) -> str:
        path_redact = state.get("path")
        if path_redact is None:
            path_redact = state["path"] = guards.display_redactor()
        value = path_redact(text)
        return redactor.redact(value) if redactor is not None else value

    return redact


def compose_host_tool_inventory(
    config: SonderConfig | None,
    *,
    redactor: Redactor | None = None,
    probes: HostProbes | None = None,
    snapshot_path: str | None = None,
) -> HostToolInventoryService:
    """Compose the lazily discovering, cached host tool inventory service."""
    del config  # No inventory configuration exists yet; the TTL is fixed.
    return HostToolInventoryService(
        _LazyDiscovery(probes),
        _LazyStore(snapshot_path),
        clock=time.time,
        ttl_seconds=INVENTORY_TTL_SECONDS,
        redact_path=_display_redactor(redactor),
        executable_guard=guards.executable_allowed,
    )


_INSTALL_LOCK = threading.Lock()
_INSTALLED: HostToolInventoryService | None = None


def install_agent_brief_summary(service: HostToolInventoryService) -> None:
    """Make ``environment_probe.agent_brief()`` append the capability summary.

    Idempotent; installing a different service replaces the previous one.
    The provider reads the cached snapshot only and never discovers.
    """
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED is service:
            return
        _INSTALLED = service
        environment_probe.set_capability_summary_provider(
            lambda: service.capability_summary(max_chars=480)
        )


def uninstall_agent_brief_summary() -> None:
    global _INSTALLED
    with _INSTALL_LOCK:
        _INSTALLED = None
        environment_probe.set_capability_summary_provider(None)


__all__ = [
    "INVENTORY_TTL_SECONDS",
    "compose_host_tool_inventory",
    "install_agent_brief_summary",
    "uninstall_agent_brief_summary",
]
