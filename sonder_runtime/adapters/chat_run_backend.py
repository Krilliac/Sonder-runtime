"""Execution backend for ``ChatToolTurn`` tool calls (``run_code``, ``tool_search``, workspace)."""
from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from sonder_runtime.adapters.security import role_tool_policy
from sonder_runtime.application.agents.delegation_runtime import can_delegate
from sonder_runtime.domain.agents.delegation import DELEGATION_TOOLS
from sonder_runtime.domain.agents.tool_help import match_catalog_tools, tool_help_text
from sonder_runtime.domain.chat_tools import catalog_entry_to_ollama_tool, tool_backend_label
from sonder_runtime.domain.cloud_access import LEGACY_ERROR_PREFIX, has_legacy_error_prefix


@dataclass(frozen=True)
class ToolExecutionResult:
    """Bounded observation and receipt fields for one executed chat tool call."""

    ok: bool
    output: str
    backend: str
    duration_ms: int = 0
    exit_code: int | None = None
    timed_out: bool = False
    stdout_preview: str = ""
    stderr_preview: str = ""
    reason: str = ""


def _filtered_catalog(
    catalog: Mapping[str, Any],
    blocked_tools: frozenset[str],
    permission_policy: Any,
) -> dict[str, Any]:
    mode = ""
    try:
        mode = str(permission_policy.current_mode() or "")
    except Exception:
        mode = ""
    allow_delegation = can_delegate()
    out: dict[str, Any] = {}
    for name, meta in (catalog or {}).items():
        if name in blocked_tools or str(name).startswith("admin_"):
            continue
        if not allow_delegation and name in DELEGATION_TOOLS:
            continue
        if role_tool_policy.agent_refusal(name):
            continue
        if mode == "plan":
            try:
                if not permission_policy.decide(name, interactive=False, record=False).allowed:
                    continue
            except Exception:
                continue
        out[name] = meta
    return out


def format_agent_gate_refusal(name: str, decision: Any) -> str:
    """Render the standing HOST POLICY observation for a permission gate denial."""
    if getattr(decision, "source", "") == "fence":
        msg = (
            f"{LEGACY_ERROR_PREFIX} HOST POLICY: tool '{name}' is refused because this worker's "
            f"authority to produce effects is gone ({decision.reason}). Stop and report what "
            "was and was not done; do not retry."
        )
        return msg
    remedy = ""
    if getattr(decision, "call_id", "") and getattr(decision, "source", "") == "unattended":
        call = decision.call_id
        remedy = (
            f" This exact call is recorded as pending call {call}; an operator can "
            f"approve it once at the console (/approve {call}), after which one "
            "unchanged retry runs."
        )
    msg = (
        f"{LEGACY_ERROR_PREFIX} HOST POLICY: tool '{name}' is refused by the active permission "
        f"gate ({decision.reason}, mode={decision.mode}, risk={decision.risk}). This is the host's "
        "standing policy, not a transient failure: retrying it unchanged will be refused again. "
        f"Choose a different tool or finalize with what you have.{remedy}"
    )
    return msg


