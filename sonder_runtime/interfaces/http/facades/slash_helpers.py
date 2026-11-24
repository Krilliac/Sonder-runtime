"""HTTP slash-command gating, chat history/run helpers, and served command catalog payloads.

Extracted from ``sonder_runtime/interfaces/http/serve.py``.  ``serve.py`` keeps
``_handle_slash`` and ``_dispatch_catalogued_tool`` directly so AST-based
command-catalog and permission-gate invariants continue inspecting them in
place, while all supporting validation, gating, history, and discovery logic
lives here with strict typing and clean SPEC-3 boundaries.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

from sonder_runtime.application.command_surface.command_text import slash_head as _slash_head
from sonder_runtime.application.command_surface.natural_intents import dispatch as _dispatch_natural_intent
from ..served_dispatch_policy import intent_slash_lines as _intent_slash_lines
from .config_wiring import HTTPRequestError

SUPPORTED_CHAT_ROLES = frozenset({"system", "user", "assistant"})
SERVER_SIDE_HISTORY_TURNS = 8
CHAT_HISTORY_SOURCES = ("auto", "client")

HTTP_NATURAL_COMMANDS = frozenset({
    "/persona", "/effort", "/temp",
    "/stats", "/context", "/contextsize", "/hardware", "/env", "/version",
    "/diagnostics", "/updatecheck", "/tool_manifest", "/commands",
    "/permissions", "/activity", "/agents", "/capacity", "/goal",
    "/workflow_list", "/npu_status", "/calibration_status",
    "/learning_health_status", "/mcp_runtime_status", "/status",
    "/system_profile_text", "/evaluation_history_status",
    "/repo_status", "/repo_log", "/repo_diff",
    "/todo",
    "/consult", "/route", "/ensemble",
})


def dangerous_http_slash(
    content: Any,
    *,
    dangerous_commands: frozenset[str],
) -> bool:
    """Return True when *content* invokes a developer-gated HTTP slash command."""
    stripped = (content or "").strip()
    if not stripped.startswith("/"):
        return False
    pieces = stripped.split(None, 2)
    command = _slash_head(pieces[0])
    if command in ("/autopilot", "/auto"):
        action = pieces[1].lower() if len(pieces) > 1 else "status"
        return action not in ("status", "show", "list", "help", "?")
    if command in ("/runtime", "/models"):
        action = pieces[1].lower() if len(pieces) > 1 else "status"
        return action not in ("status", "show", "list", "help", "?")
    if command in ("/update", "/updatecheck", "/updatesource"):
        if command == "/updatecheck":
            return False
        action = pieces[1].lower() if len(pieces) > 1 else "apply"
        return action in ("apply", "now")
    if command in ("/stash", "/runtime-stash"):
        action = (pieces[1].lower() if len(pieces) > 1 else "status").replace("_", "-")
        return action in ("save", "save-untracked", "pop")
    if command in ("/selfmod", "/selfmodify"):
        action = pieces[1].lower() if len(pieces) > 1 else "status"
        return action not in (
            "status", "show", "list", "history", "inspect", "diff",
            "tests", "backups", "verify-backup", "opportunities", "help", "?",
        )
    return command in dangerous_commands


def strip_footer(text: Any, *, runtime: Any) -> str:
    """Strip interaction footer and observable activity block from model output."""
    value = str(text or "")
    idx = value.find(runtime.FOOTER_PREFIX)
    if idx == -1:
        return str(runtime._strip_activity_block(value))
    return str(runtime._strip_activity_block(value[:idx]))


def strip_trace(text: Any) -> str:
    """Strip the inline trace section from a model answer."""
    raw = str(text or "")
    marker = "\n=== TRACE (how Sonder Runtime decided) ==="
    idx = raw.find(marker)
    if idx == -1:
        idx = raw.find("=== TRACE (how Sonder Runtime decided) ===")
    if idx == -1:
        return raw
    return raw[:idx].rstrip()


def record_chat(
    serve_mod: Any,
    role: str,
    content: Any,
    kind: str = "message",
    state: Any = None,
) -> None:
    """Append a bounded conversation event onto *state*."""
    resolved = serve_mod._state_or_legacy(state)
    resolved.events.append({
        "role": "%s/%s" % (role, kind),
        "content": content or "",
    })
    del resolved.events[:-200]


def on_off(arg: Any, current: bool) -> bool:
    """Parse an on/off toggle argument, preserving *current* on unknown values."""
    normalized = (arg or "").strip().lower()
    if normalized in ("", "on"):
        return True
    if normalized == "off":
        return False
    return current


def validate_chat_messages(messages: Any) -> list[dict[str, Any]]:
    """Validate the supported OpenAI-compatible text-only chat message subset."""
    if not isinstance(messages, list):
        raise HTTPRequestError(400, "messages must be an array")

    last_user_content: str | None = None
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise HTTPRequestError(400, "messages[%d] must be an object" % index)
        if "role" not in message:
            raise HTTPRequestError(400, "messages[%d].role is required" % index)
        role = message["role"]
        if not isinstance(role, str) or role not in SUPPORTED_CHAT_ROLES:
            raise HTTPRequestError(
                400,
                "messages[%d].role must be one of: assistant, system, user" % index,
            )
        if "content" not in message:
            raise HTTPRequestError(400, "messages[%d].content is required" % index)
        content = message["content"]
        if not isinstance(content, str):
            raise HTTPRequestError(
                400,
                "messages[%d].content must be a string; multimodal content is not supported"
                % index,
            )
        if role == "user":
            last_user_content = content

    if last_user_content is None or not last_user_content.strip():
        raise HTTPRequestError(400, "messages must contain a non-empty user message")
    return messages


def chat_facade_payload(req: Mapping[str, Any], operation: str) -> dict[str, Any]:
    """Apply chat adapter defaults before handing *req* to the model facade."""
    payload = dict(req)
    if operation == "chat.completions":
        validate_chat_messages(payload.get("messages"))
    model = payload.get("model")
    if "model" not in payload or (isinstance(model, str) and not model.strip()):
        payload["model"] = "sonder"
    if payload.get("stream", False) is None:
        payload["stream"] = False
    return payload


def last_user_message(messages: Sequence[Mapping[str, Any]] | None) -> str:
    """Return the content of the last ``user`` message, or ``""``."""
    for msg in reversed(messages or ()):
        if msg.get("role") == "user":
            return str(msg.get("content") or "")
    return ""


def history_from_messages(
    messages: Sequence[Mapping[str, Any]] | None,
    *,
    runtime: Any,
    answer_only_fn: Callable[[str], str],
) -> list[dict[str, str]]:
    """Extract normalized prior user/assistant turns, excluding the final user turn."""
    msgs = list(messages or ())
    last_user_idx: int | None = None
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "user":
            last_user_idx = i
            break
    history: list[dict[str, str]] = []
    for i, msg in enumerate(msgs):
        if i == last_user_idx:
            continue
        role = msg.get("role")
        content = str(msg.get("content") or "")
        if role == "assistant":
            content = str(runtime._strip_activity_block(answer_only_fn(content))).strip()
        if role in ("user", "assistant") and content:
            history.append({"role": str(role), "content": content})
    return history


def durable_session_history(
    storage_session: str,
    limit: int,
    *,
    session_facade: Any,
    strip_footer_fn: Callable[[str], str],
    logger: Any,
) -> list[dict[str, str]]:
    """Return the last *limit* turns of a verified durable session transcript."""
    if session_facade is None:
        return []
    try:
        result = session_facade.replay(storage_session)
    except Exception:
        logger.warning("durable session history unavailable", exc_info=True)
        return []
    if getattr(result, "status_code", None) != 200:
        logger.warning(
            "durable session history unavailable: replay status=%s",
            getattr(result, "status_code", None),
        )
        return []
    messages: list[dict[str, str]] = []
    for item in (result.body or {}).get("transcript") or ():
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        if role == "assistant":
            content = strip_footer_fn(content.split("=== ACTIVITY")[0])
        content = content.strip()
        if content:
            messages.append({"role": role, "content": content})
    user_positions = [
        index for index, message in enumerate(messages) if message["role"] == "user"
    ]
    if len(user_positions) > limit:
        messages = messages[user_positions[-limit] :]
    return messages


def server_side_history(
    serve_mod: Any,
    storage_session: Any,
    limit: int = SERVER_SIDE_HISTORY_TURNS,
    *,
    session_turns_fn: Callable[[Any, Any], Sequence[Mapping[str, Any]]],
) -> list[dict[str, str]]:
    """Rebuild prior turns from durable or legacy session storage for thin clients."""
    if not (storage_session or "").strip():
        return []
    bounded = max(1, int(limit))
    durable = durable_session_history(
        storage_session,
        bounded,
        session_facade=serve_mod._SESSION_FACADE,
        strip_footer_fn=serve_mod._strip_footer,
        logger=serve_mod._serve_logger,
    )
    if durable:
        return durable
    try:
        session_id = serve_mod.server._resolve_session(storage_session)
        conn = serve_mod.server._open_db()
        try:
            turns = session_turns_fn(conn, session_id)
        finally:
            conn.close()
    except Exception:
        return []
    history: list[dict[str, str]] = []
    for turn in turns[-bounded:]:
        task = str(turn.get("task") or "").strip()
        response = str(turn.get("response") or "").split("=== ACTIVITY")[0]
        response = str(serve_mod._strip_footer(response)).strip()
        if task:
            history.append({"role": "user", "content": task})
        if response:
            history.append({"role": "assistant", "content": response})
    return history


def parse_train_n(arg: Any, *, default_n: int, max_n: int) -> tuple[int | None, str | None]:
    """Parse ``/train``'s ``N`` argument, clamping to ``[1, max_n]``."""
    stripped = (arg or "").strip()
    if not stripped:
        return default_n, None
    try:
        n = int(stripped)
    except ValueError:
        return None, "usage: /train [N]  (N must be an integer, default %d)" % default_n
    if n < 1:
        n = 1
    if n > max_n:
        n = max_n
    return n, None


