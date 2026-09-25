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
import re
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

# The MCP SDK costs roughly a second to import -- about 40% of ``import
# server`` -- and every process that imports ``server`` for something other
# than serving MCP (the REPL, the HTTP API, CLI subcommands, every pytest
# worker) used to pay it. The SDK is therefore loaded on first use: the names
# below resolve through the module ``__getattr__`` at the bottom of this file,
# and ``server.py`` holds a ``LazyReloadableMCPServer`` that records its
# decorator registrations and builds the real ``ReloadableMCPServer`` the first
# time anything touches the registry. ``tests/test_lazy_mcp_import.py`` pins
# that ``import server`` leaves ``mcp`` out of ``sys.modules``.
if TYPE_CHECKING:  # pragma: no cover - static typing only
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


def _refuse_if_gated(name: str, arguments=None) -> None:
    """Apply the operator's permission gate to a direct MCP tool call.

    ``arguments`` are the call's own, passed through to the decider and never
    stored: they let an unattended refusal name the call (a call id an
    operator can approve once) and let such an approval answer the retry.

    This is the only place an MCP *client* enters; every internal Python call
    to the same function bypasses it, which is exactly the split we want --
    ``_agent_dispatch`` and the REPL each gate with their own ``interactive``
    value, and gating the function bodies instead would double-prompt them.

    ``interactive=False``: nobody is at a keyboard behind a protocol call, so
    a mode's ``ask`` is answered by the tool's class -- file changes, host
    programs and destructive tools are refused with the remedies named,
    ask-class tools proceed on the record -- and ``plan`` denies here too.
    Without the gate, ``plan`` advertised "reads only - no writes, no
    commands" while a client could call ``file_write`` straight through -- an
    operator who selects that mode and then watches their workspace change
    has been lied to by the indicator. An explicit per-tool ``deny`` rule
    refuses here as well.

    ``GATE_CONTROL_TOOLS`` is exempt, because the refusal below names
    ``permission_mode`` as the remedy and ``plan`` would otherwise refuse that
    tool too -- leaving a client that selected ``plan`` no way to select
    anything else, across restarts, since the mode persists to disk. The
    exemption is a way *out*, not *up*: ``server.permission_mode`` itself
    refuses an unattended request to raise the mode above ``manual``
    (``permission_modes.unattended_escalation_refusal``).

    Imported lazily: ``permission_modes`` resolves the command catalog, which
    imports ``server``, which imports this module.
    """
    import permission_modes

    tool = str(name or "")
    decision = permission_modes.decide_for_caller(
        tool, interactive=False, gate_control_exempt=True, surface="mcp",
        arguments=arguments if isinstance(arguments, dict) else None,
    )
    if decision is None or decision.allowed:
        return
    _load_sdk()
    raise ToolError(
        "%s is refused by the active permission gate: %s (mode=%s, risk=%s). "
        "Change the mode with the permission_mode tool, or write a rule with "
        "permission_rule_set." % (
            name, decision.reason, decision.mode, decision.risk,
        )
    )


def _refuse_unknown_arguments(tool, arguments) -> None:
    """Reject argument names the tool's declared input schema does not list.

    The upstream argument models ignore extra fields, so ``{"query": "x",
    "bogus": 1}`` ran as if ``bogus`` had never been sent -- a misspelt
    ``max_results`` or ``root`` silently fell back to its default and the
    caller was never told. The native surface rejects these; this is the same
    contract for the legacy one. A schema that explicitly admits additional
    properties is honoured.
    """
    if not isinstance(arguments, dict) or not arguments:
        return
    schema = getattr(tool, "parameters", None)
    if not isinstance(schema, dict):
        return
    extra = schema.get("additionalProperties")
    if extra is True or isinstance(extra, dict):
        return
    properties = schema.get("properties")
    accepted = set(properties) if isinstance(properties, dict) else set()
    unknown = sorted(str(key) for key in arguments if key not in accepted)
    if not unknown:
        return
    shown = ", ".join(key[:64] for key in unknown[:8])
    if len(unknown) > 8:
        shown += ", ... (%d more)" % (len(unknown) - 8)
    raise ToolError(
        "%s does not accept argument(s): %s. Accepted arguments: %s." % (
            getattr(tool, "name", "tool"), shown,
            ", ".join(sorted(accepted)) or "(none)",
        )
    )


