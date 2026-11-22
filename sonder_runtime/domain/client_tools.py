"""Pure domain definitions and slash resolution for client-side tool delegation."""
from __future__ import annotations

import json
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .chat_tools import ChatToolDefinition

TOOL_BACKEND_CLIENT = "client"
CLIENT_EXEC_HEADER = "X-Sonder-Client-Exec"

_CREDENTIAL_ARG_KEYS: frozenset[str] = frozenset({
    "token",
    "approval",
    "extra_roots",
    "secret",
    "api_key",
    "password",
})

CLIENT_DELEGATABLE_TOOL_DEFINITIONS: tuple[ChatToolDefinition, ...] = (
    ChatToolDefinition(
        name="run_code",
        description=(
            "Execute a short standalone code snippet on the user's client machine "
            "(python, js, powershell, bash, cpp, csharp) and return stdout/stderr."
        ),
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Source code to execute."},
                "language": {
                    "type": "string",
                    "enum": ["python", "js", "powershell", "bash", "cpp", "csharp"],
                    "default": "python",
                },
                "stdin": {"type": "string", "default": ""},
                "timeout": {"type": "integer", "minimum": 1, "maximum": 60, "default": 15},
            },
            "required": ["code"],
        },
        backend=TOOL_BACKEND_CLIENT,
    ),
    ChatToolDefinition(
        name="file_read",
        description="Read a text file on the user's client machine.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "required": ["path"],
        },
        backend=TOOL_BACKEND_CLIENT,
    ),
    ChatToolDefinition(
        name="file_write",
        description="Create, overwrite, or append a file on the user's client machine.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "mode": {"type": "string", "enum": ["create", "overwrite", "append"], "default": "overwrite"},
            },
            "required": ["path", "content"],
        },
        backend=TOOL_BACKEND_CLIENT,
    ),
    ChatToolDefinition(
        name="file_edit",
        description="Replace a unique string match inside a file on the user's client machine.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old": {"type": "string"},
                "new": {"type": "string"},
            },
            "required": ["path", "old", "new"],
        },
        backend=TOOL_BACKEND_CLIENT,
    ),
    ChatToolDefinition(
        name="file_delete",
        description="Delete a file on the user's client machine.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
            },
            "required": ["path"],
        },
        backend=TOOL_BACKEND_CLIENT,
    ),
    ChatToolDefinition(
        name="file_find",
        description="Find files matching a glob or pattern on the user's client machine.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "default": "*"},
                "root": {"type": "string", "default": "."},
            },
        },
        backend=TOOL_BACKEND_CLIENT,
    ),
    ChatToolDefinition(
        name="directory_create",
        description="Create a directory on the user's client machine.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
            },
            "required": ["path"],
        },
        backend=TOOL_BACKEND_CLIENT,
    ),
    ChatToolDefinition(
        name="directory_tree",
        description="List files and directories under a path on the user's client machine.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
                "depth": {"type": "integer", "minimum": 1, "maximum": 5, "default": 2},
            },
        },
        backend=TOOL_BACKEND_CLIENT,
    ),
    ChatToolDefinition(
        name="text_search",
        description="Search for text or a symbol across files on the user's client machine.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "root": {"type": "string", "default": "."},
                "glob": {"type": "string", "default": ""},
            },
            "required": ["query"],
        },
        backend=TOOL_BACKEND_CLIENT,
    ),
    ChatToolDefinition(
        name="run_program",
        description="Run a command or program on the user's client machine.",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string", "default": ""},
                "timeout": {"type": "integer", "minimum": 1, "maximum": 120, "default": 30},
            },
            "required": ["command"],
        },
        backend=TOOL_BACKEND_CLIENT,
    ),
    ChatToolDefinition(
        name="test_run",
        description="Run repository tests on the user's client machine.",
        parameters={
            "type": "object",
            "properties": {
                "root": {"type": "string", "default": "."},
                "path": {"type": "string", "default": ""},
                "pattern": {"type": "string", "default": ""},
            },
        },
        backend=TOOL_BACKEND_CLIENT,
    ),
    ChatToolDefinition(
        name="git_status",
        description="Inspect git working tree status on the user's client machine.",
        parameters={
            "type": "object",
            "properties": {"cwd": {"type": "string", "default": "."}},
        },
        backend=TOOL_BACKEND_CLIENT,
    ),
    ChatToolDefinition(
        name="git_diff",
        description="Inspect git diff on the user's client machine.",
        parameters={
            "type": "object",
            "properties": {
                "cwd": {"type": "string", "default": "."},
                "path": {"type": "string", "default": ""},
                "staged": {"type": "boolean", "default": False},
            },
        },
        backend=TOOL_BACKEND_CLIENT,
    ),
    ChatToolDefinition(
        name="git_log",
        description="Inspect recent git commits on the user's client machine.",
        parameters={
            "type": "object",
            "properties": {
                "cwd": {"type": "string", "default": "."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
        },
        backend=TOOL_BACKEND_CLIENT,
    ),
    ChatToolDefinition(
        name="git_branch",
        description="List or inspect git branches on the user's client machine.",
        parameters={
            "type": "object",
            "properties": {"cwd": {"type": "string", "default": "."}},
        },
        backend=TOOL_BACKEND_CLIENT,
    ),
)

CLIENT_DELEGATABLE_TOOLS: frozenset[str] = frozenset(
    item.name for item in CLIENT_DELEGATABLE_TOOL_DEFINITIONS
)

_RISK_BY_TOOL: dict[str, str] = {
    "file_read": "read_only",
    "file_find": "read_only",
    "directory_tree": "read_only",
    "text_search": "read_only",
    "git_status": "read_only",
    "git_diff": "read_only",
    "git_log": "read_only",
    "git_branch": "read_only",
    "file_write": "write",
    "file_edit": "write",
    "file_delete": "write",
    "directory_create": "write",
    "run_code": "execute",
    "run_program": "execute",
    "test_run": "execute",
}

_SLASH_ALIASES: dict[str, str] = {
    "/run": "run_code",
    "/read": "file_read",
    "/write": "file_write",
    "/edit": "file_edit",
    "/delete": "file_delete",
    "/mkdir": "directory_create",
    "/find": "file_find",
    "/files": "directory_tree",
    "/tree": "directory_tree",
    "/folders": "directory_tree",
    "/search": "text_search",
    "/grep": "text_search",
    "/runprogram": "run_program",
    "/runscript": "run_program",
    "/runproject": "run_program",
    "/test": "test_run",
    "/git_status": "git_status",
    "/git_diff": "git_diff",
    "/git_log": "git_log",
    "/git_branch": "git_branch",
}

_RUN_CODE_LANGUAGES: frozenset[str] = frozenset({
    "python", "py", "js", "javascript", "node", "powershell", "ps1", "bash", "sh", "cpp", "c++", "csharp", "cs",
})
_LANG_CANONICAL: dict[str, str] = {
    "py": "python",
    "javascript": "js",
    "node": "js",
    "ps1": "powershell",
    "sh": "bash",
    "c++": "cpp",
    "cs": "csharp",
}


def is_client_delegatable(tool_name: object) -> bool:
    """True only when ``tool_name`` is explicitly eligible for client-side execution."""
    clean = str(tool_name or "").strip().lstrip("/")
    if not clean or clean.startswith("admin_"):
        return False
    return clean in CLIENT_DELEGATABLE_TOOLS


def client_tool_risk(tool_name: object) -> str:
    clean = str(tool_name or "").strip().lstrip("/")
    return _RISK_BY_TOOL.get(clean, "execute")


def client_tools_payload() -> list[dict[str, Any]]:
    """OpenAI/Ollama function tool definitions exposed to client-exec turns."""
    return [defn.to_ollama_tool() for defn in CLIENT_DELEGATABLE_TOOL_DEFINITIONS]


def sanitize_client_args(args: Mapping[str, Any] | None) -> dict[str, Any]:
    """Strip host credential/path-escape keys before handing arguments to the client PC."""
    if not args:
        return {}
    clean: dict[str, Any] = {}
    for key, value in args.items():
        key_text = str(key)
        if key_text in _CREDENTIAL_ARG_KEYS:
            continue
        clean[key_text] = value
    return clean


@dataclass(frozen=True)
class ClientSlashPlan:
    tool: str
    args: dict[str, Any]
    risk: str


def _parse_run_code_arg(arg: str) -> dict[str, Any]:
    text = arg.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lang_tag = lines[0][3:].strip().lower()
        body_lines = lines[1:]
        if body_lines and body_lines[-1].strip() == "```":
            body_lines = body_lines[:-1]
        language = _LANG_CANONICAL.get(lang_tag, lang_tag) or "python"
        if language not in {"python", "js", "powershell", "bash", "cpp", "csharp"}:
            language = "python"
        return {"code": "\n".join(body_lines), "language": language}
    first_line, _, rest = text.partition("\n")
    tokens = first_line.split(None, 1)
    if tokens and tokens[0].lower() in _RUN_CODE_LANGUAGES and (rest or len(tokens) > 1):
        lang_raw = tokens[0].lower()
        language = _LANG_CANONICAL.get(lang_raw, lang_raw)
        code = ((tokens[1] + "\n") if len(tokens) > 1 else "") + rest
        return {"code": code.rstrip("\n"), "language": language}
    return {"code": text, "language": "python"}


def resolve_client_slash_command(prompt: object) -> ClientSlashPlan | None:
    """Resolve a client-delegatable slash command into ``ClientSlashPlan`` or ``None``."""
    stripped = str(prompt or "").strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped.split(None, 1)
    head = parts[0].split("@", 1)[0].lower()
    arg = parts[1] if len(parts) > 1 else ""
    tool = _SLASH_ALIASES.get(head)
    if tool is None:
        candidate = head.lstrip("/")
        if is_client_delegatable(candidate):
            tool = candidate
    if tool is None:
        return None

    args: dict[str, Any] = {}
    arg_stripped = arg.strip()
    if arg_stripped.startswith("{"):
        try:
            parsed = json.loads(arg_stripped)
            if isinstance(parsed, dict):
                args = sanitize_client_args(parsed)
                return ClientSlashPlan(tool=tool, args=args, risk=client_tool_risk(tool))
        except ValueError:
            pass

    if tool == "run_code":
        args = _parse_run_code_arg(arg)
    elif tool in {"file_read", "file_delete", "directory_create"}:
        if arg_stripped:
            args = {"path": arg_stripped}
    elif tool == "directory_tree":
        args = {"path": arg_stripped or "."}
    elif tool == "file_find":
        args = {"pattern": arg_stripped or "*", "root": "."}
    elif tool == "text_search":
        args = {"query": arg_stripped, "root": "."}
    elif tool == "run_program":
        args = {"command": arg_stripped}
    elif tool == "test_run":
        args = {"pattern": arg_stripped, "root": "."}
    elif tool == "file_write":
        first_line, _, body = arg.lstrip().partition("\n")
        try:
            tokens = shlex.split(first_line)
        except ValueError:
            tokens = first_line.split(None, 1)
        if body:
            path_val = tokens[0] if tokens else first_line.strip()
            args = {"path": path_val, "content": body, "mode": "overwrite"}
        elif len(tokens) >= 2:
            path_val = tokens[0]
            content_start = first_line.find(tokens[1])
            args = {
                "path": path_val,
                "content": first_line[content_start:] if content_start >= 0 else tokens[1],
                "mode": "overwrite",
            }
        elif tokens:
            args = {"path": tokens[0], "content": ""}
    elif tool == "file_edit":
        try:
            tokens = shlex.split(arg_stripped)
        except ValueError:
            tokens = []
        if len(tokens) >= 3:
            args = {"path": tokens[0], "old": tokens[1], "new": tokens[2]}
        elif arg_stripped:
            args = {"raw": arg_stripped}
    elif tool.startswith("git_"):
        args = {"cwd": arg_stripped or "."}

    return ClientSlashPlan(tool=tool, args=sanitize_client_args(args), risk=client_tool_risk(tool))


__all__ = [
    "CLIENT_DELEGATABLE_TOOLS",
    "CLIENT_DELEGATABLE_TOOL_DEFINITIONS",
    "CLIENT_EXEC_HEADER",
    "ClientSlashPlan",
    "TOOL_BACKEND_CLIENT",
    "client_tool_risk",
    "client_tools_payload",
    "is_client_delegatable",
    "resolve_client_slash_command",
    "sanitize_client_args",
]