def parse_run_timeout(arg: Any, *, grounding_mod: Any) -> tuple[int | None, str | None]:
    """Parse ``/run``'s optional timeout argument."""
    stripped = (arg or "").strip()
    if not stripped:
        return int(grounding_mod.DEFAULT_TIMEOUT), None
    try:
        value = int(stripped)
    except ValueError:
        return (
            None,
            "usage: /run [seconds]  (runs the previous fenced code block, not a filename or shell command)",
        )
    return int(grounding_mod.clamp_timeout(value)), None


def run_sources_from_messages(
    serve_mod: Any,
    messages: Sequence[Mapping[str, Any]] | None = None,
    state: Any = None,
) -> Iterator[str]:
    """Yield unique candidate assistant texts for ``/run`` / ``/runproject``."""
    resolved = serve_mod._state_or_legacy(state)
    seen: set[str] = set()
    for msg in reversed(messages or ()):
        if msg.get("role") != "assistant":
            continue
        content = str(serve_mod._answer_only(msg.get("content") or ""))
        if content and content not in seen:
            seen.add(content)
            yield content
    for source in (resolved.last_run_source, resolved.last_response):
        if source and source not in seen:
            seen.add(source)
            yield source


def http_slash_refusal(
    serve_mod: Any,
    cmd: str,
    argument: str = "",
    context: Any = None,
    arguments: Any = None,
    *,
    role_tool_policy: Any,
) -> str:
    """The permission gate for the curated HTTP slash dispatch chain."""
    catalog = serve_mod.command_catalog
    try:
        tools = catalog.http_slash_tools().get(cmd, ())
        untooled = cmd in catalog.http_slash_untooled()
    except catalog.CatalogUnavailable as exc:
        return "refused %s: %s" % (cmd, exc)
    if untooled:
        role = serve_mod._restricted_tool_role(context)
        reason = role_tool_policy.untooled_refusal_reason(cmd, role) if role else ""
        if reason:
            return "refused %s: %s" % (cmd, reason)
    tools = catalog.narrow_branch_tools(cmd, argument, tools)
    resolved_args = (
        arguments
        if arguments is not None
        else serve_mod._slash_call_arguments(cmd, argument)
    )
    return str(
        serve_mod._http_tool_refusal(
            tools, cmd, context=context, arguments=resolved_args,
        )
    )


