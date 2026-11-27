"""The tools a chat model can call in one turn, stated once and last.

Served chat told models they had tools they could not call: the runtime
identity named "tools", the standing profile named workspace tools, and the
file-turn guidance said Sonder "can use its scoped read-only tools". A hosted
model then told a user its tools "aren't reaching my context". None of those
tools is callable from a plain chat turn.

A ``TurnToolManifest`` is the one authoritative statement of what this turn's
model can call. It renders one system block that the chat route appends after
every other section, so it overrides any earlier text that names a tool.

Phase 0 builds only empty manifests: plain chat calls no tool. Phase 1 adds
entries such as ``CallableTool("run_code", "python; each run needs the user's
approval; container or host")`` without changing the block's framing or its
position.

The block names no machine detail (no path, host, hardware or model file), so
it is sent to hosted and cloud models as well as to local ones.

Pure: standard library and domain only, no I/O, no module state. Both types
are frozen dataclasses holding only strings, booleans and tuples, so a value
is immutable and safe to share between threads.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sonder_runtime.domain.prompt_composition import join_system_parts

HEADER = (
    "Tools in this turn (authoritative: this overrides any instruction above "
    "about which tools you can call):"
)
NO_TOOLS_LINE = (
    "- No tools are callable in this turn. You cannot run code or read files, "
    "and you cannot browse yourself; the host may route a web request. Never "
    "say you ran, tested or verified anything."
)
CALLABLE_PREFIX = "- Callable in this turn: "
CALLABLE_ONLY_LINE = (
    "- Call nothing else. Say you ran, tested or verified something only when "
    "a tool result in this turn shows it."
)
PROACTIVE_TOOL_LINE = (
    "- Reach for callable tools proactively whenever inspecting files, running "
    "or verifying code/math/logic, searching memory/web, keeping working notes "
    "in scratchpad_write/scratchpad_read, or delegating helps finish the task "
    "accurately rather than guessing."
)
DYNAMIC_DISCOVERY_LINE = (
    "- Need another Sonder capability (git/repo, builds/linters/formatters, "
    "file_find/patches/archives, tasks/checklists, playbooks, documents/media, "
    "data/sqlite, web, artifacts)? Call tool_search(query=...) to discover and "
    "load it into this turn, then call it directly. Say you ran, tested or "
    "verified something only when a tool result in this turn shows it."
)
USER_RUN_LINE = (
    "- The user can run a fenced code block from your answer themselves by "
    "sending /run (outside auto mode it first needs their approval)."
)

_SECTION_BREAK = "\n\n"
_SECTION_SEP = re.compile(r"\r?\n(?:[ \t]*\r?\n)+")


def _one_line(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise TypeError("%s must be a string" % what)
    if "\n" in value or "\r" in value:
        raise ValueError("%s must be one line" % what)
    return value


@dataclass(frozen=True)
class CallableTool:
    """One tool the model may call this turn.

    ``name`` is the tool name the model calls; ``detail`` is a short,
    machine-neutral note (language, approval, backend kind). Both are single
    lines so the rendered block stays one section.
    """

    name: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not _one_line(self.name, "tool name").strip():
            raise ValueError("tool name must not be empty")
        _one_line(self.detail, "tool detail")

    def label(self) -> str:
        return "%s (%s)" % (self.name, self.detail) if self.detail else self.name


@dataclass(frozen=True)
class TurnToolManifest:
    """The tools callable in one chat turn, and whether ``/run`` is the user's.

    ``tools`` is empty in phase 0. ``user_run_command`` is True only when the
    caller is developer-authorized and the permission mode is not plan; the
    producer decides that, this type only renders it.
    """

    tools: tuple[CallableTool, ...] = ()
    user_run_command: bool = False
    dynamic_discovery: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.tools, tuple) or not all(
            isinstance(tool, CallableTool) for tool in self.tools
        ):
            raise TypeError("tools must be a tuple of CallableTool")
        names = [tool.name for tool in self.tools]
        if len(set(names)) != len(names):
            raise ValueError("a tool is listed twice")
        if not isinstance(self.user_run_command, bool):
            raise TypeError("user_run_command must be a bool")
        if not isinstance(self.dynamic_discovery, bool):
            raise TypeError("dynamic_discovery must be a bool")

    def render(self) -> str:
        """The block: the header, then one line per statement, no blank line."""
        lines = [HEADER]
        if self.tools:
            lines.append(CALLABLE_PREFIX + "; ".join(t.label() for t in self.tools) + ".")
            if self.dynamic_discovery:
                lines.append(PROACTIVE_TOOL_LINE)
                lines.append(DYNAMIC_DISCOVERY_LINE)
            else:
                lines.append(CALLABLE_ONLY_LINE)
        else:
            lines.append(NO_TOOLS_LINE)
        if self.user_run_command:
            lines.append(USER_RUN_LINE)
        return "\n".join(lines)


def _split(system: str) -> tuple[str, str]:
    """``system`` without its manifest blocks, and the last block found.

    A block starts at a section boundary with ``HEADER`` (allowing CRLF
    blank-line breaks and leading horizontal whitespace on the header line) and
    runs to the next blank line or the end. Header text that does not open a
    section is left alone.
    """
    pieces = _SECTION_SEP.split(system)
    seps = _SECTION_SEP.findall(system)
    kept_block = ""
    out: list[str] = []
    prev_kept = -1
    for index, piece in enumerate(pieces):
        stripped = piece.lstrip(" \t\r\n") if index == 0 else piece.lstrip(" \t\r")
        if stripped.startswith(HEADER):
            kept_block = stripped
            continue
        if not piece:
            continue
        if out:
            out.append(seps[index - 1] if prev_kept == index - 1 else _SECTION_BREAK)
        out.append(piece)
        prev_kept = index
    if not kept_block:
        return system, ""
    return "".join(out), kept_block


def with_manifest(system: object, manifest: TurnToolManifest) -> str:
    """``system`` with any earlier block removed and ``manifest``'s appended last.

    Idempotent: rebuilding a turn's system (an escalation rung, a repair)
    never stacks a second block.
    """
    if not isinstance(manifest, TurnToolManifest):
        raise TypeError("manifest must be a TurnToolManifest")
    base, _ = _split(system if isinstance(system, str) else "")
    return join_system_parts(base, manifest.render())


def keep_last(system: object, block: str) -> object:
    """``system`` with every header-led section removed and ``block`` last.

    For a step that appends its own section after the block was added (the
    learning route's playbook selection). ``block`` is the turn's own block,
    taken with ``manifest_block`` from the system before that step ran, so a
    header-led section the step added can neither displace nor impersonate
    it: it is dropped. An empty ``block`` (a system that never had one, such
    as a non-chat caller of a shared path) returns ``system`` unchanged.
    """
    if not isinstance(system, str) or not block:
        return system
    if not isinstance(block, str) or not block.startswith(HEADER):
        raise ValueError("block must be a rendered manifest block")
    base, _ = _split(system)
    return join_system_parts(base, block)


def manifest_block(system: object) -> str:
    """The manifest block in ``system`` ("" when it has none).

    With several header-led sections the last one is returned: on a system
    built by ``with_manifest`` that is the turn's own block.
    """
    if not isinstance(system, str):
        return ""
    return _split(system)[1]


__all__ = [
    "CALLABLE_ONLY_LINE",
    "CallableTool",
    "DYNAMIC_DISCOVERY_LINE",
    "HEADER",
    "NO_TOOLS_LINE",
    "PROACTIVE_TOOL_LINE",
    "TurnToolManifest",
    "USER_RUN_LINE",
    "keep_last",
    "manifest_block",
    "with_manifest",
]
