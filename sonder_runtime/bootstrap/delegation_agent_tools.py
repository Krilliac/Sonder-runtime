"""MCP agent tool registrar for depth-1 delegation, live progress, and peer coordination."""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

import sonder_runtime.bootstrap.delegation_wiring as _wiring
from sonder_runtime.domain.cloud_access import LEGACY_ERROR_PREFIX

_F = TypeVar("_F", bound=Callable[..., object])


class _McpServer(Protocol):
    """The one registration hook ``register`` uses on the MCP server."""

    def tool(self) -> Callable[[_F], _F]: ...


def register(mcp: _McpServer, record: Any, *, runtime: Callable[[], Any]) -> None:
    """Register delegation, progress, and peer-board MCP tools."""

    def _run(name: str, args: dict[str, Any], fn: Callable[[], str]) -> str:
        started = time.time()
        try:
            output = fn()
        except (ValueError, TypeError, RuntimeError, PermissionError, OSError) as exc:
            output = (f"{LEGACY_ERROR_PREFIX} {name}: {exc}")[:1200]
        if callable(record):
            record(
                name,
                args,
                ok=not output.startswith(LEGACY_ERROR_PREFIX),
                started=started,
                summary=output[:200],
                output=output,
            )
        return output

    @mcp.tool()
    def delegate_task(
        task: str,
        name: str = "",
        role: str = "",
        personality: str = "",
        tier: str = "",
        model: str = "",
        project: str = "",
        cwd: str = "",
        max_steps: int = 8,
        read_only: bool | None = None,
    ) -> str:
        """Delegate one bounded subtask to a depth-1 child agent (depth 0 only)."""
        args: dict[str, Any] = {
            "task": task,
            "name": name,
            "role": role,
            "personality": personality,
            "tier": tier,
            "model": model,
            "project": project,
            "cwd": cwd,
            "max_steps": max_steps,
            "read_only": read_only,
        }
        return _run("delegate_task", args, lambda: _wiring.dispatch_delegation(runtime(), "delegate_task", args))

    @mcp.tool()
    def spawn_agents(
        tasks: list[dict[str, Any]] | dict[str, Any] | str | None = None,
        tasks_json: str = "",
        tier: str = "",
        model: str = "",
        project: str = "",
        cwd: str = "",
        max_steps: int = 8,
        read_only: bool | None = None,
    ) -> str:
        """Spawn multiple depth-1 child agents in parallel across Main and Node1 (depth 0 only)."""
        raw_tasks: Any = tasks if tasks not in (None, "") else tasks_json
        if isinstance(raw_tasks, str):
            try:
                parsed_tasks: Any = json.loads(raw_tasks)
            except ValueError:
                parsed_tasks = raw_tasks
        else:
            parsed_tasks = raw_tasks
        args: dict[str, Any] = {
            "tasks": parsed_tasks,
            "tier": tier,
            "model": model,
            "project": project,
            "cwd": cwd,
            "max_steps": max_steps,
            "read_only": read_only,
        }
        return _run("spawn_agents", args, lambda: _wiring.dispatch_delegation(runtime(), "spawn_agents", args))

    @mcp.tool()
    def report_progress(summary: str, thinking: str = "", phase: str = "progress") -> str:
        """Post a concise status or thinking update to the live agent progress board."""
        args = {"summary": summary, "thinking": thinking, "phase": phase}
        return _run("report_progress", args, lambda: _wiring.dispatch_report_progress(args))

    @mcp.tool()
    def post_to_board(text: str, kind: str = "chat") -> str:
        """Post a peer-to-peer coordination message (with optional @handle mentions) to the run board."""
        args = {"text": text, "kind": kind}
        return _run("post_to_board", args, lambda: _wiring.dispatch_post_to_board(args))

    @mcp.tool()
    def read_board(since_seq: int = 0, limit: int = 20, mentions_only: bool = False) -> str:
        """Read recent messages from the shared peer coordination board."""
        args = {"since_seq": since_seq, "limit": limit, "mentions_only": mentions_only}
        return _run("read_board", args, lambda: _wiring.dispatch_read_board(args))


__all__ = ["register"]