def _flag_legacy_error_reply(result):
    """Mark a legacy ``ERROR:`` reply as an MCP tool error (``isError``).

    The legacy tools report failures and refusals as text beginning with
    ``ERROR:`` rather than raising, so a client saw ``isError: false`` for a
    refused ``file_read``, disabled web tools, or an agent that ran out of
    steps, and had no protocol-level way to tell them from success. The text
    itself is kept as the error message.

    The classification is the one the loop surface already applies to the same
    tool output (``loop_text_result``), deliberately reused rather than
    re-derived so the two surfaces cannot disagree about what failed.
    """
    if getattr(result, "is_error", True):
        return result
    content = getattr(result, "content", None) or ()
    if not content:
        return result
    first = content[0]
    text = getattr(first, "text", None)
    if getattr(first, "type", None) != "text" or not isinstance(text, str):
        return result
    from sonder_runtime.domain.loop_result_formatting import loop_text_result

    if loop_text_result("mcp_tool", text)["ok"]:
        return result
    return result.model_copy(update={"is_error": True})


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




def _sync_loop_tool_docstring(fn, action_types) -> None:
    """Keep ``loop.__doc__`` vocabulary aligned with ``_LOOP_ACTION_TYPES``.

    FastMCP/MCPServer applies ``inspect.cleandoc`` when registering tools, which
    strips the indent before ``Argument shapes``. The in-module rewrite in
    ``server.py`` historically looked for that indent and silently no-oped, so
    the docstring lagged aliases the unknown-action reply already listed. Apply
    the sync here, after cleandoc, whenever the ``loop`` tool is registered.
    """
    if not action_types:
        return
    doc = fn.__doc__ or ""
    marker = "All valid `type` values:"
    marker_at = doc.find(marker)
    if marker_at < 0:
        return
    tail_match = re.search(r"\n\n[ \t]*Argument shapes", doc[marker_at:])
    if tail_match is None:
        return
    tail_at = marker_at + tail_match.start()
    head = doc[: marker_at + len(marker)]
    fn.__doc__ = head + " " + ", ".join(action_types) + "." + doc[tail_at:]


# The largest legitimate legacy frame is a ``file_write`` at the write cap
# (``file_ops.MAX_WRITE_BYTES``, 1 MB) whose content JSON-escapes to at most
# about twice its size, plus envelope headroom. The upstream stdio transport
# reads a line of any length, so without this a single frame could hold
# arbitrary memory and be echoed back whole.
LEGACY_MCP_MAX_FRAME_BYTES = 2 * 1_000_000 + 64 * 1024

_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600


def _frame_rejection(raw: bytes):
    """``None`` for a frame the upstream parser accepts, else the reply.

    The reply is ``(request_id, code, message)``. The upstream ``mcp`` stdio
    transport (2.0.0) hands a frame it cannot parse to the session as a bare
    exception, which the server loop drops without answering: ``not json``,
    a JSON array, ``"jsonrpc": "1.0"``, a lone-surrogate escape, and a request
    whose ``id`` is ``true`` (accepted upstream as a *notification*) all got no
    response at all, so a client waiting on that id hung. This applies the
    very same parser first and answers what it refuses with a JSON-RPC error,
    echoing the id whenever the frame carried a valid one.
    """
    import mcp_types

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, _PARSE_ERROR, "Parse error: frame is not valid UTF-8"
    try:
        value = json.loads(text)
    except ValueError:
        return None, _PARSE_ERROR, "Parse error: frame is not valid JSON"
    if not isinstance(value, dict):
        return None, _INVALID_REQUEST, (
            "Invalid Request: a frame must be one JSON-RPC object (batches are "
            "not supported)"
        )
    request_id = value.get("id")
    if "id" in value and type(request_id) not in (int, str):
        return None, _INVALID_REQUEST, "Invalid Request: id must be a string or integer"
    try:
        mcp_types.jsonrpc_message_adapter.validate_json(text, by_name=False)
    except Exception as exc:
        kinds = []
        errors = getattr(exc, "errors", None)
        if callable(errors):
            try:
                kinds = sorted({str(item.get("type", "")) for item in errors()})
            except Exception:
                kinds = []
        if "json_invalid" in kinds:
            return request_id, _PARSE_ERROR, (
                "Parse error: frame is not valid JSON-RPC text (for example an "
                "unpaired UTF-16 surrogate escape)"
            )
        return request_id, _INVALID_REQUEST, (
            "Invalid Request: not a valid JSON-RPC 2.0 message"
        )
    return None