def http_tool_authority_refusal(
    serve_mod: Any,
    tools: Sequence[str],
    label: str,
    context: Any = None,
    *,
    unbound_sentinel: Any,
) -> str:
    """Account-memory, system-operation, and role checks for *tools*."""
    for tool in tools:
        memory_error = serve_mod._account_global_memory_refusal(tool, context)
        if memory_error:
            return "refused %s: %s" % (label, memory_error)
        operation = serve_mod._http_system_operation_for(tool)
        if operation and context is not None:
            if operation == unbound_sentinel:
                if serve_mod.permission_policy.is_durable_authority_tool(tool):
                    pass
                elif not serve_mod._admin_authorized(context):
                    return (
                        "refused %s: administrator authorization is required "
                        "for an unclassified system operation" % label
                    )
            else:
                authority_error = serve_mod._system_operation_authority_error(
                    operation, context,
                )
                if authority_error:
                    return "refused %s: %s" % (label, authority_error)
    for tool in tools:
        role_error = serve_mod._role_tool_refusal_reason(tool, context)
        if role_error:
            return "refused %s: %s" % (label, role_error)
    return ""


def http_tool_refusal(
    serve_mod: Any,
    tools: Sequence[str],
    label: str,
    context: Any = None,
    arguments: Any = None,
) -> str:
    """Authority + unattended permission-mode decision for HTTP tools."""
    authority_refusal = serve_mod._http_tool_authority_refusal(
        tools, label, context=context,
    )
    if authority_refusal:
        return str(authority_refusal)
    policy = serve_mod.permission_policy
    for tool in tools:
        decision = policy.decide_for_caller(
            tool,
            interactive=False,
            gate_control_exempt=True,
            surface="http",
            arguments=(
                arguments if (len(tools) == 1 and isinstance(arguments, dict)) else None
            ),
        )
        if decision is None:
            continue
        if decision.action == "deny":
            return "refused %s: %s (mode: %s)" % (
                label,
                decision.reason,
                policy.mode_label(decision.mode),
            )
        policy.note_preapproved_decision(tool, decision)
    return ""


