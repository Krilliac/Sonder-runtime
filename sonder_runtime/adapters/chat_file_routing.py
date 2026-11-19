"""Bind file chat to existing guarded agents using host-selected directories.

The legacy host is injected at composition; packaged code never imports server.
This adapter grants no permissions and never resolves model text as authority.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import sonder_runtime.adapters.restricted_tool_lane as restricted_tool_lane
from sonder_runtime.platform import paths

if TYPE_CHECKING:
    import os

INSPECTION_TOOLS = frozenset({
    "workspace_inventory", "directory_tree", "file_find", "text_search",
    "file_read", "file_read_range", "repository_symbol_index",
})


def creation_folder(*, state_home: str | os.PathLike[str] | None = None, lane_id: str | None = None) -> Path:
    """A prospective directory only: callers must authorize before creating it."""
    home = Path(state_home if state_home is not None else paths.default_home()).resolve()
    name = lane_id or "chat-" + uuid4().hex
    if not name or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in name):
        raise ValueError("invalid creation lane id")
    root = home / "creations"
    candidate = root / name
    if root.resolve() != root or candidate.resolve() != candidate:
        raise ValueError("creation directory must not redirect outside its assigned path")
    return candidate


def suggested_folder(host: Any, project: str = "") -> str:
    """Describe the destination without creating state for an ordinary chat turn."""
    return host.served_work_project(project) or str(paths.default_home() / "creations" / "<lane-id>")


def route_file_request(host: Any, prompt: str, mode: str, project: str, tier: str, *, state_home: str | os.PathLike[str] | None = None, pinned: bool = False) -> tuple[str, str]:
    """Run an already-classified file request; retain every existing tool gate."""
    if mode not in {"inspection", "workbench"}:
        return "Refused: unknown file chat lane.", tier
    if host.unsafe_lab.active():
        return "Refused: file chat requires the guarded tool lane; unsafe lab removes its scope.", tier
    supplied = str(project or "").strip()
    default = supplied.lower() in {"", "default", "none"}
    scoped = host.served_work_project(supplied) if not default else ""
    if not scoped and not default:
        return "Refused: select an existing project directory inside the configured file roots.", tier
    if mode == "inspection":
        # Match the existing relative read tools for the default workspace. This
        # fallback is read-only; a default write always gets a creations lane.
        scoped = scoped or host.served_work_project(str(host.file_ops.workspace_root()))
        if not scoped:
            return "Refused: no authorized workspace is available for inspection.", tier
        output = host._agent_impl(
            prompt, tier=tier, max_steps=8, allow_web=False,
            require_file_evidence=True, read_only=True, include_evidence=True,
            project=scoped, tool_allowlist=INSPECTION_TOOLS, allow_location=False,
        )
        return "project: %s\n\n%s" % (scoped, output), tier
    if not scoped:
        try:
            destination = creation_folder(state_home=state_home)
        except (OSError, ValueError) as exc:
            return "Refused: cannot select a confined creation directory: %s" % exc, tier
        arguments = {"path": str(destination), "parents": True}
        refusal = host._agent_permission_gate_error("directory_create", arguments)
        if refusal:
            return refusal, tier
        result = host.directory_create(**arguments)
        if not destination.is_dir() or destination.resolve() != destination:
            return "Refused: creation directory was not established. %s" % result, tier
        scoped = str(destination)
    if pinned:
        # A chat turn runs this workbench call, so it never opens a restricted lane.
        with restricted_tool_lane.suppressed():
            output = host.workbench_agent(
                prompt=prompt, tier=tier, max_steps=12, allow_web=False, project=scoped, allow_location=False,
            )
    else:
        output, tier = host._workbench_agent_escalating(
            prompt, tier, max_steps=12, allow_web=False, project=scoped, allow_location=False,
        )
    return "project: %s\n\n%s" % (scoped, output), tier