class _GuardedStdin:
    """Bounded line source for ``stdio_server`` that answers bad frames.

    It is passed as ``stdio_server(stdin=...)``, whose reader only iterates it,
    and yields exactly the frames the upstream parser accepts. Everything else
    is answered here, on the server's own write stream, before the next line
    is read, so replies keep their order relative to the frames around them.
    """

    def __init__(self, buffer, *, max_frame_bytes: int = LEGACY_MCP_MAX_FRAME_BYTES):
        import anyio

        self._buffer = buffer
        self._max = int(max_frame_bytes)
        self._ready = anyio.Event()
        self._write_stream = None

    def attach(self, write_stream) -> None:
        self._write_stream = write_stream
        self._ready.set()

    def __aiter__(self):
        return self._frames()

    async def _readline(self) -> bytes:
        import anyio.to_thread

        return await anyio.to_thread.run_sync(self._buffer.readline, self._max + 1)

    async def _frames(self):
        while True:
            raw = await self._readline()
            if not raw:
                return
            if len(raw) > self._max and not raw.endswith(b"\n"):
                # Drain the rest of the line so its tail is never read back
                # as a separate frame (and so a request hidden past the bound
                # is not executed).
                while True:
                    chunk = await self._readline()
                    if not chunk or chunk.endswith(b"\n"):
                        break
                await self._reject(None, _INVALID_REQUEST, (
                    "Invalid Request: frame exceeds %d bytes" % self._max
                ))
                continue
            if not raw.strip():
                continue
            rejection = _frame_rejection(raw)
            if rejection is None:
                yield raw.decode("utf-8")
                continue
            await self._reject(*rejection)

    async def _reject(self, request_id, code: int, message: str) -> None:
        import logging

        import mcp_types
        from mcp.shared.message import SessionMessage

        logging.getLogger("sonder.mcp").warning(
            "legacy MCP frame rejected: code=%s %s", code, message,
        )
        await self._ready.wait()
        error = mcp_types.JSONRPCError(
            jsonrpc="2.0", id=request_id,
            error=mcp_types.ErrorData(code=code, message=message),
        )
        await self._write_stream.send(SessionMessage(error))


class _ReloadableMCPServerMixin:
    """MCPServer with atomic in-process source and tool-surface refresh.

    Defined as a mixin so this module imports without the MCP SDK;
    ``_load_sdk`` combines it with ``MCPServer`` into ``ReloadableMCPServer``.
    Zero-argument ``super()`` below resolves through the combined class's MRO,
    so every override still reaches ``MCPServer`` exactly as before.
    """

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


    def tool(self, *args, **kwargs):
        """Register a tool; keep ``loop`` docstring vocabulary in lockstep."""
        inner = super().tool(*args, **kwargs)

        def decorator(fn):
            result = inner(fn)
            tool_name = kwargs.get("name") or getattr(fn, "__name__", "")
            if tool_name != "loop" and getattr(fn, "__name__", "") != "loop":
                return result
            module = sys.modules.get(getattr(fn, "__module__", "") or "")
            action_types = getattr(module, "_LOOP_ACTION_TYPES", None) if module else None
            target = result if getattr(result, "__doc__", None) is not None else fn
            _sync_loop_tool_docstring(target, action_types)
            if target is not fn:
                _sync_loop_tool_docstring(fn, action_types)
            return result

        return decorator

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

    async def run_stdio_async(self) -> None:
        """Serve stdio with a bounded frame size and answered parse errors.

        Same claim of fd 0/1 as upstream ``run_stdio_async`` (children and
        stray prints never touch the wire); only the line source differs --
        see ``_GuardedStdin``. The claim helpers are upstream-private, so if a
        future ``mcp`` release moves them this falls back to the stock
        transport, loudly, rather than failing to serve.
        """
        try:
            from mcp.server import stdio as upstream_stdio

            claim_fd = upstream_stdio._claim_fd
            open_stdin_diversion = upstream_stdio._open_stdin_diversion
        except (ImportError, AttributeError):
            import logging

            logging.getLogger("sonder.mcp").warning(
                "legacy MCP frame guard unavailable for this mcp release; "
                "oversized and malformed frames are handled by upstream"
            )
            await super().run_stdio_async()
            return
        buffer, release = claim_fd(0, sys.stdin, "rb", open_stdin_diversion)
        try:
            guard = _GuardedStdin(buffer)
            async with upstream_stdio.stdio_server(stdin=guard) as (read_stream, write_stream):
                guard.attach(write_stream)
                await self._lowlevel_server.run(
                    read_stream,
                    write_stream,
                    self._lowlevel_server.create_initialization_options(),
                )
        finally:
            if release is not None:
                release()

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
        tool = self._tool_manager.get_tool(name)
        if tool is None:
            # Nothing would run, so there is nothing to gate: answer as an
            # unknown tool rather than advising a permission rule for it.
            raise ToolError(f"Unknown tool: {name}")
        # Before the gate: a call the tool cannot accept runs nothing, so it
        # must not spend a one-shot approval or be recorded as a refusal.
        _refuse_unknown_arguments(tool, arguments)
        # The reach scope wraps the gate and the call: the roots a one-shot
        # approval covered appear only once the gate has spent it for exactly
        # this call, and vanish when the call is over.
        import server

        gate_arguments = (server._agent_lane_gate_arguments(arguments)
                          if name == "agent_lane" else arguments)
        with server.approved_call_reach(name, gate_arguments):
            _refuse_if_gated(name, gate_arguments)
            result = await super().call_tool(name, arguments, context)
        return _flag_legacy_error_reply(result)

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