def served_control_guard_for(
    serve_mod: Any, context: Any, *, disabled: bool = False,
) -> Callable[[Any], Any] | None:
    """Build the ``server.served_control_guard`` callback for an HTTP request."""
    if disabled:
        return lambda _text: serve_mod.server.CONTROL_COMMAND_SKIP
    if context is None:
        return None
    return lambda text: (
        serve_mod._nested_slash_authority_refusal(
            text.strip(), _slash_head(text.strip().split(None, 1)[0]), context,
        )
        if str(text or "").strip().startswith("/")
        else ""
    )


def run_catalogued_tool_gated(
    serve_mod: Any,
    line: str,
    tool_name: str,
    kwargs: dict[str, Any],
    handler: Callable[..., Any],
    *,
    state: Any,
    context: Any,
) -> str:
    """The gated ``/<tool>`` dispatch, run inside the call's reach scope."""
    refusal = serve_mod._http_tool_refusal(
        (tool_name,), "/" + tool_name, context=context, arguments=kwargs,
    )
    if refusal:
        return str(refusal)
    if tool_name == "ollama_pool_admin_status" and context is not None:
        params = {key: value for key, value in kwargs.items() if key != "token"}
        result, _status = serve_mod._ollama_pool_admin_page(context, params)
        return json.dumps(result)
    if tool_name == "loop":
        refusal = serve_mod._loop_global_operation_refusal(
            kwargs.get("actions_json"), context,
        )
        if refusal:
            return str(refusal)
    if tool_name == "sonder":
        refusal = serve_mod._nested_slash_authority_refusal(
            kwargs.get("prompt", ""), "/sonder", context,
        )
        if refusal:
            return str(refusal)
    task_boundary_error = serve_mod._account_task_boundary_refusal(
        tool_name, kwargs, context,
    ) or (
        tool_name in ("master_orchestrate", "sonder")
        and serve_mod.server.served_project_refusal(kwargs.get("project", ""))
    )
    if task_boundary_error:
        return str(task_boundary_error)
    if context is not None and context.get("mode") != "local-open":
        if tool_name == "tool_manifest":
            return str(serve_mod._served_tool_manifest(context))
        if tool_name == "tool_capability_manifest":
            return str(serve_mod._served_tool_capability_manifest(context))
    state_token = getattr(state, "token", "")
    if "token" not in kwargs and state_token:
        command = serve_mod.command_catalog.by_name(line.split(None, 1)[0])
        if command and any(p.name == "token" for p in command.params):
            kwargs["token"] = state.token
    control_guard = serve_mod._served_control_guard_for(context)

    def _run_handler() -> str:
        if control_guard is None:
            return str(handler(**kwargs))
        with serve_mod.server.served_control_guard(control_guard):
            return str(handler(**kwargs))

    try:
        if (
            tool_name in serve_mod._SCOPED_TASK_TOOLS
            and serve_mod._task_account_scope(context) is not None
        ):
            return str(serve_mod._served_task_tool(tool_name, kwargs, context))
        return str(serve_mod._narrate_http_command(tool_name, kwargs, _run_handler, context))
    except TypeError as error:
        return "%s: %s" % (tool_name, error)
    except Exception as error:
        serve_mod._serve_logger.error(
            f"catalogued tool execution failed: tool={tool_name!r}", exc_info=True,
        )
        return "%s failed: %s: %s" % (tool_name, type(error).__name__, error)


