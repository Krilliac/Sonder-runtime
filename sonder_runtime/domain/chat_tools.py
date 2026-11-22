"""Pure domain models and OpenAI/Ollama tool schemas for interactive chat turns."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sonder_runtime.domain.agents.delegation import BOARD_MESSAGE_KINDS, DELEGATION_TOOLS

TOOL_BACKEND_CONTAINER = "container"
TOOL_BACKEND_HOST = "host"
TOOL_BACKEND_WORKSPACE = "workspace"
TOOL_BACKEND_DISCOVERY = "discovery"

_BIDI_CODEPOINTS: dict[int, str] = {
    0x061C: "U+061C", 0x200B: "U+200B", 0x200C: "U+200C", 0x200D: "U+200D",
    0x200E: "U+200E", 0x200F: "U+200F", 0x202A: "U+202A", 0x202B: "U+202B",
    0x202C: "U+202C", 0x202D: "U+202D", 0x202E: "U+202E", 0x2066: "U+2066",
    0x2067: "U+2067", 0x2068: "U+2068", 0x2069: "U+2069", 0xFEFF: "U+FEFF",
}


def detect_bidi_chars(text: object) -> tuple[bool, list[str]]:
    """Return ``(has_bidi_or_hidden, sorted_codepoint_labels)`` for ``text``."""
    raw = str(text or "")
    if not raw:
        return False, []
    found: set[str] = set()
    for ch in raw:
        code = ord(ch)
        if code in _BIDI_CODEPOINTS:
            found.add(_BIDI_CODEPOINTS[code])
        elif code < 0x20 and ch not in ("\n", "\r", "\t"):
            found.add(f"U+{code:04X}")
    labels = sorted(found)
    return bool(labels), labels


def preview_tool_arguments(
    tool_name: str,
    args: Mapping[str, Any],
    *,
    max_detail_chars: int = 4000,
) -> dict[str, Any]:
    """Return bounded, secret-free preview + bidi metadata for an approval card."""
    bidi_texts: list[str] = []
    detail = ""
    if tool_name == "run_code":
        detail = str(args.get("code") or "")
        bidi_texts.append(detail)
    elif tool_name in {"file_write", "file_edit", "file_delete", "text_patch", "workspace_run"}:
        detail = json.dumps(dict(args), ensure_ascii=False, sort_keys=True)
        bidi_texts.append(detail)
    else:
        bidi_texts.append(json.dumps(dict(args), ensure_ascii=False, sort_keys=True))
    has_bidi, bidi_chars = detect_bidi_chars("\n".join(bidi_texts))
    truncated = len(detail) > max_detail_chars
    shown_detail = (
        detail[:max_detail_chars] + f"\n... (truncated {len(detail) - max_detail_chars} of {len(detail)} chars)"
        if truncated
        else detail
    )
    safe_args: dict[str, Any] = {}
    for key, value in args.items():
        if key in {"token", "approval", "extra_roots", "bypass", "developer_authorized"}:
            continue
        safe_args[key] = value[:512] + "..." if isinstance(value, str) and len(value) > 512 else value
    return {
        "arguments": safe_args,
        "detail": shown_detail,
        "detail_truncated": truncated,
        "detail_chars": len(detail),
        "bidi_warning": has_bidi,
        "bidi_chars": list(bidi_chars),
    }


@dataclass(frozen=True)
class ChatToolBudget:
    """Hard bounds for one interactive chat-tool turn."""

    max_runs: int = 3
    max_tool_calls: int = 24
    max_model_calls: int = 6
    wall_seconds: float = 180.0
    model_output_chars: int = 4000
    user_output_chars: int = 12000
    approval_wait_seconds: float = 120.0


DEFAULT_CHAT_TOOL_BUDGET = ChatToolBudget()


@dataclass(frozen=True)
class ChatToolDefinition:
    """One native tool definition advertised to capable chat models."""

    name: str
    description: str
    parameters: Mapping[str, Any]
    backend: str = TOOL_BACKEND_HOST

    def to_ollama_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters),
            },
        }


CORE_CHAT_TOOL_DEFINITIONS: tuple[ChatToolDefinition, ...] = (
    ChatToolDefinition(
        name="run_code",
        description=(
            "Execute a short standalone code snippet (python, js, powershell, cpp, csharp) "
            "when verifying or computing an answer. Never invent execution output."
        ),
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Source code to execute."},
                "language": {
                    "type": "string",
                    "enum": ["python", "js", "powershell", "cpp", "csharp"],
                    "default": "python",
                },
                "stdin": {"type": "string", "default": ""},
                "timeout": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10},
            },
            "required": ["code"],
        },
        backend=TOOL_BACKEND_HOST,
    ),
    ChatToolDefinition(
        name="tool_search",
        description=(
            "Discover additional workspace, git, test, lint, data, or memory tools "
            "and their JSON argument schemas by query or exact tool name."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword such as read, edit, test, git."},
                "name": {"type": "string", "description": "Exact tool name to inspect."},
            },
        },
        backend=TOOL_BACKEND_DISCOVERY,
    ),
    ChatToolDefinition(
        name="file_read",
        description="Read a text file inside an authorized project directory.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "required": ["path"],
        },
        backend=TOOL_BACKEND_WORKSPACE,
    ),
    ChatToolDefinition(
        name="directory_tree",
        description="List files and directories under a project path.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
                "depth": {"type": "integer", "minimum": 1, "maximum": 5, "default": 2},
            },
        },
        backend=TOOL_BACKEND_WORKSPACE,
    ),
    ChatToolDefinition(
        name="text_search",
        description="Search for text or a symbol across project files.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "root": {"type": "string", "default": "."},
                "glob": {"type": "string", "default": ""},
            },
            "required": ["query"],
        },
        backend=TOOL_BACKEND_WORKSPACE,
    ),
    ChatToolDefinition(
        name="file_write",
        description="Create, overwrite, or append a file in an authorized directory (gated).",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "mode": {"type": "string", "enum": ["create", "overwrite", "append"], "default": "create"},
            },
            "required": ["path", "content"],
        },
        backend=TOOL_BACKEND_WORKSPACE,
    ),
    ChatToolDefinition(
        name="file_edit",
        description="Replace a unique string match inside an authorized file (gated).",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old": {"type": "string"},
                "new": {"type": "string"},
            },
            "required": ["path", "old", "new"],
        },
        backend=TOOL_BACKEND_WORKSPACE,
    ),
    ChatToolDefinition(
        name="test_run",
        description="Run repository tests with pytest/jest/vitest/cargo/go/dotnet (gated).",
        parameters={
            "type": "object",
            "properties": {
                "root": {"type": "string", "default": "."},
                "path": {"type": "string", "default": ""},
                "pattern": {"type": "string", "default": ""},
            },
        },
        backend=TOOL_BACKEND_HOST,
    ),
    ChatToolDefinition(
        name="report_progress",
        description="Post a concise status or thinking update to the shared live agent progress board.",
        parameters={
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Short status or milestone update."},
                "thinking": {"type": "string", "default": ""},
                "phase": {"type": "string", "default": "progress"},
            },
            "required": ["summary"],
        },
        backend=TOOL_BACKEND_HOST,
    ),
    ChatToolDefinition(
        name="post_to_board",
        description=(
            "Post a message to the shared peer coordination board (supports @handle mentions "
            "for handoffs, findings, praise, complaints/pushback, or brief jokes)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Message for teammates; use @handle to mention a peer."},
                "kind": {
                    "type": "string",
                    "enum": sorted(BOARD_MESSAGE_KINDS),
                    "default": "chat",
                },
            },
            "required": ["text"],
        },
        backend=TOOL_BACKEND_HOST,
    ),
    ChatToolDefinition(
        name="read_board",
        description="Read recent messages from the shared peer coordination board (optionally filtered by since_seq or mentions).",
        parameters={
            "type": "object",
            "properties": {
                "since_seq": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                "mentions_only": {"type": "boolean", "default": False},
            },
        },
        backend=TOOL_BACKEND_DISCOVERY,
    ),
    ChatToolDefinition(
        name="delegate_task",
        description=(
            "Delegate one bounded subtask to a depth-1 child agent (depth 0 only; "
            "children run independently across Main/Node1 and cannot delegate further)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Subtask instruction for the child agent."},
                "name": {"type": "string", "default": "", "description": "Optional handle such as coder, scout, security, skeptic."},
                "role": {"type": "string", "default": ""},
                "tier": {"type": "string", "default": ""},
                "model": {"type": "string", "default": ""},
                "project": {"type": "string", "default": ""},
                "cwd": {"type": "string", "default": ""},
                "max_steps": {"type": "integer", "minimum": 1, "maximum": 16, "default": 8},
                "read_only": {"type": "boolean"},
            },
            "required": ["task"],
        },
        backend=TOOL_BACKEND_HOST,
    ),
    ChatToolDefinition(
        name="spawn_agents",
        description=(
            "Spawn multiple bounded depth-1 child agents in parallel across Main and Node1 "
            "(depth 0 only; children coordinate via the peer board and cannot delegate further)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "List of task strings or {task, name, role, personality, tier, model, project, cwd, max_steps, read_only} objects.",
                    "items": {"type": "object"},
                },
                "tier": {"type": "string", "default": ""},
                "model": {"type": "string", "default": ""},
                "project": {"type": "string", "default": ""},
                "cwd": {"type": "string", "default": ""},
                "max_steps": {"type": "integer", "minimum": 1, "maximum": 16, "default": 8},
                "read_only": {"type": "boolean"},
            },
            "required": ["tasks"],
        },
        backend=TOOL_BACKEND_HOST,
    ),
)

_BACKEND_BY_TOOL = {item.name: item.backend for item in CORE_CHAT_TOOL_DEFINITIONS}


@dataclass(frozen=True)
class ChatToolRecord:
    """Structured receipt record for one tool invocation inside a chat turn."""

    call_id: str
    tool: str
    status: str
    backend: str
    duration_ms: int = 0
    exit_code: int | None = None
    timed_out: bool = False
    truncated: bool = False
    bidi_warning: bool = False
    bidi_chars: tuple[str, ...] = ()
    stdout_preview: str = ""
    stderr_preview: str = ""
    reason: str = ""

    def to_receipt(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "call_id": self.call_id,
            "tool": self.tool,
            "status": self.status,
            "backend": self.backend,
            "duration_ms": max(0, int(self.duration_ms)),
            "timed_out": bool(self.timed_out),
            "truncated": bool(self.truncated),
            "bidi_warning": bool(self.bidi_warning),
        }
        if self.exit_code is not None:
            payload["exit_code"] = int(self.exit_code)
        if self.bidi_chars:
            payload["bidi_chars"] = list(self.bidi_chars)
        if self.stdout_preview:
            payload["stdout_preview"] = self.stdout_preview
        if self.stderr_preview:
            payload["stderr_preview"] = self.stderr_preview
        if self.reason:
            payload["reason"] = self.reason
        return payload


def tool_backend_label(tool_name: str, *, container_active: bool = False) -> str:
    name = str(tool_name or "").strip()
    if name == "run_code":
        return TOOL_BACKEND_CONTAINER if container_active else TOOL_BACKEND_HOST
    return _BACKEND_BY_TOOL.get(name, TOOL_BACKEND_WORKSPACE)


def parse_tool_arguments(raw: Any) -> dict[str, Any]:
    """Normalize provider tool-call arguments into a dict (or empty dict on malformed)."""
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            loaded = json.loads(text)
            return dict(loaded) if isinstance(loaded, Mapping) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def chat_tools_payload(
    *,
    allowed_names: Sequence[str] | frozenset[str] | None = None,
    extra_definitions: Sequence[ChatToolDefinition] = (),
    depth: int = 0,
) -> list[dict[str, Any]]:
    """Render bounded Ollama/OpenAI tool definitions for ``allowed_names``."""
    items = [*CORE_CHAT_TOOL_DEFINITIONS, *extra_definitions]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    allow = None if allowed_names is None else set(allowed_names)
    for entry in items:
        if entry.name in seen:
            continue
        if int(depth) >= 1 and entry.name in DELEGATION_TOOLS:
            continue
        if allow is not None and entry.name not in allow:
            continue
        seen.add(entry.name)
        out.append(entry.to_ollama_tool())
        if len(out) >= 64:
            break
    return out


__all__ = [
    "CORE_CHAT_TOOL_DEFINITIONS",
    "ChatToolBudget",
    "ChatToolDefinition",
    "ChatToolRecord",
    "DEFAULT_CHAT_TOOL_BUDGET",
    "TOOL_BACKEND_CONTAINER",
    "TOOL_BACKEND_DISCOVERY",
    "TOOL_BACKEND_HOST",
    "TOOL_BACKEND_WORKSPACE",
    "chat_tools_payload",
    "detect_bidi_chars",
    "parse_tool_arguments",
    "preview_tool_arguments",
    "tool_backend_label",
]