class ChatRunBackend:
    """Dispatches chat tool calls through host permission gates and runtime executors."""

    def __init__(
        self,
        *,
        dispatch_agent_tool: Callable[..., str],
        catalog_provider: Callable[[], Mapping[str, Any]],
        permission_policy: Any,
        blocked_tools: frozenset[str],
        container_active: Callable[[], bool] | None = None,
        project_root: str = "",
    ) -> None:
        self._dispatch_agent_tool = dispatch_agent_tool
        self._catalog_provider = catalog_provider
        self._permission_policy = permission_policy
        self._blocked_tools = frozenset(blocked_tools)
        self._container_active = container_active
        self._project_root = str(project_root or "")

    def backend_for(self, tool_name: str) -> str:
        active = False
        if self._container_active is not None:
            try:
                active = bool(self._container_active())
            except Exception:
                active = False
        return tool_backend_label(tool_name, container_active=active)

    def resolve_catalog_tool(self, tool_name: str) -> dict[str, Any] | None:
        """Return a sanitized OpenAI/Ollama schema if ``tool_name`` is permitted in the catalog."""
        name = str(tool_name or "").strip().lstrip("/")
        if not name or name in {"tool_search", "tool_help"}:
            return None
        catalog = _filtered_catalog(
            self._catalog_provider(), self._blocked_tools, self._permission_policy,
        )
        entry = catalog.get(name)
        if not isinstance(entry, Mapping):
            return None
        return catalog_entry_to_ollama_tool(name, entry)

    def search_catalog_tools(
        self,
        *,
        query: str = "",
        name: str = "",
        limit: int = 8,
    ) -> tuple[bool, str, list[dict[str, Any]]]:
        """Search the filtered catalog and return ``(ok, help_text, matched_ollama_tools)``."""
        catalog = _filtered_catalog(
            self._catalog_provider(), self._blocked_tools, self._permission_policy,
        )
        target_name = str(name or "").strip()
        query_text = str(query or "").strip()
        if target_name and target_name not in catalog:
            return False, f"{LEGACY_ERROR_PREFIX} tool '{target_name}' is not available in this turn.", []
        help_fn: Any = tool_help_text
        text = str(help_fn(name=target_name, query=query_text or ("" if target_name else "read"), catalog=catalog))
        ok = not has_legacy_error_prefix(text)
        matched_names = match_catalog_tools(
            name=target_name,
            query=query_text or ("" if target_name else "read"),
            catalog=catalog,
            limit=limit,
        )
        schemas = [
            catalog_entry_to_ollama_tool(matched, catalog[matched])
            for matched in matched_names
            if matched in catalog and isinstance(catalog[matched], Mapping)
        ]
        return ok, text, schemas

    def execute(self, tool_name: str, args: Mapping[str, Any]) -> ToolExecutionResult:
        started = time.monotonic()
        backend = self.backend_for(tool_name)
        name = str(tool_name or "").strip().lstrip("/")
        if name in {"tool_search", "tool_help"}:
            query = str(args.get("query") or args.get("q") or "").strip()
            target_name = str(args.get("name") or "").strip()
            catalog = _filtered_catalog(
                self._catalog_provider(), self._blocked_tools, self._permission_policy,
            )
            if target_name and target_name not in catalog:
                text = f"{LEGACY_ERROR_PREFIX} tool '{target_name}' is not available in this turn."
                ok = False
            else:
                help_fn: Any = tool_help_text
                text = str(help_fn(name=target_name, query=query or "read", catalog=catalog))
                ok = not has_legacy_error_prefix(text)
            return ToolExecutionResult(
                ok=ok,
                output=text,
                backend=backend,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        extra_roots = self._project_root if self._project_root else ""
        raw = str(
            self._dispatch_agent_tool(
                name,
                dict(args),
                allow_web=True,
                read_only=False,
                allow_location=False,
                repository_extra_roots=extra_roots,
            )
            or ""
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        ok = not has_legacy_error_prefix(raw) and not raw.lstrip().startswith("refused")
        timed_out = bool(re.search(r"(?im)(?:^status:\s*timed[_ ]?out\b|\btimed out\b)", raw))
        exit_code: int | None = None
        rc_match = re.search(r"(?im)^[ \t]*returncode:\s*(-?\d+)\b", raw)
        if rc_match is not None:
            try:
                exit_code = int(rc_match.group(1))
            except ValueError:
                exit_code = None
        if ok and name in {
            "run_code", "run_project", "script_run", "workspace_run",
            "test_run", "lint_run", "format_code", "typecheck_run", "build_run",
        }:
            if (
                timed_out
                or (exit_code is not None and exit_code != 0)
                or re.search(r"(?im)^[ \t]*status:\s*(?:failed|error|timed[_ ]?out)\b", raw)
                or re.search(r"(?im)^[ \t]*ok:\s*false\b", raw)
            ):
                ok = False
        if exit_code is None and ok:
            exit_code = 0
        return ToolExecutionResult(
            ok=ok,
            output=raw,
            backend=backend,
            duration_ms=duration_ms,
            exit_code=exit_code,
            timed_out=timed_out,
            stdout_preview=raw[:500] if ok else "",
            stderr_preview=raw[:500] if not ok else "",
            reason="" if ok else raw[:240],
        )


def resolve_agent_project_scope(project: object) -> tuple[str, str]:
    """Resolve a project directory while preserving bare checklist namespaces."""
    from sonder_runtime.adapters.filesystem import file_ops as _file_ops

    text = str(project or "").strip()
    if not text:
        return "", ""
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        msg = f"{LEGACY_ERROR_PREFIX} invalid agent project root: control characters are not permitted"
        return "", msg
    try:
        cleaned = _file_ops._reject_network_path(text)
        expanded = os.path.expanduser(cleaned)
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in expanded):
            msg = f"{LEGACY_ERROR_PREFIX} invalid agent project root: control characters are not permitted"
            return "", msg
        expanded = _file_ops._reject_network_path(expanded)
        candidate = os.path.realpath(os.path.abspath(expanded))
        if os.path.isdir(candidate):
            return candidate, ""
        path_like = (
            os.path.isabs(expanded)
            or bool(re.match(r"^[A-Za-z]:", expanded))
            or expanded.startswith((".", "~"))
            or "/" in expanded
            or "\\" in expanded
            or os.path.exists(candidate)
        )
        if path_like:
            msg = f"{LEGACY_ERROR_PREFIX} invalid agent project root: path does not name an existing directory: {candidate}"
            return "", msg
    except (OSError, ValueError) as exc:
        msg = f"{LEGACY_ERROR_PREFIX} invalid agent project root: {exc}"
        return "", msg
    return "", ""


def run_chat_tool_turn(
    turn: Any,
    *,
    chat_request: Callable[..., tuple[dict[str, Any], Any]],
    model: str,
    cloud: bool,
    options: Mapping[str, Any],
    keep_alive: Any,
    system: str,
    prompt: str,
    history: list[dict[str, Any]] | None,
) -> Any:
    """Execute ``turn`` against ``server._chat_request`` and return ``ChatToolOutcome``."""
    def step(messages: list[dict[str, Any]], tools_payload: list[dict[str, Any]] | None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": dict(options),
        }
        if not cloud and keep_alive is not None:
            payload["keep_alive"] = keep_alive
        out, message = chat_request(
            payload,
            model=model,
            cloud=cloud,
            tools=tools_payload,
            return_message=True,
            accept_native_tool_calls=True,
        )
        msg_dict = message if isinstance(message, Mapping) else {"role": "assistant", "content": str(message or "")}
        return {
            "message": dict(msg_dict),
            "usage": {
                "tokens_in": int((out or {}).get("prompt_eval_count") or 0),
                "tokens_out": int((out or {}).get("eval_count") or 0),
            },
            "meta": {"model": (out or {}).get("model") or model},
        }

    return turn.run(step, system=system, prompt=prompt, history=history)


def maybe_answer_chat_tools(
    *,
    chat_request: Callable[..., tuple[dict[str, Any], Any]],
    model: str,
    cloud: bool,
    temperature: float | None,
    num_predict: int,
    num_ctx: int | None,
    keep_alive: Any,
    local_options_fn: Callable[..., Mapping[str, Any]],
    system: str,
    prompt: str,
    history: list[dict[str, Any]] | None,
) -> tuple[bool, str | None]:
    """If an active ``ChatToolTurn`` with callable tools is bound, run it and return ``(True, content)``."""
    from sonder_runtime.application.chat import tool_loop as _chat_tool_loop
    active = _chat_tool_loop.current_turn()
    if active is None or not active.manifest.tools:
        return False, None
    tool_opts = (
        {"num_predict": num_predict, **({} if temperature is None else {"temperature": temperature})}
        if cloud else local_options_fn(temperature, num_predict, num_ctx)
    )
    outcome = run_chat_tool_turn(
        active,
        chat_request=chat_request,
        model=model,
        cloud=cloud,
        options=tool_opts,
        keep_alive=None if cloud else keep_alive,
        system=system,
        prompt=prompt,
        history=history,
    )
    return True, str(outcome.content)


__all__ = [
    "ChatRunBackend",
    "ToolExecutionResult",
    "format_agent_gate_refusal",
    "maybe_answer_chat_tools",
    "resolve_agent_project_scope",
    "run_chat_tool_turn",
]
