"""Bounded interactive chat-tool turn orchestration (`ChatToolTurn`).

Drives native tool-calling turns inside ``/v1/chat/completions`` and ``sonder()``
with strict wall/step/run budgets, mid-turn operator approval waits via
``ApprovalLedger.open_for`` / ``denied_for``, SSE ``sonder_tool`` event
callbacks, and ``TurnToolManifest`` honesty checks.
"""
from __future__ import annotations

import contextvars
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any

from sonder_runtime.application.agents import delegation_runtime as _delegation
from sonder_runtime.application.chat.honesty import guard_unverified_run_claim
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

_ACTIVE_TURN: contextvars.ContextVar["ChatToolTurn | None"] = contextvars.ContextVar(
    "sonder_active_chat_tool_turn", default=None,
)

_EXECUTION_CAP_TOOLS = frozenset({
    "run_code", "run_project", "workspace_run", "script_run",
    "test_run", "lint_run", "format_code", "typecheck_run", "build_run",
})
_CREDENTIAL_KEYS = frozenset({"token", "approval", "extra_roots"})


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
    records: list[ChatToolRecord] = field(default_factory=list)

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
        return text[:limit] + f"\n... (truncated {len(text) - limit} chars)", True

    def _run_tool_call(self, raw_call: Mapping[str, Any], *, run_count: int) -> tuple[str, int]:
        fn = raw_call.get("function") if isinstance(raw_call, Mapping) else None
        if not isinstance(fn, Mapping):
            return "ERROR: malformed native tool call", run_count
        name = str(fn.get("name") or "").strip().lstrip("/")
        args = parse_tool_arguments(fn.get("arguments"))
        backend = self.backend.backend_for(name)
        if not name:
            return "ERROR: tool name is required", run_count
        if name in DELEGATION_TOOLS and not _delegation.can_delegate():
            msg = _delegation.depth_refusal_message(name)
            self.records.append(ChatToolRecord("", name, "refused", backend, reason=msg[:200]))
            _delegation.emit_agent_event(phase="refused", tool=name, ok=False, summary=msg)
            return msg, run_count
        if name in self.blocked_tools and not self.allow_system_tools:
            msg = f"ERROR: HOST POLICY: tool '{name}' is restricted to operator control surfaces."
            self.records.append(ChatToolRecord("", name, "refused", backend, reason=msg[:200]))
            return msg, run_count
        if any(isinstance(args.get(key), str) and args.get(key) for key in _CREDENTIAL_KEYS):
            msg = f"ERROR: HOST POLICY: tool '{name}' cannot receive caller credential arguments."
            self.records.append(ChatToolRecord("", name, "refused", backend, reason=msg[:200]))
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

        decision = self.permission_policy.decide(
            name,
            interactive=False,
            surface="chat-tool",
            arguments=args,
        )
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
                    decision = self.permission_policy.decide(
                        name, interactive=False, surface="chat-tool", arguments=args,
                    )
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
                return msg, run_count

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
        with preapproved_ctx:
            exec_result = self.backend.execute(name, args)
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
        return clipped_model, new_runs

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

        started = time.monotonic()
        run_count = 0
        calls = 0
        total_in = 0
        total_out = 0
        last_meta: dict[str, Any] = {}
        final_content = ""
        board_cursor = 0
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
                    step_tools = self.tools_payload if calls < self.budget.max_model_calls else None
                    step = generate_step(messages, step_tools)
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
                        final_content = content
                        break

                    assistant_msg: dict[str, Any] = {"role": "assistant", "content": content}
                    assistant_msg["tool_calls"] = list(tool_calls)
                    messages.append(assistant_msg)
                    for raw_call in tool_calls:
                        obs, run_count = self._run_tool_call(raw_call, run_count=run_count)
                        tool_name = str(((raw_call.get("function") or {}) if isinstance(raw_call, Mapping) else {}).get("name") or "")
                        messages.append({"role": "tool", "name": tool_name, "content": obs})
                    digest, board_cursor = _delegation.unseen_peer_board_digest(board_cursor)
                    if digest:
                        messages.append({"role": "user", "content": "[PEER BOARD UPDATES]\n" + digest})
                else:
                    final_content = final_content or "Reached the per-turn tool-call limit before completing a final reply."

                if not final_content.strip():
                    final_content = "Turn ended without a final assistant answer."
                final_content = guard_unverified_run_claim(final_content, self.records)
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
            note = "discover other Sonder tools by query or name"
        elif name == "report_progress":
            note = "post live progress/thinking updates"
        elif name in {"post_to_board", "read_board"}:
            note = "shared peer board coordination with @handle mentions"
        elif name in {"delegate_task", "spawn_agents"}:
            note = "delegate bounded depth-1 child tasks (depth 0 only)"
        else:
            note = f"mode={mode}"
        callable_entries.append(CallableTool(name, note))
    return TurnToolManifest(tuple(callable_entries), user_run_command=bool(user_run_command))


__all__ = [
    "ChatToolOutcome",
    "ChatToolTurn",
    "bind_turn",
    "build_tool_turn_manifest",
    "current_turn",
]
