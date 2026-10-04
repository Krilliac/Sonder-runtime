"""Chat-to-agent delegation over the durable interactive lane service.

This adapter owns only the user-facing delegation policy.  Lane persistence,
authorization, normal tool execution, scheduling, and cancellation remain in
``AgentLaneService`` so the HTTP lane API and delegated work share one source
of truth.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

_MAX_TASK_CHARS = 12_000
_MAX_PROJECT_CHARS = 2_048


def _text(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be non-empty bounded text")
    return value.strip()


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _granted_prefixes(workspace_roots) -> tuple[str, ...]:
    """Each granted root as a case-normalized, separator-terminated prefix.

    Both the lexical and the resolved spelling are kept, so a root reached
    through a link still admits the paths a caller writes beneath it.
    """
    prefixes = set()
    for granted in workspace_roots or ():
        for spelling in (os.path.abspath(granted), os.path.realpath(granted)):
            prefixes.add(os.path.join(os.path.normcase(spelling), ""))
    return tuple(prefixes)


def is_creation_workspace(lane, context, state_home) -> bool:
    """Recognize only an exact host-issued creation grant on every tool call.

    Naming a directory under state home is not authority: the original
    operation context must contain this exact lane folder as its own root.
    """
    lane_id = str(lane.get("id") or "")
    if len(lane_id) != 37 or not lane_id.startswith("lane-") or any(
        c not in "0123456789abcdef" for c in lane_id[5:]
    ):
        return False
    creations = Path(state_home).resolve() / "creations"
    expected = creations / lane_id
    root = Path(lane["workspace_root"])
    if root != expected or not root.is_dir():
        return False
    for path in (creations, root):
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            return False
    return (root.resolve() == expected and
            any(Path(grant) == expected for grant in context.workspace_roots))


class DelegationService:
    """Create one durable lane for an explicit chat delegation request.

    ``workspace_root`` is selected by the transport boundary after applying
    its state-home/project policy.  ``parent_session_id`` is normally the
    authenticated chat session.  When a transport has not opened one yet, a
    fresh model parent capability is opened through the existing service; no
    authority is minted by this adapter.  The returned receipt is the normal
    lane receipt with additive delegation metadata for chat acknowledgements.
    """

    def __init__(self, lanes):
        if not callable(getattr(lanes, "spawn", None)):
            raise TypeError("lanes must provide AgentLaneService.spawn")
        self.lanes = lanes

    def delegate(
        self,
        task: str,
        *,
        context,
        parent_session_id: str | None = None,
        project: str | None = None,
        command_id: str | None = None,
        title: str | None = None,
        workspace_root: str | Path | None = None,
        lane_id: str | None = None,
        folder_kind: str | None = None,
        tier: str = "code",
        max_steps: int = 8,
        max_output_tokens: int = 2048,
        max_wall_seconds: int = 120,
    ) -> dict:
        task = _text(task, "task", _MAX_TASK_CHARS)
        if project is not None and (
            not isinstance(project, str) or len(project) > _MAX_PROJECT_CHARS
        ):
            raise ValueError("project must be bounded text")
        if context.expired or context.cancellation.cancelled:
            raise PermissionError("request authority expired or cancelled")

        parent = parent_session_id or getattr(context, "session_id", None)
        if not parent:
            opened = self.lanes.open_model_parent(context)
            parent = opened["parent_session_id"]
        parent = _text(parent, "parent_session_id", 160)

        selected = project.strip() if isinstance(project, str) else ""
        if selected and workspace_root is not None:
            raise ValueError("project and workspace_root are mutually exclusive")
        if selected:
            requested = selected
            folder_kind = "project"
        elif workspace_root is not None:
            requested = os.fspath(workspace_root)
            folder_kind = folder_kind or "creation"
        else:
            raise ValueError("workspace_root is required for delegated work")
        # Containment is decided on the normalized text BEFORE the filesystem
        # is touched. Resolving and probing a caller-named path first made the
        # two refusals below an existence oracle for any folder on the machine
        # ("must be an existing directory" vs "outside caller workspace"). The
        # resolved path is checked again so a link inside the workspace cannot
        # lead out of it.
        lexical = os.path.join(
            os.path.normcase(os.path.abspath(os.path.expanduser(requested))), "",
        )
        if not lexical.startswith(_granted_prefixes(context.workspace_roots)):
            raise PermissionError("delegation workspace is outside caller workspace")
        root = Path(lexical).resolve()
        if not any(
            _inside(root, Path(candidate).resolve())
            for candidate in context.workspace_roots
        ):
            raise PermissionError("delegation workspace is outside caller workspace")
        if not root.is_dir():
            raise ValueError("delegation workspace must be an existing directory")
        effective_context = context

        receipt = self.lanes.spawn(
            command_id=_text(command_id or "delegate-" + uuid.uuid4().hex, "command_id", 160),
            parent_session_id=parent,
            task=task,
            workspace_root=str(root),
            context=effective_context,
            lane_id=lane_id,
            title=title,
            tier=tier,
            max_steps=max_steps,
            max_output_tokens=max_output_tokens,
            max_wall_seconds=max_wall_seconds,
            author="user",
        )
        lane = receipt["lane"]
        lane_id_value = lane["id"]
        folder = lane["workspace_root"]
        return {
            **receipt,
            "delegation": {
                "lane_id": lane_id_value,
                "folder": folder,
                "folder_kind": folder_kind,
                "parent_session_id": lane["parent_session_id"],
                # These are deliberately data-only UI hints.  The app turns
                # them into a tab target; it never receives an executable URL.
                "open": {"surface": "agents", "lane_id": lane_id_value},
                "acknowledgement": (
                    f"Delegated to {lane_id_value} in {folder}. "
                    "Open it in Agents to follow progress."
                ),
            },
        }


def delegate_task(lanes, task: str, *, context, **kwargs) -> dict:
    """Functional seam for transports that do not retain a service object."""
    return DelegationService(lanes).delegate(task, context=context, **kwargs)


__all__ = ["DelegationService", "delegate_task"]
