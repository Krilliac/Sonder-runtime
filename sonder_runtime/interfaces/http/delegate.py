"""HTTP boundary for explicit chat delegation requests."""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path

from ...application.agents.delegation import DelegationService


def _text(value, name, maximum=160):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be non-empty bounded text")
    return value.strip()


def _plain_directory(path):
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise PermissionError("creation folders must not be links or junctions")


def dispatch_delegate(service, payload, context, *, state_home, allow_creation=False):
    """Create a lane using the authenticated HTTP context and path policy."""
    if not isinstance(payload, dict):
        raise TypeError("delegate request must be an object")
    forbidden = {
        "principal_id", "author", "context", "lane_id", "workspace_root",
        "allow_creation",
    }
    if forbidden.intersection(payload):
        raise PermissionError("delegation identity is inherited from HTTP context")
    allowed = {"task", "project", "workspace_project", "parent_session_id", "command_id",
               "title", "tier", "max_steps", "max_output_tokens", "max_wall_seconds"}
    if set(payload) - allowed:
        raise ValueError("unknown delegation fields")
    task = _text(payload.get("task"), "task", 12_000)
    project = payload.get("workspace_project")
    if project is None:
        project = payload.get("project")
    if project is not None and (not isinstance(project, str) or len(project) > 2048):
        raise ValueError("project must be bounded text")
    if context.expired or context.cancellation.cancelled:
        raise PermissionError("request authority expired or cancelled")
    parent = payload.get("parent_session_id") or context.session_id
    command_id = _text(payload.get("command_id", "delegate-" + uuid.uuid4().hex), "command_id")
    principal = _text(context.principal_id, "principal_id", 256)
    parent = _text(parent, "parent_session_id") if parent else (
        "delegate-parent-" + uuid.uuid5(uuid.NAMESPACE_URL, principal + "\0" + command_id).hex
    )
    options = {"title": _text(payload.get("title") or task[:120], "title"),
               "tier": _text(payload.get("tier", "code"), "tier", 80)}
    for name, default, ceiling in (("max_steps", 8, 32), ("max_output_tokens", 2048, 16384),
                                    ("max_wall_seconds", 120, 600)):
        value = payload.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ceiling:
            raise ValueError(name + " outside bounded range")
        options[name] = value

    selected = (
        isinstance(project, str)
        and bool(project.strip())
        and project.strip().casefold() != "default"
    )
    if not selected and not allow_creation:
        raise PermissionError("delegated creations require administrator authority")
    lane_id = "lane-" + uuid.uuid5(
        uuid.NAMESPACE_URL, "%s\0%s\0%s" % (principal, parent, command_id)
    ).hex
    if selected:
        effective = context
        workspace = project
        kind = "project"
    else:
        state_root = Path(state_home).expanduser().resolve()
        creations = state_root / "creations"
        _plain_directory(creations)
        creations.mkdir(parents=True, exist_ok=True)
        creations = creations.resolve()
        if creations.parent != state_root:
            raise PermissionError("creation root escaped state home")
        root = creations / lane_id
        _plain_directory(root)
        root.mkdir(parents=True, exist_ok=True)
        if root.resolve().parent != creations:
            raise PermissionError("delegation folder escaped creation root")
        workspace = str(root)
        effective = replace(context, workspace_roots=tuple(context.workspace_roots) + (root,))
        kind = "creation"
    # After dispatch, a transport failure cannot prove that no lane was
    # persisted or started. Never remove a workspace on an uncertain result.
    return DelegationService(service).delegate(
            task,
            context=effective,
            parent_session_id=parent,
            project=project if selected else None,
            command_id=command_id,
            workspace_root=None if selected else workspace,
            lane_id=lane_id,
            folder_kind=kind,
            **options,
        )


__all__ = ["dispatch_delegate"]