_SDK_EXPORTS = frozenset({
    "MCPServer",
    "ToolError",
    "ResourceManager",
    "PromptManager",
    "ToolManager",
    "NotificationOptions",
    "PromptsListChanged",
    "ResourcesListChanged",
    "ToolsListChanged",
    "ReloadableMCPServer",
})
_SDK_LOCK = threading.Lock()


def _load_sdk():
    """Import the MCP SDK once; publish its names and ``ReloadableMCPServer``."""
    namespace = globals()
    loaded = namespace.get("ReloadableMCPServer")
    if loaded is not None:
        return loaded
    with _SDK_LOCK:
        loaded = namespace.get("ReloadableMCPServer")
        if loaded is not None:
            return loaded
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

        class ReloadableMCPServer(_ReloadableMCPServerMixin, MCPServer):
            __doc__ = _ReloadableMCPServerMixin.__doc__

        ReloadableMCPServer.__module__ = __name__
        ReloadableMCPServer.__qualname__ = "ReloadableMCPServer"
        namespace.update(
            MCPServer=MCPServer,
            ToolError=ToolError,
            ResourceManager=ResourceManager,
            PromptManager=PromptManager,
            ToolManager=ToolManager,
            NotificationOptions=NotificationOptions,
            PromptsListChanged=PromptsListChanged,
            ResourcesListChanged=ResourcesListChanged,
            ToolsListChanged=ToolsListChanged,
        )
        # Published last: its presence is what marks the SDK as loaded.
        namespace["ReloadableMCPServer"] = ReloadableMCPServer
        return ReloadableMCPServer


def __getattr__(name: str):
    if name in _SDK_EXPORTS:
        _load_sdk()
        return globals()[name]
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


def sdk_loaded() -> bool:
    """Whether the MCP SDK (and so ``ReloadableMCPServer``) has been imported."""
    return "ReloadableMCPServer" in globals()


def is_reloadable_server(value) -> bool:
    """``isinstance`` against either registry type, without importing the SDK.

    ``server.py`` uses this to find the registry that survives a hot reload.
    Naming ``ReloadableMCPServer`` there would import the SDK on every
    ``import server`` -- the cost the lazy registry exists to avoid -- and a
    plain ``isinstance`` on a lazy registry would build the real one.
    """
    if type(value) is LazyReloadableMCPServer:
        return True
    cls = globals().get("ReloadableMCPServer")
    return cls is not None and isinstance(value, cls)