def handle_feedback(serve_mod: Any, content: Any, state: Any = None) -> str | None:
    """Record plain-language passive feedback on the last interaction, else ``None``."""
    resolved = serve_mod._state_or_legacy(state)
    if not resolved.last_iid:
        return None
    signal = serve_mod.feedback.classify_signal(content)
    if signal and serve_mod.server.reward_rules.reward_score(signal) > 0:
        serve_mod.server.record_outcome(resolved.last_iid, signal)
        resolved.last_iid = None
        return "Got it - recorded %s so I can learn." % signal
    if signal:
        serve_mod.server.record_outcome(resolved.last_iid, signal)
        resolved.last_iid = None
        return "Got it - recorded that as not-helpful so I can learn."
    fb = serve_mod.feedback.classify_feedback(content)
    if fb == "positive":
        serve_mod.server.record_outcome(resolved.last_iid, "accepted")
        resolved.last_iid = None
        return "Got it — recorded that as helpful so I can learn."
    if fb == "negative":
        serve_mod.server.record_outcome(resolved.last_iid, "rejected")
        resolved.last_iid = None
        return "Got it — recorded that as not-helpful so I can learn."
    return None


def handle_intent(
    serve_mod: Any,
    content: Any,
    messages: Any = None,
    state: Any = None,
    context: Any = None,
) -> str | None:
    """Reply text for a natural-language control intent, else ``None``."""
    resolved = serve_mod._state_or_legacy(state)
    intent = serve_mod.intents.classify(content)
    if not intent:
        return None
    replies: list[str] = [
        str(serve_mod._handle_slash(line, messages=messages, state=resolved, context=context))
        for line in _intent_slash_lines(
            {k: intent[k] for k in ("trace", "strict") if k in intent}
        )
    ]
    replies.extend(
        _dispatch_natural_intent(
            intent,
            lambda command: serve_mod._handle_slash(
                command, messages=messages, state=resolved, context=context,
            ),
            lambda: context is None or bool(serve_mod._developer_authorized(context)),
        )
    )
    return "\n".join(replies)


