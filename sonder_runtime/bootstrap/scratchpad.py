"""Composition helpers and MCP tool registration for Sonder's persistent scratchpad."""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, TypeVar

from sonder_runtime.adapters.persistence.scratchpad_store import (
    ScratchpadError,
    ScratchpadStore,
)
from sonder_runtime.adapters.security import approval_ledger as _approval_ledger
from sonder_runtime.application.context import current_operation_context
from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.memory.scratchpad import ScratchpadPolicy
from sonder_runtime.platform import paths as runtime_paths
from sonder_runtime.platform.config import load_config
from sonder_runtime.platform.logging import redactor_for_config

_CONFIG_GETTER: Callable[[], Any] | None = None
_F = TypeVar("_F", bound=Callable[..., Any])


class _McpRegistrar(Protocol):
    def tool(self) -> Callable[[_F], _F]: ...


def resolve_owner(explicit_owner: str | None = None) -> str:
    """Resolve the effective caller owner without trusting model-supplied inputs."""
    if explicit_owner and str(explicit_owner).strip():
        return str(explicit_owner).strip()
    op = current_operation_context()
    if op is not None:
        for attr in ("owner_id", "principal", "account_id"):
            val = getattr(op, attr, None)
            if val and str(val).strip():
                return str(val).strip()
    return str(_approval_ledger.current_owner() or "local")


def get_store(*, config: Any = None, home: str | Path | None = None) -> ScratchpadStore:
    """Return a ScratchpadStore anchored to the active Sonder state home."""
    if config is None and home is None and _CONFIG_GETTER is not None:
        config = _CONFIG_GETTER()
    if config is None:
        candidate_home = Path(home) if home is not None else runtime_paths.default_home()
        candidate = candidate_home / "sonder.toml"
        config = load_config(candidate if candidate.is_file() else None)
    if home is None:
        home = getattr(getattr(config, "state", None), "home", "") or runtime_paths.default_home()
    return ScratchpadStore(home, ScratchpadPolicy(), redactor=redactor_for_config(config))


def register_tools(
    mcp: _McpRegistrar,
    *,
    config: Any = None,
    home: str | Path | None = None,
    config_getter: Callable[[], Any] | None = None,
) -> tuple[Callable[..., str], Callable[..., str], Callable[..., str]]:
    """Register ``scratchpad_read``, ``scratchpad_write``, and ``scratchpad_delete``."""
    global _CONFIG_GETTER
    if config_getter is not None:
        _CONFIG_GETTER = config_getter

    @mcp.tool()
    def scratchpad_read(
        name: str = "",
        query: str = "",
        tag: str = "",
        start_line: int = 1,
        max_lines: int = 200,
    ) -> str:
        """Read, list, or search your persistent free-form Markdown scratchpad notes.

        Pass ``name`` to read a specific note (bounded by ``start_line``/``max_lines``),
        ``query`` to search across notes, or leave both empty to list all notes
        (optionally filtered by ``tag``). Notes are owner-scoped and persist
        across restarts as reference working material (never system policy).
        """
        owner = resolve_owner()
        store = get_store(config=config, home=home)
        try:
            if name and str(name).strip():
                data = store.read(owner, name, start_line=start_line, max_lines=max_lines)
            elif query and str(query).strip():
                data = store.search(owner, query)
            else:
                data = store.list_notes(owner, tag=tag)
        except (TypeError, ValueError, ScratchpadError) as exc:
            raise InvalidInput(str(exc)) from exc
        return json.dumps(data, ensure_ascii=False, sort_keys=True)

    @mcp.tool()
    def scratchpad_write(
        name: str,
        content: str,
        mode: str = "replace",
        tags: list[str] | None = None,
    ) -> str:
        """Create, replace, or append to a persistent free-form Markdown scratchpad note.

        Notes are stored per owner under ``<sonder_home>/scratchpad/`` with secret
        redaction and size/count quotas so working plans, drafts, and investigation
        notes survive restarts. Use ``mode='replace'`` (default) or ``mode='append'``.
        """
        owner = resolve_owner()
        store = get_store(config=config, home=home)
        try:
            data = store.write(owner, name, content, mode=mode, tags=tags)
        except (TypeError, ValueError, ScratchpadError) as exc:
            raise InvalidInput(str(exc)) from exc
        return json.dumps(data, ensure_ascii=False, sort_keys=True)

    @mcp.tool()
    def scratchpad_delete(name: str) -> str:
        """Delete one named note from your persistent scratchpad."""
        owner = resolve_owner()
        store = get_store(config=config, home=home)
        try:
            data = store.delete(owner, name)
        except (TypeError, ValueError, ScratchpadError) as exc:
            raise InvalidInput(str(exc)) from exc
        return json.dumps(data, ensure_ascii=False, sort_keys=True)

    return scratchpad_read, scratchpad_write, scratchpad_delete


__all__ = ["get_store", "register_tools", "resolve_owner"]
