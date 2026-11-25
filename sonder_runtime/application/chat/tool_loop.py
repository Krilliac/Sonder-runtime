"""Bounded interactive chat-tool turn orchestration (`ChatToolTurn`).

Drives native tool-calling turns inside ``/v1/chat/completions`` and ``sonder()``
with strict wall/step/run budgets, mid-turn operator approval waits via
``ApprovalLedger.open_for`` / ``denied_for``, SSE ``sonder_tool`` event
callbacks, and ``TurnToolManifest`` honesty checks.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any

from sonder_runtime.application.agents import delegation_runtime as _delegation
from sonder_runtime.application.chat.honesty import guard_chat_turn_hallucinations
from sonder_runtime.application.chat.tool_manifest import CallableTool, TurnToolManifest
from sonder_runtime.domain.agents.delegation import (
    DELEGATION_TOOLS,
    compact_text,
    extract_thinking_summary,
)
from sonder_runtime.domain.chat_tools import (
    DEFAULT_CHAT_TOOL_BUDGET,
    ChatToolBudget,
    ChatToolRecord,
    parse_tool_arguments,
    preview_tool_arguments,
)
from sonder_runtime.domain.code_audit import (
    audit_source,
    format_proactive_audit_notice,
)
from sonder_runtime.domain.code_gate_policy import (
    CODE_GATE_SIGNS,
    check_python_static_errors,
    uses_interactive_stdin,
)
from sonder_runtime.domain.prompt_defense import sanitize_tool_observation
from sonder_runtime.domain.resilience import (
    GLOBAL_TELEMETRY,
    classify_model_error,
    compact_messages_for_retry,
    format_fallback_hint,
    repair_tool_arguments,
    resolve_tool_name,
)
from sonder_runtime.domain.tools.builtin_traits import builtin_traits

_FENCED_PYTHON_RE = re.compile(r"```(?:python|py)\s*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
_NON_RETRYABLE_VERIFY_STATUSES = frozenset({
    "refused",
    "denied",
    "timeout",
    "timed_out",
    "awaiting_client",
    "awaiting_client_result",
    "budget_exceeded",
})


def _extract_fenced_python(text: str) -> str | None:
    """Return the first non-trivial fenced Python block from a model reply, or ``None``."""
    if "```" not in text:
        return None
    for match in _FENCED_PYTHON_RE.finditer(text):
        code = match.group(1)
        if code and CODE_GATE_SIGNS.search(code):
            return code
    return None


def _code_digest(code: str) -> str:
    return hashlib.sha256(code.strip().encode("utf-8", errors="replace")).hexdigest()

_ACTIVE_TURN: contextvars.ContextVar["ChatToolTurn | None"] = contextvars.ContextVar(
    "sonder_active_chat_tool_turn", default=None,
)

_EXECUTION_CAP_TOOLS = frozenset({
    "run_code", "run_project", "workspace_run", "script_run",
    "test_run", "lint_run", "format_code", "typecheck_run", "build_run",
})
_CREDENTIAL_KEYS = frozenset({"token", "approval", "extra_roots", "bypass", "developer_authorized"})


def _has_credential_argument(args: Mapping[str, Any]) -> bool:
    return any(
        key in args and args.get(key) not in (None, "", False, (), [], {})
        for key in _CREDENTIAL_KEYS
    )


@dataclass(frozen=True)
class ChatToolOutcome:
    """Final output and structured records from running a ``ChatToolTurn``."""

    content: str
    records: tuple[ChatToolRecord, ...] = ()
    model_calls: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    response_meta: dict[str, Any] = field(default_factory=dict)

    def receipt_entries(self) -> list[dict[str, Any]]:
        return [record.to_receipt() for record in self.records]


@dataclass
class ChatToolTurn:
    """State and callbacks for one interactive chat turn that can call tools."""

    manifest: TurnToolManifest
    tools_payload: list[dict[str, Any]]
    backend: Any
    permission_policy: Any
    blocked_tools: frozenset[str] = frozenset()
    budget: ChatToolBudget = DEFAULT_CHAT_TOOL_BUDGET
    on_tool_event: Callable[[dict[str, Any]], None] | None = None
    on_agent_event: Callable[[dict[str, Any]], None] | None = None
    on_board_event: Callable[[dict[str, Any]], None] | None = None
    wait_for_approval: Callable[[str, str, str, dict[str, Any]], str] | None = None
    is_aborted: Callable[[], bool] | None = None
    role_refusal: Callable[[str], str] | None = None
    allow_system_tools: bool = False
    allow_dynamic_catalog: bool = False
    client_exec_tools: frozenset[str] = frozenset()
    client_exec_only: bool = False
    delegate_client_tool: Callable[[str, dict[str, Any]], tuple[str, ChatToolRecord]] | None = None
    client_handoffs: list[dict[str, Any]] = field(default_factory=list)
    records: list[ChatToolRecord] = field(default_factory=list)
    _dynamic_admitted: set[str] = field(default_factory=set, init=False, repr=False)
    _dynamic_order: list[str] = field(default_factory=list, init=False, repr=False)
    _active_tools_payload: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)

    def _emit(self, payload: Mapping[str, Any]) -> None:
        if self.on_tool_event is None:
            return
        try:
            self.on_tool_event(dict(payload))
        except Exception:
            pass

    def _clip(self, text: str, limit: int) -> tuple[str, bool]:
        if len(text) <= limit:
            return text, False
        omitted = len(text) - limit
        if limit < 600:
            return text[:limit] + f"\n... (truncated {omitted} chars)", True
        # Preserve both opening context (65%) and trailing error/summary (35%)
        head_len = int(limit * 0.65)
        tail_len = max(120, limit - head_len)
        marker = f"\n... (truncated {omitted} middle chars; head & tail preserved) ...\n"
        return text[:head_len] + marker + text[-tail_len:], True

    def _guard_observation(self, tool_name: str, text: str) -> str:
        """Apply always-on secret redaction, Trojan-Source stripping, and prompt-injection defense."""
        sanitized, meta = sanitize_tool_observation(tool_name, text)
        if meta.get("secrets_redacted"):
            GLOBAL_TELEMETRY.record("tool_secret_redacted", tool=tool_name)
        if meta.get("unicode_stripped"):
            GLOBAL_TELEMETRY.record("trojan_unicode_stripped", tool=tool_name)
        if meta.get("injection_flagged"):
            GLOBAL_TELEMETRY.record("prompt_injection_flagged", tool=tool_name)
        return sanitized

    def _proactive_write_audit(self, tool_name: str, args: Mapping[str, Any]) -> str:
        """Run fast in-memory security/bug check on Python writes/edits."""
        if tool_name not in ("file_write", "file_edit"):
            return ""
        path_str = str(args.get("path") or "")
        if not path_str.lower().endswith((".py", ".pyi", ".pyw")):
            return ""
        if tool_name == "file_write":
            content = str(args.get("content") or "")
            notice = format_proactive_audit_notice(content, filename=path_str)
            if notice:
                GLOBAL_TELEMETRY.record("proactive_code_audit_flagged", tool=tool_name, detail=path_str[:120])
            return notice
        # file_edit: snippet may be indented partial block; ignore PY000 partial syntax errors
        import textwrap
        snippet = textwrap.dedent(str(args.get("replacement") or args.get("new_text") or ""))
        if not snippet.strip():
            return ""
        report = audit_source(snippet, filename=path_str, language="python", min_severity="high", max_findings=5)
        real_findings = [f for f in report.get("findings", []) if f.get("rule_id") != "PY000"]
        if not real_findings:
            return ""
        GLOBAL_TELEMETRY.record("proactive_code_audit_flagged", tool=tool_name, detail=path_str[:120])
        lines = [f"\n[PROACTIVE CODE AUDIT: {len(real_findings)} high/critical issue(s) in edited snippet of {path_str}]"]
        for f in real_findings:
            lines.append(f"  - [{f['severity'].upper()} {f['rule_id']}]: {f['message']} Fix: {f['remediation']}")
        return "\n".join(lines)

    def _admitted_tool_names(self) -> tuple[str, ...]:
        base = [t.name for t in self.manifest.tools if t.name]
        if not self._dynamic_admitted:
            return tuple(base)
        seen = set(base)
        extra = [n for n in self._dynamic_order if n in self._dynamic_admitted and n not in seen]
        return tuple([*base, *extra])

    def _admit_dynamic_schemas(self, schemas: Sequence[Mapping[str, Any]]) -> list[str]:
        """Register dynamically discovered tool schemas into this turn (capped at 64 total)."""
        if not self.allow_dynamic_catalog or self.client_exec_only or not schemas:
            return []
        core_names = {t.name for t in self.manifest.tools if t.name}
        existing_payload_names = {
            str(((item.get("function") or {}) if isinstance(item, Mapping) else {}).get("name") or "")
            for item in self._active_tools_payload
        }
        newly_loaded: list[str] = []
        for entry in schemas:
            fn = entry.get("function") if isinstance(entry, Mapping) else None
            name = str((fn or {}).get("name") or "").strip() if isinstance(fn, Mapping) else ""
            if not name or name in core_names or name in self.blocked_tools:
                continue
            if self.role_refusal is not None and self.role_refusal(name):
                continue
            if name not in self._dynamic_admitted:
                self._dynamic_admitted.add(name)
                self._dynamic_order.append(name)
                newly_loaded.append(name)
            if name not in existing_payload_names:
                while len(self._active_tools_payload) >= 64 and self._dynamic_order:
                    evicted = next(
                        (
                            cand for cand in self._dynamic_order
                            if cand not in core_names and cand in existing_payload_names and cand != name
                        ),
                        None,
                    )
                    if evicted is None:
                        break
                    self._active_tools_payload = [
                        item for item in self._active_tools_payload
                        if str(((item.get("function") or {}) if isinstance(item, Mapping) else {}).get("name") or "") != evicted
                    ]
                    existing_payload_names.discard(evicted)
                    self._dynamic_order = [n for n in self._dynamic_order if n != evicted]
                if len(self._active_tools_payload) < 64:
                    self._active_tools_payload.append(dict(entry))
                    existing_payload_names.add(name)
        return newly_loaded

    def _decide_permission(self, name: str, args: Mapping[str, Any]) -> Any:
        decision = self.permission_policy.decide(
            name,
            interactive=False,
            surface="chat-tool",
            arguments=args,
        )
        if (
            name == "tool_search"
            and getattr(decision, "risk", "") == "unclassified"
            and getattr(decision, "source", "") in {"unclassified", "mode"}
        ):
            decision = self.permission_policy.decide(
                "tool_help",
                interactive=False,
                surface="chat-tool",
                arguments=args,
            )
        return decision

    def _run_tool_call(
        self,
        raw_call: Mapping[str, Any],
        *,
        run_count: int,
        failed_tools: set[str] | None = None,
    ) -> tuple[str, int]:
        fn = raw_call.get("function") if isinstance(raw_call, Mapping) else None
        if not isinstance(fn, Mapping):
            return "ERROR: malformed native tool call", run_count
        admitted = self._admitted_tool_names()
        raw_name = str(fn.get("name") or "").strip().lstrip("/")
        name, suggestions, aliased = resolve_tool_name(raw_name, admitted)
        if aliased:
            GLOBAL_TELEMETRY.record("tool_name_aliased", tool=name, detail=f"{raw_name} -> {name}")
        args, repaired_args = repair_tool_arguments(fn.get("arguments"))
        if repaired_args:
            GLOBAL_TELEMETRY.record("tool_args_repaired", tool=name)
        backend = self.backend.backend_for(name)
        if not name:
            return "ERROR: tool name is required", run_count
        if name in self.blocked_tools and not self.allow_system_tools and not self.client_exec_only:
            msg = f"ERROR: HOST POLICY: tool '{name}' is restricted to operator control surfaces."
            self.records.append(ChatToolRecord("", name, "refused", backend, reason=msg[:200]))
            return msg, run_count
        if (
            name not in admitted
            and self.allow_dynamic_catalog
            and not self.client_exec_only
            and name not in self.blocked_tools
        ):
            resolve_fn = getattr(self.backend, "resolve_catalog_tool", None)
            if callable(resolve_fn):
                resolved_schema = resolve_fn(name)
                if isinstance(resolved_schema, Mapping):
                    self._admit_dynamic_schemas([resolved_schema])
                    admitted = self._admitted_tool_names()
                    suggestions = []
        if name not in admitted and suggestions:
            msg = (
                f"ERROR: unknown tool '{name}' is not in this turn's admitted tools. "
                f"Did you mean: {', '.join(suggestions)}?"
            )
            self.records.append(ChatToolRecord("", name, "error", backend, reason=msg[:200]))
            GLOBAL_TELEMETRY.record("tool_name_unknown", tool=name, detail=", ".join(suggestions))
            return msg, run_count
        if (
            name not in admitted
            and "learn_tool" in admitted
            and name not in self.blocked_tools
            and name not in DELEGATION_TOOLS
            and name not in self.client_exec_tools
            and name not in _EXECUTION_CAP_TOOLS
            and not self.client_exec_only
            and (self.role_refusal is None or not self.role_refusal(name))
        ):
            msg = (
                f"ERROR: '{name}' is not a built-in Sonder tool. "
                f"If '{name}' is an external program, CLI, or package, call "
                f"learn_tool(tool='{name}', goal='<your task>') to discover whether it is installed, "
                "inspect its real --version/--help flags and headless scripting options, or research how to use it."
            )
            self.records.append(ChatToolRecord("", name, "error", backend, reason=msg[:200]))
            GLOBAL_TELEMETRY.record("foreign_tool_guided_to_learn", tool=name)
            return msg, run_count
        if name in DELEGATION_TOOLS and not _delegation.can_delegate():
            msg = _delegation.depth_refusal_message(name)
            self.records.append(ChatToolRecord("", name, "refused", backend, reason=msg[:200]))
            _delegation.emit_agent_event(phase="refused", tool=name, ok=False, summary=msg)
            return msg, run_count
        if name in self.blocked_tools and not self.allow_system_tools:
            msg = f"ERROR: HOST POLICY: tool '{name}' is restricted to operator control surfaces."
            self.records.append(ChatToolRecord("", name, "refused", backend, reason=msg[:200]))
            return msg, run_count
        if _has_credential_argument(args):
            msg = f"ERROR: HOST POLICY: tool '{name}' cannot receive caller credential arguments."
            self.records.append(ChatToolRecord("", name, "refused", backend, reason=msg[:200]))
            return msg, run_count
        if self.delegate_client_tool is not None and name in self.client_exec_tools:
            if name in _EXECUTION_CAP_TOOLS and run_count >= self.budget.max_runs:
                msg = (
                    f"ERROR: HOST POLICY: chat turn execution budget reached "
                    f"({self.budget.max_runs} execution runs per turn). Summarize with existing results."
                )
                self.records.append(ChatToolRecord("", name, "budget_exceeded", "client", reason=msg[:200]))
                return msg, run_count
            obs, record = self.delegate_client_tool(name, args)
            self.records.append(record)
            clipped_obs, _ = self._clip(obs, self.budget.model_output_chars)
            return self._guard_observation(name, clipped_obs), run_count + (1 if name in _EXECUTION_CAP_TOOLS else 0)
        if self.client_exec_only:
            msg = (
                f"ERROR: HOST POLICY: tool '{name}' is not available on this turn "
                "(only client-delegatable tools are permitted)."
            )
            self.records.append(ChatToolRecord("", name, "refused", "client", reason=msg[:200]))
            return msg, run_count
        if self.role_refusal is not None and (role_refusal := self.role_refusal(name)):
            self.records.append(ChatToolRecord("", name, "refused", backend, reason=role_refusal[:200]))
            return role_refusal, run_count
        if name in _EXECUTION_CAP_TOOLS and run_count >= self.budget.max_runs:
            msg = (
                f"ERROR: HOST POLICY: chat turn execution budget reached "
                f"({self.budget.max_runs} execution runs per turn). Summarize with existing results."
            )
            self.records.append(ChatToolRecord("", name, "budget_exceeded", backend, reason=msg[:200]))
            return msg, run_count

        decision = self._decide_permission(name, args)
        call_id = str(getattr(decision, "call_id", "") or "")
        preview = preview_tool_arguments(name, args)

        if not getattr(decision, "allowed", False):
            source = str(getattr(decision, "source", "") or "")
            if (
                source == "unattended"
                and call_id
                and self.wait_for_approval is not None
            ):
                self._emit({
                    "phase": "awaiting_approval",
                    "call_id": call_id,
                    "tool": name,
                    "risk": str(getattr(decision, "risk", "") or ""),
                    "mode": str(getattr(decision, "mode", "") or ""),
                    "backend": backend,
                    "arguments": preview["arguments"],
                    "detail": preview["detail"],
                    "detail_truncated": preview["detail_truncated"],
                    "detail_chars": preview["detail_chars"],
                    "bidi_warning": preview["bidi_warning"],
                    "bidi_chars": preview["bidi_chars"],
                    "timeout_seconds": int(self.budget.approval_wait_seconds),
                })
                outcome = self.wait_for_approval(name, call_id, decision, preview)
                if outcome == "approved":
                    self._emit({"phase": "approved", "call_id": call_id, "tool": name})
                    decision = self._decide_permission(name, args)
                else:
                    self._emit({"phase": outcome, "call_id": call_id, "tool": name})
                    msg = f"ERROR: HOST POLICY: operator {outcome} {name} (call {call_id})."
                    self.records.append(ChatToolRecord(
                        call_id, name, outcome, backend,
                        bidi_warning=bool(preview["bidi_warning"]),
                        bidi_chars=tuple(preview["bidi_chars"]),
                        reason=msg[:200],
                    ))
                    return msg, run_count
            if not getattr(decision, "allowed", False):
                msg = f"ERROR: HOST POLICY: {getattr(decision, 'reason', 'refused')}"
                self.records.append(ChatToolRecord(
                    call_id, name, "refused", backend,
                    bidi_warning=bool(preview["bidi_warning"]),
                    bidi_chars=tuple(preview["bidi_chars"]),
                    reason=msg[:200],
                ))
                GLOBAL_TELEMETRY.record("tool_refused", tool=name, detail=msg[:160])
                if failed_tools is not None:
                    failed_tools.add(name)
                hint = format_fallback_hint(
                    name, msg, available_tools=admitted, tried_tools=failed_tools,
                )
                return msg + hint, run_count

        self._emit({
            "phase": "started",
            "call_id": call_id,
            "tool": name,
            "backend": backend,
            "bidi_warning": preview["bidi_warning"],
            "bidi_chars": preview["bidi_chars"],
        })
        _delegation.emit_agent_event(
            phase="tool_call",
            tool=name,
            summary=f"calling {name}",
        )
        preapproved = getattr(self.permission_policy, "preapproved_scope", None)
        preapproved_ctx = (
            preapproved(name, decision)
            if (callable(preapproved) and decision is not None and getattr(decision, "allowed", False))
            else nullcontext()
        )
        is_safe_retryable = bool(builtin_traits(name).is_read_only) and name not in _EXECUTION_CAP_TOOLS
        exec_result = None
        last_exc: Exception | None = None
        for attempt in range(2 if is_safe_retryable else 1):
            try:
                with preapproved_ctx:
                    exec_result = self.backend.execute(name, args)
                last_exc = None
                if (
                    attempt == 0
                    and is_safe_retryable
                    and not exec_result.ok
                    and any(tok in str(exec_result.output or "").lower() for tok in ("database is locked", "temporarily unavailable", "timed out"))
                ):
                    GLOBAL_TELEMETRY.record("readonly_tool_retry", tool=name)
                    time.sleep(0.1)
                    continue
                break
            except (TimeoutError, ConnectionError, OSError) as exc:
                last_exc = exc
                if attempt == 0 and is_safe_retryable:
                    GLOBAL_TELEMETRY.record("readonly_tool_retry", tool=name, detail=str(exc)[:120])
                    time.sleep(0.1)
                    continue
                break
            except Exception as exc:
                last_exc = exc
                break

        if exec_result is None or last_exc is not None:
            err_msg = f"ERROR: tool '{name}' failed unexpectedly: {type(last_exc).__name__}: {last_exc}"
            self.records.append(ChatToolRecord(
                call_id=call_id, tool=name, status="error", backend=backend, reason=err_msg[:200],
            ))
            GLOBAL_TELEMETRY.record("tool_exec_failed", tool=name, detail=err_msg[:160])
            if failed_tools is not None:
                failed_tools.add(name)
            hint = format_fallback_hint(
                name, err_msg, available_tools=admitted, tried_tools=failed_tools,
            )
            return err_msg + hint, run_count

        _delegation.emit_agent_event(
            phase="tool_result",
            tool=name,
            ok=exec_result.ok,
            elapsed_ms=exec_result.duration_ms,
            summary=compact_text(exec_result.output, limit=180),
        )
        new_runs = run_count + (1 if name in _EXECUTION_CAP_TOOLS else 0)
        clipped_model, truncated = self._clip(exec_result.output, self.budget.model_output_chars)
        status = "ok" if exec_result.ok else ("timeout" if exec_result.timed_out else "error")
        self.records.append(ChatToolRecord(
            call_id=call_id,
            tool=name,
            status=status,
            backend=exec_result.backend,
            duration_ms=exec_result.duration_ms,
            exit_code=exec_result.exit_code,
            timed_out=exec_result.timed_out,
            truncated=truncated,
            bidi_warning=bool(preview["bidi_warning"]),
            bidi_chars=tuple(preview["bidi_chars"]),
            stdout_preview=exec_result.stdout_preview,
            stderr_preview=exec_result.stderr_preview,
            reason=exec_result.reason,
        ))
        self._emit({
            "phase": "finished",
            "call_id": call_id,
            "tool": name,
            "ok": exec_result.ok,
            "status": status,
            "backend": exec_result.backend,
            "exit_code": exec_result.exit_code,
            "duration_ms": exec_result.duration_ms,
        })
        if not exec_result.ok:
            GLOBAL_TELEMETRY.record("tool_exec_failed", tool=name, detail=str(exec_result.output or exec_result.reason)[:160])
            if failed_tools is not None:
                failed_tools.add(name)
            hint = format_fallback_hint(
                name, exec_result.output or exec_result.reason,
                available_tools=admitted, tried_tools=failed_tools,
            )
            return self._guard_observation(name, clipped_model) + hint, new_runs
        guarded = self._guard_observation(name, clipped_model)
        if name in {"tool_search", "tool_help"} and self.allow_dynamic_catalog and not self.client_exec_only:
            search_fn = getattr(self.backend, "search_catalog_tools", None)
            if callable(search_fn):
                try:
                    _ok, _help, matched_schemas = search_fn(
                        query=str(args.get("query") or args.get("q") or ""),
                        name=str(args.get("name") or ""),
                        limit=8,
                    )
                    loaded = self._admit_dynamic_schemas(matched_schemas)
                    if loaded:
                        guarded += f"\n[LOADED FOR THIS TURN — callable now: {', '.join(loaded)}]"
                except Exception:
                    pass
        audit_notice = self._proactive_write_audit(name, args)
        return guarded + audit_notice, new_runs

    def _call_model_with_recovery(
        self,
        generate_step: Callable[[list[dict[str, Any]], list[dict[str, Any]] | None], dict[str, Any]],
        messages: list[dict[str, Any]],
        step_tools: list[dict[str, Any]] | None,
        *,
        started: float,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], bool, Exception | None]:
        """Call ``generate_step`` with bounded transient retry, context compaction, and tool-schema fallback."""
        cur_messages = messages
        cur_tools = step_tools
        tools_disabled = False
        compacted = False
        max_attempts = 3
        last_exc: Exception | None = None

        for attempt in range(max_attempts):
            if self.is_aborted is not None and self.is_aborted():
                return None, cur_messages, tools_disabled, last_exc
            if (time.monotonic() - started) >= self.budget.wall_seconds:
                return None, cur_messages, tools_disabled, last_exc
            try:
                step = generate_step(cur_messages, cur_tools)
                if attempt > 0:
                    GLOBAL_TELEMETRY.record("model_call_recovered", detail=f"attempt={attempt + 1}")
                return step, cur_messages, tools_disabled, None
            except Exception as exc:
                last_exc = exc
                err_class = classify_model_error(exc)
                GLOBAL_TELEMETRY.record("model_call_failed", detail=f"{err_class}: {exc}"[:160])
                if err_class == "non_retryable":
                    raise
                if err_class == "context_overflow" and not compacted:
                    new_msgs, saved = compact_messages_for_retry(cur_messages)
                    if saved > 0:
                        compacted = True
                        cur_messages = new_msgs
                        GLOBAL_TELEMETRY.record("context_compaction_retry", detail=f"saved={saved}")
                        continue
                if err_class == "tools_unsupported" and cur_tools is not None:
                    cur_tools = None
                    tools_disabled = True
                    GLOBAL_TELEMETRY.record("tools_unsupported_fallback")
                    continue
                if err_class == "transient" and attempt < max_attempts - 1:
                    GLOBAL_TELEMETRY.record("transient_model_retry", detail=f"attempt={attempt + 1}")
                    time.sleep(min(0.35, 0.15 * (attempt + 1)))
                    continue
                break

        return None, cur_messages, tools_disabled, last_exc

    def run(
        self,
        generate_step: Callable[[list[dict[str, Any]], list[dict[str, Any]] | None], dict[str, Any]],
        *,
        system: str,
        prompt: str,
        history: list[dict[str, Any]] | None = None,
    ) -> ChatToolOutcome:
        """Execute up to ``budget.max_model_calls`` model/tool turns."""
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        if history:
            messages.extend(dict(item) for item in history)
        messages.append({"role": "user", "content": prompt})

        self._dynamic_admitted = set()
        self._dynamic_order = []
        self._active_tools_payload = list(self.tools_payload)
        started = time.monotonic()
        run_count = 0
        tool_calls_total = 0
        calls = 0
        total_in = 0
        total_out = 0
        last_meta: dict[str, Any] = {}
        final_content = ""
        board_cursor = 0
        has_run_code_tool = (
            any(t.name == "run_code" for t in self.manifest.tools)
            and "run_code" not in self.client_exec_tools
        )
        verified_code_hashes: set[str] = set()
        failed_auto_verify_hashes: set[str] = set()
        auto_verify_blocked = False
        tools_disabled_by_fallback = False
        failed_tools: set[str] = set()
        call_signatures: dict[str, int] = {}
        active_board = _delegation.current_board()
        owns_board = active_board is None
        board = active_board or _delegation.AgentProgressBoard()
        on_agent = self.on_agent_event
        on_peer = self.on_board_event
        unsub_agent = (
            board.subscribe(lambda ev: on_agent(ev.to_dict()))
            if on_agent is not None else None
        )
        unsub_peer = (
            board.subscribe_board(lambda msg: on_peer(msg.to_dict()))
            if on_peer is not None else None
        )

        with (_delegation.bind_board(board) if owns_board else nullcontext()):
            _delegation.emit_agent_event(
                phase="starting",
                step=0,
                max_steps=self.budget.max_model_calls,
                summary=compact_text(prompt, limit=180),
            )
            try:
                while calls < self.budget.max_model_calls:
                    if self.is_aborted is not None and self.is_aborted():
                        break
                    if (time.monotonic() - started) >= self.budget.wall_seconds:
                        break
                    calls += 1
                    step_tools = (
                        list(self._active_tools_payload)
                        if (
                            calls < self.budget.max_model_calls
                            and not self.client_handoffs
                            and not tools_disabled_by_fallback
                        )
                        else None
                    )
                    step, messages, newly_disabled, step_exc = self._call_model_with_recovery(
                        generate_step, messages, step_tools, started=started,
                    )
                    if newly_disabled:
                        tools_disabled_by_fallback = True
                    if step is None:
                        if step_exc is not None and not self.records:
                            raise step_exc
                        if step_exc is not None and self.records:
                            GLOBAL_TELEMETRY.record("graceful_degraded_summary", detail=str(step_exc)[:160])
                            ok_summary = ", ".join(
                                f"{r.tool}({r.status})" for r in self.records[-8:]
                            )
                            final_content = (
                                f"Model call failed after completing tool step(s) [{ok_summary}]: "
                                f"{type(step_exc).__name__}: {step_exc}"
                            )
                        break
                    message = step.get("message") if isinstance(step, Mapping) else {}
                    if not isinstance(message, Mapping):
                        message = {}
                    usage = step.get("usage") if isinstance(step, Mapping) else None
                    if isinstance(usage, Mapping):
                        total_in += int(usage.get("tokens_in") or 0)
                        total_out += int(usage.get("tokens_out") or 0)
                    meta = step.get("meta") if isinstance(step, Mapping) else None
                    if isinstance(meta, Mapping):
                        last_meta = dict(meta)

                    tool_calls = message.get("tool_calls")
                    content = str(message.get("content") or step.get("content") or "")
                    thinking = compact_text(
                        message.get("thinking") or step.get("thinking") or extract_thinking_summary(content),
                        limit=280,
                    )
                    if thinking:
                        _delegation.emit_agent_event(
                            phase="thinking",
                            step=calls,
                            max_steps=self.budget.max_model_calls,
                            thinking=thinking,
                            summary=thinking[:160],
                            model=str(last_meta.get("model") or ""),
                        )
                    if not isinstance(tool_calls, (list, tuple)) or not tool_calls:
                        candidate_code = (
                            _extract_fenced_python(content)
                            if (
                                has_run_code_tool
                                and not auto_verify_blocked
                                and not self.client_handoffs
                                and calls < self.budget.max_model_calls
                            )
                            else None
                        )
                        if candidate_code is not None:
                            digest = _code_digest(candidate_code)
                            if digest not in verified_code_hashes and digest not in failed_auto_verify_hashes:
                                if uses_interactive_stdin(candidate_code):
                                    static_err = check_python_static_errors(candidate_code)
                                    if static_err:
                                        failed_auto_verify_hashes.add(digest)
                                        messages.append({"role": "assistant", "content": content})
                                        messages.append({
                                            "role": "user",
                                            "content": (
                                                "HOST CODE VERIFICATION FAILED:\n"
                                                f"{static_err}\n\n"
                                                "Fix the error in your code and return the corrected complete answer."
                                            ),
                                        })
                                        continue
                                    verified_code_hashes.add(digest)
                                elif run_count < self.budget.max_runs:
                                    prior_len = len(self.records)
                                    obs, run_count = self._run_tool_call(
                                        {
                                            "function": {
                                                "name": "run_code",
                                                "arguments": {"language": "python", "code": candidate_code},
                                            },
                                        },
                                        run_count=run_count,
                                    )
                                    last_rec = self.records[-1] if len(self.records) > prior_len else None
                                    if last_rec is not None and last_rec.status == "ok":
                                        verified_code_hashes.add(digest)
                                    elif last_rec is not None and last_rec.status in _NON_RETRYABLE_VERIFY_STATUSES:
                                        auto_verify_blocked = True
                                    elif last_rec is not None and last_rec.status in {"error", "failed"}:
                                        failed_auto_verify_hashes.add(digest)
                                        messages.append({"role": "assistant", "content": content})
                                        messages.append({
                                            "role": "user",
                                            "content": (
                                                "HOST CODE VERIFICATION FAILED:\n"
                                                f"{obs}\n\n"
                                                "Fix the error in your code and return the corrected complete answer."
                                            ),
                                        })
                                        continue
                        final_content = content
                        break

                    assistant_msg: dict[str, Any] = {"role": "assistant", "content": content}
                    assistant_msg["tool_calls"] = list(tool_calls)
                    messages.append(assistant_msg)
                    stopped_early = False
                    for raw_call in tool_calls:
                        if self.is_aborted is not None and self.is_aborted():
                            stopped_early = True
                            break
                        if (time.monotonic() - started) >= self.budget.wall_seconds:
                            stopped_early = True
                            break
                        tool_name = str(
                            ((raw_call.get("function") or {}) if isinstance(raw_call, Mapping) else {}).get("name") or ""
                        ).strip().lstrip("/")
                        raw_call_id = (
                            str(raw_call.get("id") or "").strip()
                            if isinstance(raw_call, Mapping)
                            else ""
                        )
                        if tool_calls_total >= self.budget.max_tool_calls:
                            msg = (
                                f"ERROR: HOST POLICY: chat turn tool-call budget reached "
                                f"({self.budget.max_tool_calls} tool calls per turn). Summarize with existing results."
                            )
                            backend = self.backend.backend_for(tool_name) if tool_name else "host"
                            self.records.append(ChatToolRecord("", tool_name, "budget_exceeded", backend, reason=msg[:200]))
                            budget_msg: dict[str, Any] = {"role": "tool", "name": tool_name, "content": msg}
                            if raw_call_id:
                                budget_msg["tool_call_id"] = raw_call_id
                            messages.append(budget_msg)
                            break
                        tool_calls_total += 1
                        prior_len = len(self.records)
                        raw_fn_sig = raw_call.get("function") if isinstance(raw_call, Mapping) else {}
                        sig_args = parse_tool_arguments(raw_fn_sig.get("arguments")) if isinstance(raw_fn_sig, Mapping) else {}
                        sig_key = f"{tool_name}:{json.dumps(sig_args, sort_keys=True, default=str)[:400]}"
                        call_signatures[sig_key] = call_signatures.get(sig_key, 0) + 1
                        obs, run_count = self._run_tool_call(
                            raw_call, run_count=run_count, failed_tools=failed_tools,
                        )
                        if call_signatures[sig_key] >= 2 and "[RECOVERY HINT]" not in obs:
                            GLOBAL_TELEMETRY.record("repeated_tool_loop_hint", tool=tool_name)
                            obs = obs + format_fallback_hint(
                                tool_name,
                                obs[:240],
                                available_tools=self._admitted_tool_names(),
                                tried_tools=failed_tools | {tool_name},
                                repeated=True,
                            )
                        if tool_name == "run_code" and len(self.records) > prior_len:
                            rec = self.records[-1]
                            if rec.status == "ok":
                                raw_fn = raw_call.get("function") if isinstance(raw_call, Mapping) else None
                                raw_args = parse_tool_arguments(raw_fn.get("arguments")) if isinstance(raw_fn, Mapping) else {}
                                ran_code = str(raw_args.get("code") or "")
                                if ran_code:
                                    verified_code_hashes.add(_code_digest(ran_code))
                            elif rec.status in _NON_RETRYABLE_VERIFY_STATUSES:
                                auto_verify_blocked = True
                        tool_msg: dict[str, Any] = {"role": "tool", "name": tool_name, "content": obs}
                        if raw_call_id:
                            tool_msg["tool_call_id"] = raw_call_id
                        messages.append(tool_msg)
                    if stopped_early:
                        break
                    digest, board_cursor = _delegation.unseen_peer_board_digest(board_cursor)
                    if digest:
                        messages.append({"role": "user", "content": "[PEER BOARD UPDATES]\n" + digest})
                else:
                    final_content = final_content or "Reached the per-turn tool-call limit before completing a final reply."

                if not final_content.strip():
                    final_content = "Turn ended without a final assistant answer."
                final_content = guard_chat_turn_hallucinations(final_content, self.records)
                _delegation.emit_agent_event(
                    phase="completed",
                    step=calls,
                    max_steps=self.budget.max_model_calls,
                    ok=True,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    summary=compact_text(final_content, limit=180),
                    model=str(last_meta.get("model") or ""),
                )
            finally:
                if unsub_agent is not None:
                    unsub_agent()
                if unsub_peer is not None:
                    unsub_peer()
        return ChatToolOutcome(
            content=final_content,
            records=tuple(self.records),
            model_calls=calls,
            usage={"tokens_in": total_in, "tokens_out": total_out, "token_source": "chat_tool_loop"},
            response_meta=last_meta,
        )


@contextmanager
def bind_turn(turn: ChatToolTurn | None) -> Iterator[ChatToolTurn | None]:
    token = _ACTIVE_TURN.set(turn)
    try:
        yield turn
    finally:
        _ACTIVE_TURN.reset(token)


def current_turn() -> ChatToolTurn | None:
    return _ACTIVE_TURN.get()


def build_tool_turn_manifest(
    *,
    admitted_tools: Sequence[str],
    user_run_command: bool,
    mode: str,
    container_active: bool = False,
    dynamic_discovery: bool = False,
) -> TurnToolManifest:
    """Build the authoritative ``TurnToolManifest`` for an active chat-tool turn."""
    callable_entries: list[CallableTool] = []
    seen: set[str] = set()
    for raw in admitted_tools:
        name = str(raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        if name == "run_code":
            backend = "container" if container_active else "host"
            note = f"python/js/powershell; mode={mode}; backend={backend}"
        elif name == "tool_search":
            note = "discover and dynamically load any additional Sonder tool into this turn"
        elif name == "scratchpad_read":
            note = "read, list, or search persistent Markdown scratchpad notes"
        elif name == "scratchpad_write":
            note = "save or append persistent Markdown working notes/plans"
        elif name == "scratchpad_delete":
            note = "delete one named scratchpad note"
        elif name == "reasoning_board":
            note = "structured facts/hypotheses/decisions audit board"
        elif name == "memory_search":
            note = "search learned local facts, lessons, and prior solutions"
        elif name == "learn_tool":
            note = "inspect an external CLI or package via live help/metadata"
        elif name == "report_progress":
            note = "post live progress/thinking updates"
        elif name in {"post_to_board", "read_board"}:
            note = "shared peer board coordination with @handle mentions"
        elif name in {"delegate_task", "spawn_agents"}:
            note = "delegate bounded depth-1 child tasks (depth 0 only)"
        elif name in {"ensemble_answer", "consult"}:
            note = "local multi-model synthesis or second opinion"
        else:
            note = f"mode={mode}"
        callable_entries.append(CallableTool(name, note))
    return TurnToolManifest(
        tuple(callable_entries),
        user_run_command=bool(user_run_command),
        dynamic_discovery=bool(dynamic_discovery),
    )


__all__ = [
    "ChatToolOutcome",
    "ChatToolTurn",
    "bind_turn",
    "build_tool_turn_manifest",
    "current_turn",
]
