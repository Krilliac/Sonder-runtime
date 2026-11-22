"""This chat turn's tool manifest, built from the request's authority and mode.

``application/chat/tool_manifest.py`` owns the manifest and its block; this
adapter supplies the two facts it needs from host policy:

* the tools callable in this turn: none in phase 0, plain chat calls no tool;
* whether the user may run a code block with ``/run``. ``/run`` is in
  ``DANGEROUS_HTTP_SLASH_COMMANDS``, so only a developer-authorized caller may
  use it, and the hint is also left out in plan mode. Outside auto mode a
  served ``/run`` waits for the user's approval, and the hint says so.
  Developer authority is read from the caller role the HTTP handler binds for
  the request it serves (``role_tool_policy``): ``None`` there is exactly
  ``_developer_authorized`` for a served context. An unbound context gets no
  hint (fail closed).

Only the two served chat routes call ``chat_system``: the plain turn
(``server._answer_with_history_impl``) and the ``response_format`` turn
(``server.structured_answer_with_history``). Agent, workbench and other tool
lanes compose their systems without it, so they never receive the "no tools"
block.

Stateless: every call reads the current context and the process-wide
permission mode; nothing is cached. Thread affinity: call on the thread that
serves the request, where the caller role was bound.
"""
from __future__ import annotations

from typing import Any

from sonder_runtime.adapters.security import role_tool_policy
from sonder_runtime.adapters.security.permission_policy import permission_policy
from sonder_runtime.application.chat import tool_loop, tool_manifest
from sonder_runtime.application.chat.honesty import no_tools_system

# permission_modes.PLAN. The provider exposes no accessor for one mode name;
# tests/test_chat_tool_manifest.py pins the two together.
PLAN_MODE = "plan"


def user_may_run_code() -> bool:
    """True when the ``/run`` hint is truthful for this caller and mode."""
    if not role_tool_policy.caller_bound_unrestricted():
        return False
    return permission_policy.current_mode() != PLAN_MODE


def for_chat_turn() -> tool_manifest.TurnToolManifest:
    """The manifest for one plain or tool-enabled chat turn."""
    active = tool_loop.current_turn()
    if active is not None and isinstance(active.manifest, tool_manifest.TurnToolManifest):
        return active.manifest
    return tool_manifest.TurnToolManifest(user_run_command=user_may_run_code())


def chat_system(system: str, prompt: str, *, file_guidance: bool) -> str:
    """A chat turn's system: file-turn guidance when tool-less, then the manifest last."""
    manifest = for_chat_turn()
    if file_guidance and not manifest.tools:
        system = no_tools_system(system, prompt)
    return tool_manifest.with_manifest(system, manifest)


def manifest_last(before: object, augmented: tuple[Any, Any]) -> tuple[Any, Any]:
    """``(system, extra)`` with the turn's own manifest block put back last.

    ``before`` is the system as it was before the step ran; ``augmented`` is
    the step's ``(system, selection)`` pair (the playbook selection in
    ``server._answer``). The block kept is the one ``before`` carried, and any
    header-led section the step added is dropped, so appended text cannot
    forge the turn's tools statement. A ``before`` with no block (a non-chat
    caller of ``_answer``) leaves the pair unchanged.
    """
    system, extra = augmented
    return tool_manifest.keep_last(system, tool_manifest.manifest_block(before)), extra


__all__ = ["PLAN_MODE", "chat_system", "for_chat_turn", "manifest_last", "user_may_run_code"]