class LazyReloadableMCPServer:
    """A ``ReloadableMCPServer`` that is built on first use.

    Until something reads the registry, the decorator API ``server.py`` runs
    at import time -- ``tool``, ``resource``, ``prompt`` and the
    ``begin/finish/abort_module_refresh`` staging protocol -- is recorded
    without importing the MCP SDK. Any other attribute access (``run``,
    ``call_tool``, ``_tool_manager``, ``runtime_snapshot``, ``isinstance``
    against ``ReloadableMCPServer``, ...) builds the real server, replays the
    recorded registrations in their original order, and from then on every
    attribute read, write and delete is forwarded to it.

    The staging protocol keeps its guarantees while lazy: registrations made
    between ``begin_module_refresh`` and ``finish_module_refresh`` are held
    apart and replace the committed set only on finish; ``abort`` discards
    them and keeps the last known-good set. The source identity recorded by
    ``finish_module_refresh`` is captured at finish time, not at build time,
    so a file edited in between still shows as a pending refresh afterwards.

    One ordering difference is inherent: upstream decorator validation (a
    malformed resource URI template, say) raises when the registry is built
    rather than while ``server.py`` executes. Hot reloads run against the
    built registry and still fail closed at exec time.
    """

    def __init__(self, *args, **kwargs):
        object.__setattr__(self, "_lazy_lock", threading.RLock())
        object.__setattr__(self, "_lazy_args", (args, kwargs))
        object.__setattr__(self, "_lazy_real", None)
        object.__setattr__(self, "_lazy_committed", [])
        object.__setattr__(self, "_lazy_staging", None)
        object.__setattr__(self, "_lazy_finish", None)
        object.__setattr__(self, "_lazy_swaps", 0)
        object.__setattr__(self, "_lazy_swap_ts", 0)
        object.__setattr__(self, "_lazy_error", "")

    # -- recording -------------------------------------------------------

    def _lazy_built(self):
        """The real server, built now if the SDK is already imported.

        Deferring only pays while the SDK is unloaded. Once some caller has
        imported it anyway, building immediately restores the eager ordering
        exactly -- ``finish_module_refresh`` then runs on the real registry
        while the module executes, as it always did.
        """
        real = self._lazy_real
        if real is None and sdk_loaded():
            real = self._lazy_materialize()
        return real

    def _lazy_record(self, entry) -> bool:
        """Record ``entry`` unless the real server exists (then return False)."""
        with self._lazy_lock:
            if self._lazy_built() is not None:
                return False
            target = self._lazy_staging
            if target is None:
                target = self._lazy_committed
            target.append(entry)
            return True

    def tool(self, *args, **kwargs):
        real = self._lazy_built()
        if real is not None:
            return real.tool(*args, **kwargs)
        if args and callable(args[0]):
            # The same misuse error upstream raises for ``@mcp.tool``.
            raise TypeError(
                "The @tool decorator was used incorrectly. Did you forget to "
                "call it? Use @tool() instead of @tool"
            )

        def decorator(fn):
            if not self._lazy_record(("tool", args, kwargs, fn, fn.__doc__)):
                return self._lazy_real.tool(*args, **kwargs)(fn)
            # The real ``tool`` keeps ``loop.__doc__`` in lockstep with
            # ``_LOOP_ACTION_TYPES`` after registering it; readers of the
            # function must see the synced docstring before the build too.
            tool_name = kwargs.get("name") or getattr(fn, "__name__", "")
            if tool_name == "loop" or getattr(fn, "__name__", "") == "loop":
                module = sys.modules.get(getattr(fn, "__module__", "") or "")
                action_types = (
                    getattr(module, "_LOOP_ACTION_TYPES", None) if module else None
                )
                _sync_loop_tool_docstring(fn, action_types)
            return fn

        return decorator

    def resource(self, uri: str, **kwargs):
        real = self._lazy_built()
        if real is not None:
            return real.resource(uri, **kwargs)
        if callable(uri):
            raise TypeError(
                "The @resource decorator was used incorrectly. Did you forget "
                "to call it? Use @resource('uri') instead of @resource"
            )

        def decorator(fn):
            if not self._lazy_record(("resource", (uri,), kwargs, fn, None)):
                return self._lazy_real.resource(uri, **kwargs)(fn)
            return fn

        return decorator

    def prompt(self, *args, **kwargs):
        real = self._lazy_built()
        if real is not None:
            return real.prompt(*args, **kwargs)
        if args and callable(args[0]):
            raise TypeError(
                "The @prompt decorator was used incorrectly. Did you forget "
                "to call it? Use @prompt() instead of @prompt"
            )

        def decorator(fn):
            if not self._lazy_record(("prompt", args, kwargs, fn, None)):
                return self._lazy_real.prompt(*args, **kwargs)(fn)
            return fn

        return decorator

    def begin_module_refresh(self) -> None:
        with self._lazy_lock:
            real = self._lazy_built()
            if real is None:
                if self._lazy_staging is None:
                    object.__setattr__(self, "_lazy_staging", [])
                return
        real.begin_module_refresh()

    def abort_module_refresh(self, error: Exception | str) -> None:
        with self._lazy_lock:
            real = self._lazy_built()
            if real is None:
                object.__setattr__(self, "_lazy_staging", None)
                error_type = (
                    "RuntimeError" if isinstance(error, str) else type(error).__name__
                )
                object.__setattr__(
                    self, "_lazy_error", "%s: source refresh failed" % error_type
                )
                return
        real.abort_module_refresh(error)

    def finish_module_refresh(
        self,
        module_name: str,
        source_path: str,
        namespace: dict | None = None,
    ) -> bool:
        with self._lazy_lock:
            real = self._lazy_built()
            if real is None:
                # Read now, exactly when the eager registry read it.
                state = _source_state(source_path)
                if self._lazy_staging is not None:
                    object.__setattr__(self, "_lazy_committed", self._lazy_staging)
                    object.__setattr__(self, "_lazy_staging", None)
                    object.__setattr__(self, "_lazy_swaps", self._lazy_swaps + 1)
                    object.__setattr__(self, "_lazy_swap_ts", int(time.time()))
                object.__setattr__(
                    self, "_lazy_finish", (module_name, source_path, namespace, state)
                )
                object.__setattr__(self, "_lazy_error", "")
                # No client can have listed a surface that was never built.
                return False
        return real.finish_module_refresh(module_name, source_path, namespace)

    # -- building --------------------------------------------------------

    @staticmethod
    def _lazy_replay(real, entry) -> None:
        kind, args, kwargs, fn, raw_doc = entry
        if kind == "tool":
            # Register with the docstring the eager registry saw; ``tool``
            # re-applies the ``loop`` sync afterwards and reaches the same text.
            fn.__doc__ = raw_doc
            real.tool(*args, **kwargs)(fn)
        elif kind == "resource":
            real.resource(*args, **kwargs)(fn)
        else:
            real.prompt(*args, **kwargs)(fn)

    def _lazy_materialize(self):
        real = self._lazy_real
        if real is not None:
            return real
        with self._lazy_lock:
            real = self._lazy_real
            if real is not None:
                return real
            cls = _load_sdk()
            args, kwargs = self._lazy_args
            real = cls(*args, **kwargs)
            for entry in self._lazy_committed:
                self._lazy_replay(real, entry)
            finish = self._lazy_finish
            if finish is not None:
                module_name, source_path, namespace, state = finish
                real._staging_source_state = state
                real.finish_module_refresh(module_name, source_path, namespace)
            if self._lazy_swaps:
                real._refresh_count += self._lazy_swaps
                real._last_refresh_ts = self._lazy_swap_ts
            if self._lazy_error:
                real._last_error = self._lazy_error
            staging = self._lazy_staging
            if staging is not None:
                # Built mid-refresh: keep collecting into an isolated registry.
                real.begin_module_refresh()
                for entry in staging:
                    self._lazy_replay(real, entry)
            object.__setattr__(self, "_lazy_committed", [])
            object.__setattr__(self, "_lazy_staging", None)
            object.__setattr__(self, "_lazy_real", real)
            return real

    # -- forwarding ------------------------------------------------------

    @property
    def __class__(self):
        return type(self._lazy_materialize())

    def __getattr__(self, name: str):
        if name.startswith("__") and name.endswith("__"):
            # Protocol probes (copy, pickle, inspect, ...) must not import the
            # SDK; ``MCPServer`` defines no dunder attributes they look for.
            raise AttributeError(name)
        return getattr(self._lazy_materialize(), name)

    def __setattr__(self, name: str, value) -> None:
        setattr(self._lazy_materialize(), name, value)

    def __delattr__(self, name: str) -> None:
        delattr(self._lazy_materialize(), name)

    def __dir__(self):
        return dir(self._lazy_materialize())

    def __repr__(self) -> str:
        real = self._lazy_real
        if real is None:
            return "<LazyReloadableMCPServer (MCP SDK not loaded yet)>"
        return repr(real)

