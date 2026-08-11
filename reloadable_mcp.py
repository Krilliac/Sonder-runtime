"""Atomic live refresh for a long-running FastMCP tool registry.

FastMCP supports adding tools at runtime, and MCP supports
``notifications/tools/list_changed``. This wrapper combines those primitives
with a fail-closed source reload: a complete replacement registry is staged in
isolation and swapped only after the updated server module executes cleanly.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.resources import FunctionResource, ResourceManager
from mcp.server.fastmcp.prompts import PromptManager
from mcp.server.fastmcp.tools import ToolManager
from mcp.server.fastmcp.utilities.context_injection import find_context_parameter
from mcp.server.lowlevel.server import NotificationOptions


def _refuse_if_gated(name: str) -> None:
    """Apply the operator's permission gate to a direct MCP tool call.

    This is the only place an MCP *client* enters; every internal Python call
    to the same function bypasses it, which is exactly the split we want --
    ``_agent_dispatch`` and the REPL each gate with their own ``interactive``
    value, and gating the function bodies instead would double-prompt them.

    ``interactive=False``: nobody is at a keyboard behind a protocol call, so
    ``ask`` degrades to ``allow`` and the default ``manual`` mode refuses
    nothing a client could do yesterday. What this does add is the mode that
    exists to hold still: ``plan`` denies here too. Without this, ``plan``
    advertised "reads only - no writes, no commands" while a client could call
    ``file_write`` straight through -- an operator who selects that mode and
    then watches their workspace change has been lied to by the indicator.
    An explicit per-tool ``deny`` rule refuses here as well.

    ``GATE_CONTROL_TOOLS`` is exempt, because the refusal below names
    ``permission_mode`` as the remedy and ``plan`` would otherwise refuse that
    tool too -- leaving a client that selected ``plan`` no way to select
    anything else, across restarts, since the mode persists to disk.

    Imported lazily: ``permission_modes`` resolves the command catalog, which
    imports ``server``, which imports this module.
    """
    import permission_modes

    tool = str(name or "")
    decision = permission_modes.decide_for_caller(
        tool, interactive=False, gate_control_exempt=True,
    )
    if decision is None or decision.allowed:
        return
    raise ToolError(
        "%s is refused by the active permission gate: %s (mode=%s, risk=%s). "
        "Change the mode with the permission_mode tool, or write a rule with "
        "permission_rule_set." % (
            name, decision.reason, decision.mode, decision.risk,
        )
    )


def _recovery_action(configured_ready: bool) -> str:
    if configured_ready:
        return (
            "Restart/reconnect the MCP process with working directory "
            "SONDER_RUNTIME_ROOT and command: python -m sonder_runtime mcp."
        )
    return (
        "Set SONDER_RUNTIME_ROOT to an existing canonical checkout, then "
        "restart/reconnect the MCP process there with: "
        "python -m sonder_runtime mcp."
    )


def _provenance_error(issue: str) -> str:
    return {
        "stale_source_root": "stale runtime source: loaded MCP file is unavailable",
        "configured_root_missing": "configured runtime root is unavailable",
        "root_mismatch": "loaded MCP source does not match configured runtime root",
    }.get(issue, "")


def _runtime_root_ready(root: Path | None) -> bool:
    """Whether ``python -m sonder_runtime mcp`` is structurally available."""
    if root is None:
        return False
    required = (
        root / "server.py",
        root / "sonder_runtime" / "__init__.py",
        root / "sonder_runtime" / "__main__.py",
    )
    try:
        return root.is_dir() and all(path.is_file() for path in required)
    except OSError:
        return False


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


def _model_signature(items: list) -> str:
    rows = [item.model_dump(mode="json") for item in items]
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _resource_manager_signature(manager: ResourceManager) -> str:
    return _model_signature(manager.list_resources() + manager.list_templates())


def _prompt_manager_signature(manager: PromptManager) -> str:
    return _model_signature(manager.list_prompts())


class ReloadableFastMCP(FastMCP):
    """FastMCP with atomic in-process source and tool-surface refresh."""

    def __init__(self, *args, **kwargs):
        self._reload_lock = threading.RLock()
        self._staging_manager: ToolManager | None = None
        self._staging_resource_manager: ResourceManager | None = None
        self._staging_prompt_manager: PromptManager | None = None
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
        self._last_surface_changes = {
            "tools": False,
            "resources": False,
            "prompts": False,
        }
        self._advertise_list_changes()

    def _advertise_list_changes(self) -> None:
        original = self._mcp_server.create_initialization_options

        def create_options(notification_options=None, experimental_capabilities=None):
            current = notification_options or NotificationOptions()
            options = NotificationOptions(
                prompts_changed=True,
                resources_changed=True,
                tools_changed=True,
            )
            return original(options, experimental_capabilities)

        self._mcp_server.create_initialization_options = create_options

    def begin_module_refresh(self) -> None:
        """Start collecting decorators into an isolated replacement manager."""
        with self._reload_lock:
            if self._staging_manager is None:
                self._staging_manager = ToolManager(warn_on_duplicate_tools=False)
                self._staging_resource_manager = ResourceManager(
                    warn_on_duplicate_resources=False
                )
                self._staging_prompt_manager = PromptManager(
                    warn_on_duplicate_prompts=False
                )

    def abort_module_refresh(self, error: Exception | str) -> None:
        """Discard an incomplete registry and preserve the last known-good one."""
        with self._reload_lock:
            self._staging_manager = None
            self._staging_resource_manager = None
            self._staging_prompt_manager = None
            self._staging_source_state = None
            error_type = (
                "RuntimeError" if isinstance(error, str) else type(error).__name__
            )
            # Exception messages can embed source lines, absolute paths, URLs,
            # and credentials. The operator-facing state needs the failure
            # class, not those unbounded details.
            self._last_error = "%s: source refresh failed" % error_type

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
            changes = {"tools": False, "resources": False, "prompts": False}
            if self._staging_manager is not None:
                changes["tools"] = _manager_signature(self._tool_manager) != _manager_signature(
                    self._staging_manager
                )
                changes["resources"] = _resource_manager_signature(
                    self._resource_manager
                ) != _resource_manager_signature(self._staging_resource_manager)
                changes["prompts"] = _prompt_manager_signature(
                    self._prompt_manager
                ) != _prompt_manager_signature(self._staging_prompt_manager)
                self._tool_manager = self._staging_manager
                self._resource_manager = self._staging_resource_manager
                self._prompt_manager = self._staging_prompt_manager
                self._staging_manager = None
                self._staging_resource_manager = None
                self._staging_prompt_manager = None
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
            self._last_surface_changes = changes
            self._last_surface_changed = any(changes.values())
            self._last_error = ""
            return self._last_surface_changed

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

    def add_resource(self, resource) -> None:
        with self._reload_lock:
            manager = self._staging_resource_manager
            if manager is not None:
                manager.add_resource(resource)
                return
        super().add_resource(resource)

    def resource(self, uri: str, **kwargs):
        """Register resources against the isolated manager during refresh."""
        if callable(uri):
            raise TypeError("Use @resource('uri') instead of @resource")

        def decorator(fn):
            signature = inspect.signature(fn)
            has_uri_params = "{" in uri and "}" in uri
            if has_uri_params or signature.parameters:
                context_param = find_context_parameter(fn)
                uri_params = set(re.findall(r"{(\w+)}", uri))
                func_params = {p for p in signature.parameters if p != context_param}
                if uri_params != func_params:
                    raise ValueError(
                        f"Mismatch between URI parameters {uri_params} "
                        f"and function parameters {func_params}"
                    )
                with self._reload_lock:
                    manager = self._staging_resource_manager or self._resource_manager
                    manager.add_template(fn=fn, uri_template=uri, **kwargs)
            else:
                resource = FunctionResource.from_function(fn=fn, uri=uri, **kwargs)
                self.add_resource(resource)
            return fn

        return decorator

    def add_prompt(self, prompt) -> None:
        with self._reload_lock:
            manager = self._staging_prompt_manager
            if manager is not None:
                manager.add_prompt(prompt)
                return
        super().add_prompt(prompt)

    def _current_source_state(self) -> dict | None:
        if not self._reload_source_path:
            return None
        try:
            path = Path(self._reload_source_path)
            stat = path.stat()
            if (
                not self._last_error
                and int(stat.st_mtime_ns) == self._loaded_mtime_ns
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
        except FileNotFoundError:
            self._last_error = _provenance_error("stale_source_root")
            return None
        except OSError as exc:
            self._last_error = "source access %s" % type(exc).__name__
            return None

    def _runtime_provenance(self) -> dict:
        source = Path(self._reload_source_path) if self._reload_source_path else None
        source_root = source.parent if source else None
        configured_text = os.environ.get("SONDER_RUNTIME_ROOT", "").strip()
        configured = None
        configured_path_error = False
        if configured_text:
            try:
                configured = Path(configured_text).expanduser()
            except (OSError, RuntimeError):
                # Path.expanduser raises RuntimeError when the host cannot
                # determine a home directory. Treat that like an unavailable
                # configured root; never discard the active registry or expose
                # the path/error detail.
                configured_path_error = True
        source_exists = bool(source and source.is_file())
        source_root_exists = bool(source_root and source_root.is_dir())
        configured_exists = bool(configured and configured.is_dir())
        same_root = bool(
            source_root
            and configured
            and os.path.normcase(str(source_root.resolve()))
            == os.path.normcase(str(configured.resolve()))
        )
        issue = ""
        if source and not source_exists:
            issue = "stale_source_root"
        elif configured_text and (configured_path_error or not configured_exists):
            issue = "configured_root_missing"
        elif configured_exists and not same_root:
            issue = "root_mismatch"
        configured_ready = _runtime_root_ready(configured)
        action = _recovery_action(configured_ready) if issue else ""
        try:
            os.getcwd()
            cwd = "(available)"
        except OSError:
            cwd = "(deleted or unavailable)"
        return {
            "pid": os.getpid(),
            "python": Path(sys.executable).name or "python",
            "cwd": cwd,
            "source_root": "(loaded source root)" if source_root else "",
            "source_exists": source_exists,
            "source_root_exists": source_root_exists,
            "configured_runtime_root": "(set)" if configured_text else "",
            "configured_root_exists": configured_exists,
            "configured_root_ready": configured_ready,
            "root_matches_configured": same_root if configured else None,
            "issue": issue,
            "recovery_action": action,
        }

    def refresh_if_changed(self) -> dict:
        """Load changed source into a fresh namespace and swap on full success."""
        if not _enabled() or not self._reload_source_path:
            return {"reloaded": False, "surface_changed": False}
        provenance = self._runtime_provenance()
        provenance_error = _provenance_error(provenance.get("issue", ""))
        if provenance_error:
            self._last_error = provenance_error
            return {
                "reloaded": False,
                "surface_changed": False,
                "error": self._last_error,
            }
        current = self._current_source_state()
        if current is None:
            return {
                "reloaded": False,
                "surface_changed": False,
                "error": self._last_error or "MCP source is unavailable",
            }
        if current["digest"] == self._loaded_digest:
            self._loaded_mtime_ns = current["mtime_ns"]
            self._loaded_size = current["size"]
            self._last_error = ""
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
                self._last_error = ""
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
        refreshed = self.refresh_if_changed()
        await self._notify_surface_changes(refreshed)
        return await super().list_tools()

    async def call_tool(self, name: str, arguments: dict):
        refreshed = self.refresh_if_changed()
        await self._notify_surface_changes(refreshed)
        _refuse_if_gated(name)
        return await super().call_tool(name, arguments)

    async def _notify_surface_changes(self, refreshed: dict) -> None:
        if not refreshed.get("surface_changed"):
            return
        try:
            context = self.get_context()
            session = context.request_context.session
            changes = self._last_surface_changes
            if changes["tools"]:
                await session.send_tool_list_changed()
            if changes["resources"]:
                await session.send_resource_list_changed()
            if changes["prompts"]:
                await session.send_prompt_list_changed()
            self._last_notification_error = ""
        except Exception as exc:  # pragma: no cover - transport/client specific
            self._last_notification_error = (
                "%s: MCP list-change notification failed" % type(exc).__name__
            )

    async def list_resources(self):
        refreshed = self.refresh_if_changed()
        await self._notify_surface_changes(refreshed)
        return await super().list_resources()

    async def list_resource_templates(self):
        refreshed = self.refresh_if_changed()
        await self._notify_surface_changes(refreshed)
        return await super().list_resource_templates()

    async def read_resource(self, uri):
        refreshed = self.refresh_if_changed()
        await self._notify_surface_changes(refreshed)
        return await super().read_resource(uri)

    async def list_prompts(self):
        refreshed = self.refresh_if_changed()
        await self._notify_surface_changes(refreshed)
        return await super().list_prompts()

    async def get_prompt(self, name: str, arguments: dict | None = None):
        refreshed = self.refresh_if_changed()
        await self._notify_surface_changes(refreshed)
        return await super().get_prompt(name, arguments)

    def runtime_snapshot(self) -> dict:
        current = self._current_source_state()
        provenance = self._runtime_provenance()
        effective_error = self._last_error or _provenance_error(
            provenance.get("issue", "")
        )
        current_digest = current["digest"] if current is not None else ""
        source_changed = bool(
            current_digest
            and self._loaded_digest
            and current_digest != self._loaded_digest
        )
        # Disabled must win over source_changed. When live reload is off the
        # registry is frozen no matter what the file on disk says, so an on-disk
        # edit will NEVER be applied -- reporting "refresh pending" there tells
        # an operator auditing convergence that a refresh is imminent when in
        # fact none will ever occur. A pending edit that is being ignored is
        # worth naming, so it gets its own explicit status.
        if effective_error:
            status = "error"
        elif not _enabled():
            status = "disabled (pending edit ignored)" if source_changed else "disabled"
        elif source_changed:
            status = "refresh pending"
        else:
            status = "current"
        return {
            "status": status,
            "enabled": _enabled(),
            "module": self._reload_module_name,
            "path": "(registered)" if self._reload_source_path else "",
            "loaded_digest": self._loaded_digest,
            "current_digest": current_digest,
            "source_changed": source_changed,
            "registered_tools": len(self._tool_manager.list_tools()),
            "refresh_count": self._refresh_count,
            "last_refresh_ts": self._last_refresh_ts,
            "last_surface_changed": self._last_surface_changed,
            "last_surface_changes": dict(self._last_surface_changes),
            "last_error": effective_error,
            "last_notification_error": self._last_notification_error,
            "protocol_list_changed": True,
            "provenance": provenance,
        }