def handle_natural_command(
    serve_mod: Any,
    content: Any,
    *,
    command_router: Any,
    messages: Any = None,
    state: Any = None,
    project: str = "",
    context: Any = None,
    idempotency_key: str = "",
    workspace_project: Any = None,
    lane_session: str = "",
    storage_session: str = "",
) -> Any:
    """Resolve curated plain-language commands through the gated slash handler."""
    if context is not None and not serve_mod._developer_authorized(context):
        return None
    resolved = command_router.resolve(content)
    if not resolved or not resolved.startswith("/"):
        return None
    head = _slash_head(resolved.split(None, 1)[0])
    if head not in HTTP_NATURAL_COMMANDS:
        return None
    if head == "/contextsize" and " " in resolved.strip():
        return None
    if (
        context is not None
        and serve_mod._dangerous_http_slash(resolved)
        and not serve_mod._developer_authorized(context)
    ):
        return None
    return serve_mod._handle_slash(
        resolved,
        messages=messages,
        state=state,
        project=project,
        context=context,
        idempotency_key=idempotency_key,
        workspace_project=workspace_project,
        lane_session=lane_session,
        storage_session=storage_session,
    )


def served_command_visible(
    serve_mod: Any,
    command: Any,
    context: Any = None,
    *,
    unbound_sentinel: Any,
) -> bool:
    """Whether *command*'s schema belongs in this served caller's catalog."""
    if context is None or context.get("mode") == "local-open":
        return True
    tool = str(getattr(command, "tool", "") or "")
    if tool:
        operation = serve_mod._http_system_operation_for(tool)
        if operation == unbound_sentinel:
            return bool(serve_mod._admin_authorized(context))
        if operation and serve_mod._system_operation_authority_error(operation, context):
            return False
    if bool(getattr(command, "native", False)):
        name = str(getattr(command, "name", "") or "")
        if serve_mod._dangerous_http_slash(name) and not serve_mod._developer_authorized(context):
            return False
    return True


def served_commands(serve_mod: Any, context: Any = None) -> tuple[Any, ...]:
    """Return catalog rows visible to one HTTP authorization context."""
    return tuple(
        command
        for command in serve_mod.command_catalog.catalog()
        if serve_mod._served_command_visible(command, context)
    )


def served_http_commands(serve_mod: Any, context: Any = None) -> tuple[Any, ...]:
    """Return the caller-visible catalog commands the HTTP dispatcher handles."""
    http_names = {command.name for command in serve_mod.command_catalog.http_catalog()}
    return tuple(
        command
        for command in serve_mod._served_commands(context)
        if command.name in http_names
    )


def served_help_text(serve_mod: Any, topic: Any, context: Any = None) -> str:
    """Render help from the caller-filtered catalog without leaking misses."""
    commands = serve_mod._served_commands(context)
    needle = str(topic or "").strip().lower()
    if not needle:
        categories = sorted({command.category for command in commands})
        return (
            "Sonder commands available to this account (use /v1/commands for details):\n"
            + "  "
            + ", ".join(categories)
        )
    if needle in serve_mod.command_catalog.CATEGORIES:
        rows = [command for command in commands if command.category == needle]
        if not rows:
            return "No commands in that category are available to this account."
        return "%s --\n%s" % (
            needle,
            "\n".join("  %s  %s" % (row.name, row.summary) for row in rows),
        )
    for command in commands:
        if needle == command.name or needle in command.aliases:
            return "%s\nusage: %s\n%s" % (
                command.name,
                command.usage(),
                command.summary,
            )
    return "No command with that name is available to this account."


def served_tool_manifest(serve_mod: Any, context: Any) -> str:
    """Human-readable HTTP manifest that excludes unreachable tool schemas."""
    rows = serve_mod._served_commands(context)
    return "\n".join("  %s: %s" % (command.name, command.summary) for command in rows)


