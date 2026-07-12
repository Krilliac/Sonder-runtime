"""Atomic live refresh for a long-running FastMCP tool registry.

FastMCP supports adding tools at runtime, and MCP supports
``notifications/tools/list_changed``. This wrapper combines those primitives
with a fail-closed source reload: a complete replacement registry is staged in
isolation and swapped only after the updated server module executes cleanly.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.tools import ToolManager
from mcp.server.lowlevel.server import NotificationOptions


def _enabled() -> bool:
    return os.environ.get("SONDER_LIVE_RELOAD", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _source_state(path: str | os.PathLike[str]) -> dict:
    source = Path(path).resolve()
    stat = source.stat()
    data = source.read_bytes()
    return {
        "path": str(source),
        "mtime_ns": int(stat.st_mtime_ns),
        "size": len(data),
        "digest": hashlib.sha256(data).hexdigest(),
        "source": data,
    }


def _manager_signature(manager: ToolManager) -> str:
    rows = []
    for tool in manager.list_tools():
        rows.append(
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
                "output_schema": tool.output_schema,
            }
        )
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ReloadableFastMCP(FastMCP):
    """FastMCP with atomic in-process source and tool-surface refresh."""

    def __init__(self, *args, **kwargs):
        self._reload_lock = threading.RLock()
        self._staging_manager: ToolManager | None = None
        self._staging_source_state: dict | None = None
        self._reload_module_name = ""
        self._reload_source_path = ""
        self._loaded_digest = ""
        self._loaded_mtime_ns = 0
        self._loaded_size = 0
        self._active_namespace: dict | None = None
        self._refresh_count = 0
        self._last_refresh_ts = 0
        self._last_surface_changed = False
        self._last_error = ""
        self._last_notification_error = ""
        super().__init__(*args, **kwargs)
        self._advertise_tool_list_changes()

    def _advertise_tool_list_changes(self) -> None:
        original = self._mcp_server.create_initialization_options

        def create_options(notification_options=None, experimental_capabilities=None):
            current = notification_options or NotificationOptions()
            options = NotificationOptions(
                prompts_changed=bool(current.prompts_changed),
                resources_changed=bool(current.resources_changed),
                tools_changed=True,
            )
            return original(options, experimental_capabilities)

        self._mcp_server.create_initialization_options = create_options

    def begin_module_refresh(self) -> None:
        """Start collecting decorators into an isolated replacement manager."""
        with self._reload_lock:
            if self._staging_manager is None:
                self._staging_manager = ToolManager(warn_on_duplicate_tools=False)

    def abort_module_refresh(self, error: Exception | str) -> None:
        """Discard an incomplete registry and preserve the last known-good one."""
        with self._reload_lock:
            self._staging_manager = None
            self._staging_source_state = None
            self._last_error = (
                error
                if isinstance(error, str)
                else "%s: %s" % (type(error).__name__, error)
            )

    def finish_module_refresh(
        self,
        module_name: str,
        source_path: str,
        namespace: dict | None = None,
    ) -> bool:
        """Atomically publish a staged registry and mark source as loaded."""
        with self._reload_lock:
            state = self._staging_source_state
            if state is None or state["path"] != str(Path(source_path).resolve()):
                # Read metadata before publishing the replacement manager so a
                # disappearing source cannot produce a half-committed refresh.
                state = _source_state(source_path)
            changed = False
            if self._staging_manager is not None:
                changed = _manager_signature(self._tool_manager) != _manager_signature(
                    self._staging_manager
                )
                self._tool_manager = self._staging_manager
                self._staging_manager = None
                # The low-level server separately caches MCP schemas for output
                # validation. Clear it so changed/removed tools cannot retain a
                # stale schema after the atomic manager swap.
                self._mcp_server._tool_cache.clear()
                self._refresh_count += 1
                self._last_refresh_ts = int(time.time())
            self._staging_source_state = None
            self._reload_module_name = str(module_name or "__main__")
            self._reload_source_path = state["path"]
            self._loaded_digest = state["digest"]
            self._loaded_mtime_ns = state["mtime_ns"]
            self._loaded_size = state["size"]
            self._active_namespace = namespace
            self._last_surface_changed = changed
            self._last_error = ""
            return changed

    def add_tool(self, fn, *args, **kwargs) -> None:
        with self._reload_lock:
            manager = self._staging_manager
            if manager is not None:
                manager.add_tool(fn, *args, **kwargs)
                return
        super().add_tool(fn, *args, **kwargs)

    def remove_tool(self, name: str) -> None:
        with self._reload_lock:
            manager = self._staging_manager
            if manager is not None:
                manager.remove_tool(name)
                return
        super().remove_tool(name)

    def _current_source_state(self) -> dict | None:
        if not self._reload_source_path:
            return None
        try:
            path = Path(self._reload_source_path)
            stat = path.stat()
            if (
                int(stat.st_mtime_ns) == self._loaded_mtime_ns
                and int(stat.st_size) == self._loaded_size
            ):
                return {
                    "path": str(path),
                    "mtime_ns": self._loaded_mtime_ns,
                    "size": self._loaded_size,
                    "digest": self._loaded_digest,
                    "source": None,
                }
            return _source_state(path)
        except OSError as exc:
            self._last_error = "%s: %s" % (type(exc).__name__, exc)
            return None

    def refresh_if_changed(self) -> dict:
        """Load changed source into a fresh namespace and swap on full success."""
        if not _enabled() or not self._reload_source_path:
            return {"reloaded": False, "surface_changed": False}
        current = self._current_source_state()
        if current is None or current["digest"] == self._loaded_digest:
            if current is not None:
                self._loaded_mtime_ns = current["mtime_ns"]
                self._loaded_size = current["size"]
            return {"reloaded": False, "surface_changed": False}
        with self._reload_lock:
            try:
                current = _source_state(self._reload_source_path)
            except OSError as exc:
                self.abort_module_refresh(exc)
                return {
                    "reloaded": False,
                    "surface_changed": False,
                    "error": self._last_error,
                }
            if current["digest"] == self._loaded_digest:
                self._loaded_mtime_ns = current["mtime_ns"]
                self._loaded_size = current["size"]
                return {"reloaded": False, "surface_changed": False}
            try:
                code = compile(
                    current["source"],
                    self._reload_source_path,
                    "exec",
                )
                namespace = {
                    "__name__": self._reload_module_name,
                    "__file__": self._reload_source_path,
                    "__package__": None,
                    "__builtins__": __builtins__,
                    "_PERSISTENT_MCP": self,
                    "_MCP_HOT_RELOAD_EXEC": True,
                }
                # Preserve the identity of the exact bytes being executed. If
                # an editor writes the file again during exec, the newer digest
                # remains visibly pending for the next request boundary.
                self._staging_source_state = current
                exec(code, namespace, namespace)
                if self._staging_manager is not None:
                    raise RuntimeError(
                        "server source did not finish the staged MCP registry"
                    )
                changed = bool(self._last_surface_changed)
                return {"reloaded": True, "surface_changed": changed}
            except Exception as exc:
                self.abort_module_refresh(exc)
                return {
                    "reloaded": False,
                    "surface_changed": False,
                    "error": self._last_error,
                }

    async def list_tools(self):
        self.refresh_if_changed()
        return await super().list_tools()

    async def call_tool(self, name: str, arguments: dict):
        refreshed = self.refresh_if_changed()
        if refreshed.get("surface_changed"):
            try:
                context = self.get_context()
                await context.request_context.session.send_tool_list_changed()
                self._last_notification_error = ""
            except Exception as exc:  # pragma: no cover - transport/client specific
                self._last_notification_error = "%s: %s" % (
                    type(exc).__name__,
                    exc,
                )
        return await super().call_tool(name, arguments)

    def runtime_snapshot(self) -> dict:
        current = self._current_source_state()
        current_digest = current["digest"] if current is not None else ""
        source_changed = bool(
            current_digest
            and self._loaded_digest
            and current_digest != self._loaded_digest
        )
        if self._last_error:
            status = "error"
        elif source_changed:
            status = "refresh pending"
        elif not _enabled():
            status = "disabled"
        else:
            status = "current"
        return {
            "status": status,
            "enabled": _enabled(),
            "module": self._reload_module_name,
            "path": self._reload_source_path,
            "loaded_digest": self._loaded_digest,
            "current_digest": current_digest,
            "source_changed": source_changed,
            "registered_tools": len(self._tool_manager.list_tools()),
            "refresh_count": self._refresh_count,
            "last_refresh_ts": self._last_refresh_ts,
            "last_surface_changed": self._last_surface_changed,
            "last_error": self._last_error,
            "last_notification_error": self._last_notification_error,
            "protocol_list_changed": True,
        }
