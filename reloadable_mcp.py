"""Atomic live refresh for a long-running MCPServer tool registry.

``MCPServer`` supports adding tools at runtime, and MCP supports
``notifications/tools/list_changed``. This wrapper combines those primitives
with a fail-closed source reload: a complete replacement registry is staged in
isolation and swapped only after the updated server module executes cleanly.

MCP 2.x renamed ``mcp.server.fastmcp`` to ``mcp.server.mcpserver`` and
``FastMCP`` to ``MCPServer``; the ergonomic-server API this module extends is
otherwise the same. See ``docs/MCP_2_MIGRATION.md`` for the per-symbol map.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.resources import ResourceManager
from mcp.server.mcpserver.prompts import PromptManager
from mcp.server.mcpserver.tools import ToolManager
from mcp.server.lowlevel.server import NotificationOptions
from mcp.shared.subscriptions import (
    PromptsListChanged,
    ResourcesListChanged,
    ToolsListChanged,
)


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


class ReloadableMCPServer(MCPServer):
    """MCPServer with atomic in-process source and tool-surface refresh."""

    def __init__(self, *args, **kwargs):
        self._reload_lock = threading.RLock()
        # Set before super().__init__, which assigns _resource_manager through
        # the property below and would otherwise read an attribute that does
        # not exist yet.
        self._decorating = threading.local()
        self._active_resource_manager: ResourceManager | None = None
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

    @property
    def _resource_manager(self) -> ResourceManager:
        """The manager upstream code writes to and reads from.

        During a staged refresh this returns the staging manager, but ONLY on
        the stack doing the reload. Publishing it instance-wide instead would
        expose a half-built registry to any concurrent ``resources/list`` --
        the one thing the staged swap exists to make impossible.
        """
        target = getattr(self._decorating, "resources", None)
        return target if target is not None else self._active_resource_manager

    @_resource_manager.setter
    def _resource_manager(self, manager: ResourceManager) -> None:
        self._active_resource_manager = manager

    def _advertise_list_changes(self) -> None:
        original = self._lowlevel_server.create_initialization_options

        def create_options(
            notification_options=None,
            experimental_capabilities=None,
            extensions=None,
        ):
            options = NotificationOptions(
                prompts_changed=True,
                resources_changed=True,
                tools_changed=True,
            )
            return original(options, experimental_capabilities, extensions)

        self._lowlevel_server.create_initialization_options = create_options

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
                # MCP 1.x's low-level server kept a separate ``_tool_cache`` of
                # output schemas for result validation, and this swap had to
                # clear it or a changed/removed tool kept a stale schema. MCP
                # 2.x validates against ``Tool.fn_metadata.output_schema`` on
                # the tool object itself, so replacing the manager replaces the
                # schema with it and there is no second cache to invalidate.
                # The command catalog is a different story, and it is the
                # permission gate's
                # only source of truth for a tool's risk class. It is an
                # ``lru_cache`` over this very registry, and nothing ever
                # called ``reset_cache()`` -- its docstring said "used after a
                # live reload adds tools" and it had no callers at all. The
                # consequence outlives the reload: a tool this swap
                # RECLASSIFIED (say ``safe`` -> ``dangerous``) kept its stale
                # grade for the life of the process, and a newly added tool
                # was unknown to the catalog entirely. Imported lazily for the
                # import-cycle reason ``_refuse_if_gated`` documents.
                try:
                    import command_catalog

                    command_catalog.reset_cache()
                except Exception:
                    # A catalog that cannot be reset must not abort a swap that
                    # has already happened. The gate fails closed on a catalog
                    # it cannot read, which is the safe direction to leave this.
                    pass
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
        r"""Register resources against the isolated manager during refresh.

        This used to re-implement the upstream decorator so the template branch
        could target the staging manager. It no longer can: MCP 2.x parses the
        URI as a full RFC 6570 template and rejects shapes the old
        ``{(\w+)}`` regex silently accepted (and vice versa). A private copy
        of that logic would drift from the validation the server actually
        applies, so the registration is delegated upstream and only the manager
        it writes into is swapped -- for the duration of the decorator call,
        under the reload lock, which is the one thing this class needs to
        change.
        """
        inner = super().resource(uri, **kwargs)

        def decorator(fn):
            with self._reload_lock:
                staging = self._staging_resource_manager
                if staging is None:
                    return inner(fn)
                self._decorating.resources = staging
                try:
                    return inner(fn)
                finally:
                    self._decorating.resources = None

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

    # The public surface below refreshes but never notifies. MCP 1.x exposed
    # the in-flight request through ``FastMCP.get_context()``, so any of these
    # could reach the client session ambiently; 2.x removed that accessor and
    # passes the request context to the ``_handle_*`` protocol entry points
    # instead. ``list_tools()`` and friends now take no context at all, and an
    # in-process caller has no session to notify in the first place. So the
    # refresh stays here, where every caller reaches it, and the notification
    # moved to the handlers below, which is the only place a session exists.

    async def list_tools(self):
        self.refresh_if_changed()
        return await super().list_tools()

    async def call_tool(self, name: str, arguments: dict, context=None):
        self.refresh_if_changed()
        _refuse_if_gated(name)
        return await super().call_tool(name, arguments, context)

    async def list_resources(self):
        self.refresh_if_changed()
        return await super().list_resources()

    async def list_resource_templates(self):
        self.refresh_if_changed()
        return await super().list_resource_templates()

    async def read_resource(self, uri, context=None):
        self.refresh_if_changed()
        return await super().read_resource(uri, context)

    async def list_prompts(self):
        self.refresh_if_changed()
        return await super().list_prompts()

    async def get_prompt(self, name: str, arguments: dict | None = None, context=None):
        self.refresh_if_changed()
        return await super().get_prompt(name, arguments, context)

    async def _notify_surface_changes(self, refreshed: dict, session) -> None:
        """Announce a swapped surface on every channel a client might be on.

        Two channels, because the client's protocol era decides which one
        carries the event and one server holds connections from both:

        * ``<= 2025-11-25`` clients receive change notifications on the
          connection channel -- ``session.send_*_list_changed()``.
        * ``2026-07-28`` clients do not. That channel drops them by
          construction (``NotifyOnlyOutbound.notify`` discards every method in
          ``LISTEN_STREAM_METHODS`` with a debug log, because the era forbids a
          change notification a subscription did not ask for). They arrive only
          through the ``subscriptions/listen`` stream the client opened, fed by
          the server's ``SubscriptionBus``.

        Sending on one channel only strands every client on the other era in
        the worst possible way: the swap succeeds, the new surface is live, and
        the client is never told -- so it keeps calling tools that no longer
        exist and never sees the ones that now do. Nothing raises; the drop is
        a debug log. This is the failure the migration from MCP 1.x introduced,
        where the connection channel was the only one that existed.

        Both are sent unconditionally rather than branched on
        ``ctx.protocol_version``: the redundant copy is discarded by whichever
        era did not want it, and that costs less than an era assumption that
        silently rots at the next protocol version.
        """
        if not refreshed.get("surface_changed"):
            return
        try:
            changes = self._last_surface_changes
            if changes["tools"]:
                await session.send_tool_list_changed()
                await self._subscriptions.publish(ToolsListChanged())
            if changes["resources"]:
                await session.send_resource_list_changed()
                await self._subscriptions.publish(ResourcesListChanged())
            if changes["prompts"]:
                await session.send_prompt_list_changed()
                await self._subscriptions.publish(PromptsListChanged())
            self._last_notification_error = ""
        except Exception as exc:  # pragma: no cover - transport/client specific
            self._last_notification_error = (
                "%s: MCP list-change notification failed" % type(exc).__name__
            )

    async def _refresh_and_notify(self, ctx) -> None:
        """Refresh at a protocol boundary and tell the client what moved.

        Runs before the handler dispatches, so a client that reacts to
        ``list_changed`` by re-listing sees the post-swap surface, and the
        response to the request in flight is already produced from it.
        """
        refreshed = self.refresh_if_changed()
        session = getattr(ctx, "session", None)
        if session is None:  # pragma: no cover - transport-specific
            return
        await self._notify_surface_changes(refreshed, session)

    async def _handle_list_tools(self, ctx, params):
        await self._refresh_and_notify(ctx)
        return await super()._handle_list_tools(ctx, params)

    async def _handle_call_tool(self, ctx, params):
        await self._refresh_and_notify(ctx)
        return await super()._handle_call_tool(ctx, params)

    async def _handle_list_resources(self, ctx, params):
        await self._refresh_and_notify(ctx)
        return await super()._handle_list_resources(ctx, params)

    async def _handle_list_resource_templates(self, ctx, params):
        await self._refresh_and_notify(ctx)
        return await super()._handle_list_resource_templates(ctx, params)

    async def _handle_read_resource(self, ctx, params):
        await self._refresh_and_notify(ctx)
        return await super()._handle_read_resource(ctx, params)

    async def _handle_list_prompts(self, ctx, params):
        await self._refresh_and_notify(ctx)
        return await super()._handle_list_prompts(ctx, params)

    async def _handle_get_prompt(self, ctx, params):
        await self._refresh_and_notify(ctx)
        return await super()._handle_get_prompt(ctx, params)

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