def served_tool_capability_manifest(serve_mod: Any, context: Any) -> str:
    """Fingerprint the served, caller-visible command schema surface only."""
    rows = [command.to_dict() for command in serve_mod._served_commands(context)]
    canonical = json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return json.dumps(
        {
            "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "tool_count": len(rows),
            "manifest": serve_mod._served_tool_manifest(context),
            "authority": "informational only; host policy remains authoritative",
        },
        indent=2,
        sort_keys=True,
    )


def commands_index_payload(serve_mod: Any, context: Any = None) -> dict[str, Any]:
    """Build ``GET /v1/commands`` response payload."""
    catalog = serve_mod.command_catalog
    try:
        commands = [command.to_dict() for command in serve_mod._served_http_commands(context)]
        error = ""
    except catalog.CatalogUnavailable as exc:
        commands, error = [], str(exc)
    payload: dict[str, Any] = {
        "commands": commands,
        "categories": dict(catalog.CATEGORIES),
        "popular": [
            name
            for name in catalog.POPULAR
            if any(command.name == name for command in serve_mod._served_commands(context))
        ],
    }
    if error:
        payload["error"] = error
    return payload


def commands_complete_payload(
    serve_mod: Any,
    query: str,
    limit: str = "",
    context: Any = None,
    *,
    max_limit: int,
    completion_limit_fn: Callable[[Any], int],
) -> dict[str, Any]:
    """Build ``GET /v1/commands/complete`` response payload."""
    catalog = serve_mod.command_catalog
    try:
        visible = {command.name for command in serve_mod._served_http_commands(context)}
        matches = [
            command.to_dict()
            for command in catalog.complete(query, limit=max_limit)
            if command.name in visible
        ]
    except catalog.CatalogUnavailable as exc:
        return {"matches": [], "error": str(exc)}
    return {"matches": matches[: completion_limit_fn(limit)]}


def commands_help_payload(
    serve_mod: Any, topic: str = "", context: Any = None,
) -> dict[str, str]:
    """Build ``GET /v1/commands/help`` response payload."""
    catalog = serve_mod.command_catalog
    if context is None or context.get("mode") == "local-open":
        requested = str(topic or "").strip()
        catalogued = catalog.by_name(requested) if requested else None
        if catalogued is not None and catalogued.native:
            wanted = requested.lower()
            if not wanted.startswith("/"):
                wanted = "/" + wanted
            http_spellings = {
                spelling.lower()
                for command in catalog.http_catalog()
                for spelling in command.all_names
            }
            if wanted not in http_spellings:
                return {"text": "no HTTP command '%s'." % requested}
        return {"text": str(catalog.help_text(topic))}
    try:
        return {"text": str(serve_mod._served_help_text(topic, context))}
    except catalog.CatalogUnavailable as exc:
        return {"text": "Command catalog unavailable: %s" % exc}


__all__ = [
    "CHAT_HISTORY_SOURCES",
    "HTTP_NATURAL_COMMANDS",
    "SERVER_SIDE_HISTORY_TURNS",
    "SUPPORTED_CHAT_ROLES",
    "chat_facade_payload",
    "commands_complete_payload",
    "commands_help_payload",
    "commands_index_payload",
    "dangerous_http_slash",
    "durable_session_history",
    "handle_feedback",
    "handle_intent",
    "handle_natural_command",
    "history_from_messages",
    "http_slash_refusal",
    "http_tool_authority_refusal",
    "http_tool_refusal",
    "last_user_message",
    "on_off",
    "parse_run_timeout",
    "parse_train_n",
    "record_chat",
    "run_catalogued_tool_gated",
    "run_sources_from_messages",
    "served_command_visible",
    "served_commands",
    "served_control_guard_for",
    "served_help_text",
    "served_http_commands",
    "served_tool_capability_manifest",
    "served_tool_manifest",
    "server_side_history",
    "strip_footer",
    "strip_trace",
    "validate_chat_messages",
]
