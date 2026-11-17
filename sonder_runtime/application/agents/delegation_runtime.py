"""Application-layer runtime for depth-1 delegation, shared peer board, and live progress."""
from __future__ import annotations

import contextlib
import logging
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol

from ...domain.agents.delegation import (
    AGENT_PHASES,
    BOARD_MESSAGE_KINDS,
    DEFAULT_EVENT_BACKLOG,
    MAX_DELEGATION_DEPTH,
    AgentProgressEvent,
    DelegationChildResult,
    DelegationTaskSpec,
    PeerBoardMessage,
    compact_text,
    extract_mentions,
    make_event_timestamp,
    persona_for,
)
from ...domain.cloud_access import LEGACY_ERROR_PREFIX

_logger = logging.getLogger(__name__)


class DelegationExecutor(Protocol):
    """Host-supplied executor capable of running depth-1 child agent specs."""

    def __call__(
        self,
        specs: Sequence[DelegationTaskSpec],
        *,
        parent_agent_id: str,
        parent_project: str,
        parent_read_only: bool,
        board: AgentProgressBoard,
    ) -> tuple[DelegationChildResult, ...]: ...


class AgentProgressBoard:
    """Thread-safe shared progress & peer-coordination board for one run."""

    def __init__(self, *, run_id: str = "", max_events: int = DEFAULT_EVENT_BACKLOG) -> None:
        self.run_id = str(run_id or "").strip() or uuid.uuid4().hex[:12]
        self._max_events = max(16, int(max_events))
        self._lock = threading.RLock()
        self._seq = 0
        self._peer_seq = 0
        self._events: list[AgentProgressEvent] = []
        self._peer_messages: list[PeerBoardMessage] = []
        self._agents: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._subscribers: list[Callable[[AgentProgressEvent], None]] = []
        self._board_subscribers: list[Callable[[PeerBoardMessage], None]] = []

    def subscribe(self, callback: Callable[[AgentProgressEvent], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._subscribers.remove(callback)
                except ValueError:
                    pass

        return unsubscribe

    def subscribe_board(self, callback: Callable[[PeerBoardMessage], None]) -> Callable[[], None]:
        with self._lock:
            self._board_subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._board_subscribers.remove(callback)
                except ValueError:
                    pass

        return unsubscribe

    def emit(
        self,
        *,
        agent_id: str,
        parent_id: str = "",
        depth: int = 0,
        phase: str = "progress",
        tier: str = "",
        model: str = "",
        origin: str = "",
        step: int = 0,
        max_steps: int = 0,
        summary: str = "",
        thinking: str = "",
        tool: str = "",
        ok: bool | None = None,
        elapsed_ms: int | None = None,
    ) -> AgentProgressEvent:
        norm_phase = phase if phase in AGENT_PHASES else "progress"
        clean_agent = str(agent_id or "coordinator").strip() or "coordinator"
        with self._lock:
            self._seq += 1
            event = AgentProgressEvent(
                run_id=self.run_id,
                seq=self._seq,
                ts=make_event_timestamp(),
                agent_id=clean_agent,
                parent_id=str(parent_id or ""),
                depth=max(0, int(depth)),
                phase=norm_phase,
                tier=str(tier or ""),
                model=str(model or ""),
                origin=str(origin or ""),
                step=max(0, int(step)),
                max_steps=max(0, int(max_steps)),
                summary=compact_text(summary, limit=240),
                thinking=compact_text(thinking, limit=280),
                tool=str(tool or ""),
                ok=ok,
                elapsed_ms=elapsed_ms,
            )
            self._events.append(event)
            if len(self._events) > self._max_events:
                del self._events[: len(self._events) - self._max_events]
            state = dict(self._agents.get(clean_agent) or {})
            state.update({
                "agent_id": event.agent_id,
                "parent_id": event.parent_id,
                "depth": event.depth,
                "phase": event.phase,
                "tier": event.tier or state.get("tier", ""),
                "model": event.model or state.get("model", ""),
                "origin": event.origin or state.get("origin", ""),
                "step": event.step,
                "max_steps": event.max_steps or state.get("max_steps", 0),
                "summary": event.summary or state.get("summary", ""),
                "thinking": event.thinking or state.get("thinking", ""),
                "tool": event.tool,
                "ok": event.ok,
                "elapsed_ms": event.elapsed_ms,
                "seq": event.seq,
            })
            self._agents[clean_agent] = state
            subscribers = list(self._subscribers)
        for callback in subscribers:
            with contextlib.suppress(RuntimeError, ValueError, TypeError, OSError):
                callback(event)
        return event

    def post_peer_message(
        self,
        *,
        agent: str,
        text: str,
        kind: str = "chat",
        role: str = "",
        model: str = "",
        origin: str = "",
        personality: str = "",
    ) -> PeerBoardMessage:
        """Append one peer-to-peer message and notify board subscribers."""
        clean_agent = str(agent or "coordinator").strip() or "coordinator"
        norm_kind = str(kind or "chat").strip().lower()
        if norm_kind not in BOARD_MESSAGE_KINDS:
            norm_kind = "chat"
        clean_text = compact_text(text, limit=600)
        resolved_role, resolved_personality, _, _ = persona_for(
            clean_agent,
            explicit_role=role,
            explicit_personality=personality,
        )
        mentions = extract_mentions(clean_text)
        with self._lock:
            self._peer_seq += 1
            msg = PeerBoardMessage(
                run_id=self.run_id,
                seq=self._peer_seq,
                ts=make_event_timestamp(),
                agent=clean_agent,
                role=resolved_role,
                model=str(model or ""),
                origin=str(origin or ""),
                personality=resolved_personality,
                kind=norm_kind,
                text=clean_text,
                mentions=mentions,
            )
            self._peer_messages.append(msg)
            if len(self._peer_messages) > self._max_events:
                del self._peer_messages[: len(self._peer_messages) - self._max_events]
            board_subs = list(self._board_subscribers)
        for callback in board_subs:
            with contextlib.suppress(RuntimeError, ValueError, TypeError, OSError):
                callback(msg)
        return msg

    def read_peer_messages(
        self,
        *,
        since_seq: int = 0,
        limit: int = 20,
        mention_for: str = "",
        include_self: bool = True,
        caller: str = "",
    ) -> tuple[PeerBoardMessage, ...]:
        """Return peer messages after ``since_seq`` matching optional filters."""
        needle = str(mention_for or "").strip().lstrip("@").lower()
        caller_norm = str(caller or "").strip().lstrip("@").lower()
        with self._lock:
            rows = [msg for msg in self._peer_messages if msg.seq > max(0, int(since_seq))]
        if not include_self and caller_norm:
            rows = [msg for msg in rows if msg.agent.lower() != caller_norm]
        if needle:
            rows = [
                msg for msg in rows
                if any(m.lower() == needle for m in msg.mentions)
            ]
        if limit <= 0:
            return tuple(rows)
        return tuple(rows[-limit:])

    def format_peer_digest(
        self,
        *,
        since_seq: int = 0,
        for_agent: str = "",
        limit: int = 10,
    ) -> tuple[str, int]:
        """Format unread peer board messages for step-boundary injection."""
        with self._lock:
            rows = [msg for msg in self._peer_messages if msg.seq > max(0, int(since_seq))]
        target = str(for_agent or "").strip().lstrip("@").lower()
        if target:
            rows = [msg for msg in rows if msg.agent.lower() != target]
        if not rows:
            return "", max(0, int(since_seq))
        max_seq = rows[-1].seq
        shown = rows[-max(1, int(limit)) :]
        lines: list[str] = []
        for msg in shown:
            mentioned = bool(target and any(m.lower() == target for m in msg.mentions))
            tag = " [MENTIONED YOU]" if mentioned else ""
            lines.append(f"- #{msg.seq} @{msg.agent} ({msg.role}, {msg.kind}){tag}: {msg.text}")
        return "\n".join(lines), max_seq

    def events(self, *, limit: int = 50, agent_id: str = "") -> tuple[AgentProgressEvent, ...]:
        with self._lock:
            rows = self._events
            if agent_id:
                rows = [ev for ev in rows if ev.agent_id == agent_id]
            if limit <= 0:
                return tuple(rows)
            return tuple(rows[-limit:])

    def agent_states(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._agents.values()]

    def peer_summary_lines(self, *, limit: int = 10) -> list[str]:
        with self._lock:
            rows = self._peer_messages[-max(1, int(limit)) :]
        return [
            f"#{msg.seq} @{msg.agent} [{msg.kind}]: {compact_text(msg.text, limit=160)}"
            for msg in rows
        ]

    def summary_lines(self, *, limit: int = 12) -> list[str]:
        states = self.agent_states()
        lines: list[str] = []
        for row in states[: max(1, int(limit))]:
            origin = row.get("origin") or row.get("tier") or row.get("model") or ""
            origin_part = f" [{origin}]" if origin else ""
            step = int(row.get("step") or 0)
            max_steps = int(row.get("max_steps") or 0)
            step_part = f" step {step}/{max_steps}" if max_steps > 0 else (f" step {step}" if step > 0 else "")
            msg = row.get("summary") or row.get("thinking") or ""
            lines.append(f"{row['agent_id']} (d={row['depth']} {row['phase']}{origin_part}{step_part}): {msg}".rstrip())
        return lines


_AGENT_DEPTH: ContextVar[int] = ContextVar("sonder_agent_depth", default=0)
_CURRENT_AGENT_ID: ContextVar[str] = ContextVar("sonder_agent_id", default="coordinator")
_PARENT_AGENT_ID: ContextVar[str] = ContextVar("sonder_parent_agent_id", default="")
_AGENT_ROLE: ContextVar[str] = ContextVar("sonder_agent_role", default="coordinator")
_AGENT_PERSONALITY: ContextVar[str] = ContextVar("sonder_agent_personality", default="")
_AGENT_MODEL: ContextVar[str] = ContextVar("sonder_agent_model", default="")
_AGENT_ORIGIN: ContextVar[str] = ContextVar("sonder_agent_origin", default="")
_ACTIVE_BOARD: ContextVar[AgentProgressBoard | None] = ContextVar("sonder_agent_board", default=None)
_DELEGATION_EXECUTOR: ContextVar[DelegationExecutor | None] = ContextVar(
    "sonder_delegation_executor", default=None,
)


def current_depth() -> int:
    return max(0, int(_AGENT_DEPTH.get()))


def can_delegate(*, max_depth: int = MAX_DELEGATION_DEPTH) -> bool:
    return current_depth() < max(0, int(max_depth))


def current_agent_id() -> str:
    return str(_CURRENT_AGENT_ID.get() or "coordinator")


def parent_agent_id() -> str:
    return str(_PARENT_AGENT_ID.get() or "")


def current_agent_role() -> str:
    return str(_AGENT_ROLE.get() or "coordinator")


def current_agent_personality() -> str:
    return str(_AGENT_PERSONALITY.get() or "")


def current_agent_model() -> str:
    return str(_AGENT_MODEL.get() or "")


def current_agent_origin() -> str:
    return str(_AGENT_ORIGIN.get() or "")


def current_board() -> AgentProgressBoard | None:
    return _ACTIVE_BOARD.get()


def current_delegation_executor() -> DelegationExecutor | None:
    return _DELEGATION_EXECUTOR.get()


@contextmanager
def depth_scope(depth: int) -> Iterator[int]:
    """Bind the active agent nesting depth for this context."""
    bounded = max(0, int(depth))
    token = _AGENT_DEPTH.set(bounded)
    try:
        yield bounded
    finally:
        _AGENT_DEPTH.reset(token)


@contextmanager
def agent_identity_scope(
    agent_id: str,
    *,
    parent_id: str = "",
    role: str = "",
    personality: str = "",
    model: str = "",
    origin: str = "",
) -> Iterator[str]:
    """Bind the active agent handle, parent, role, personality, model, and origin."""
    clean_id = str(agent_id or "coordinator").strip() or "coordinator"
    resolved_role, resolved_personality, _, _ = persona_for(
        clean_id,
        explicit_role=role,
        explicit_personality=personality,
    )
    t_id = _CURRENT_AGENT_ID.set(clean_id)
    t_parent = _PARENT_AGENT_ID.set(str(parent_id or "").strip())
    t_role = _AGENT_ROLE.set(resolved_role)
    t_pers = _AGENT_PERSONALITY.set(resolved_personality)
    t_model = _AGENT_MODEL.set(str(model or "").strip())
    t_origin = _AGENT_ORIGIN.set(str(origin or "").strip())
    try:
        yield clean_id
    finally:
        _AGENT_ORIGIN.reset(t_origin)
        _AGENT_MODEL.reset(t_model)
        _AGENT_PERSONALITY.reset(t_pers)
        _AGENT_ROLE.reset(t_role)
        _PARENT_AGENT_ID.reset(t_parent)
        _CURRENT_AGENT_ID.reset(t_id)


@contextmanager
def bind_board(board: AgentProgressBoard | None) -> Iterator[AgentProgressBoard | None]:
    token = _ACTIVE_BOARD.set(board)
    try:
        yield board
    finally:
        _ACTIVE_BOARD.reset(token)


@contextmanager
def bind_delegation_executor(executor: DelegationExecutor | None) -> Iterator[DelegationExecutor | None]:
    token = _DELEGATION_EXECUTOR.set(executor)
    try:
        yield executor
    finally:
        _DELEGATION_EXECUTOR.reset(token)


def depth_refusal_message(
    tool_name: str,
    *,
    depth: int | None = None,
    max_depth: int = MAX_DELEGATION_DEPTH,
) -> str:
    """Standard deterministic refusal when a depth>=1 child attempts delegation."""
    active_depth = current_depth() if depth is None else max(0, int(depth))
    msg = (
        f"{LEGACY_ERROR_PREFIX} {tool_name} is blocked at delegation depth {active_depth} "
        f"(maximum depth is {max_depth}; delegated child agents cannot spawn further agents)"
    )
    return msg


def emit_agent_event(
    *,
    phase: str = "progress",
    summary: str = "",
    thinking: str = "",
    tool: str = "",
    step: int = 0,
    max_steps: int = 0,
    tier: str = "",
    model: str = "",
    origin: str = "",
    ok: bool | None = None,
    elapsed_ms: int | None = None,
    agent_id: str | None = None,
    parent_id: str | None = None,
    depth: int | None = None,
) -> AgentProgressEvent | None:
    """Emit a live progress event onto the active board if one is bound."""
    board = _ACTIVE_BOARD.get()
    if board is None:
        return None
    return board.emit(
        agent_id=current_agent_id() if agent_id is None else agent_id,
        parent_id=parent_agent_id() if parent_id is None else parent_id,
        depth=current_depth() if depth is None else depth,
        phase=phase,
        tier=tier,
        model=model or current_agent_model(),
        origin=origin or current_agent_origin(),
        step=step,
        max_steps=max_steps,
        summary=summary,
        thinking=thinking,
        tool=tool,
        ok=ok,
        elapsed_ms=elapsed_ms,
    )


def report_progress(
    summary: str,
    *,
    phase: str = "progress",
    thinking: str = "",
    tool: str = "",
    step: int = 0,
    max_steps: int = 0,
    ok: bool | None = None,
) -> dict[str, Any]:
    """Post an explicit progress/thinking update to the shared board."""
    clean_summary = compact_text(summary, limit=240)
    clean_thinking = compact_text(thinking, limit=280)
    if not clean_summary and not clean_thinking:
        return {"ok": False, "error": "progress summary must not be empty"}
    board = _ACTIVE_BOARD.get()
    if board is None:
        return {
            "ok": True,
            "recorded": False,
            "agent_id": current_agent_id(),
            "depth": current_depth(),
            "phase": phase,
            "summary": clean_summary,
            "thinking": clean_thinking,
        }
    event = emit_agent_event(
        phase=phase if phase in AGENT_PHASES else "progress",
        summary=clean_summary,
        thinking=clean_thinking,
        tool=tool,
        step=step,
        max_steps=max_steps,
        ok=ok,
    )
    return {
        "ok": True,
        "recorded": event is not None,
        "run_id": board.run_id,
        "seq": event.seq if event is not None else 0,
        "agent_id": current_agent_id(),
        "depth": current_depth(),
        "phase": event.phase if event is not None else phase,
        "summary": clean_summary,
    }


def post_to_board(
    text: str,
    *,
    kind: str = "chat",
    model: str = "",
    origin: str = "",
) -> dict[str, Any]:
    """Post a peer-to-peer coordination message onto the active shared run board."""
    clean_text = compact_text(text, limit=600)
    if not clean_text:
        return {"ok": False, "error": "board message text must not be empty"}
    board = _ACTIVE_BOARD.get()
    agent = current_agent_id()
    role = current_agent_role()
    personality = current_agent_personality()
    if not personality:
        role, personality, _, _ = persona_for(agent, explicit_role=role)
    resolved_model = str(model or current_agent_model() or "")
    resolved_origin = str(origin or current_agent_origin() or "")
    if board is None:
        return {
            "ok": True,
            "recorded": False,
            "agent": agent,
            "role": role,
            "model": resolved_model,
            "origin": resolved_origin,
            "personality": personality,
            "kind": kind if kind in BOARD_MESSAGE_KINDS else "chat",
            "text": clean_text,
            "mentions": list(extract_mentions(clean_text)),
        }
    msg = board.post_peer_message(
        agent=agent,
        role=role,
        model=resolved_model,
        origin=resolved_origin,
        personality=personality,
        kind=kind,
        text=clean_text,
    )
    return {"ok": True, "recorded": True, **msg.to_dict()}


def read_board_messages(
    *,
    since_seq: int = 0,
    limit: int = 20,
    mentions_only: bool = False,
) -> dict[str, Any]:
    """Read peer coordination messages from the active shared run board."""
    board = _ACTIVE_BOARD.get()
    if board is None:
        return {
            "ok": True,
            "active": False,
            "agent": current_agent_id(),
            "messages": [],
        }
    mention_target = current_agent_id() if mentions_only else ""
    msgs = board.read_peer_messages(
        since_seq=since_seq,
        limit=limit,
        mention_for=mention_target,
    )
    return {
        "ok": True,
        "active": True,
        "run_id": board.run_id,
        "agent": current_agent_id(),
        "messages": [item.to_dict() for item in msgs],
    }


def unseen_peer_board_digest(since_seq: int, *, limit: int = 10) -> tuple[str, int]:
    """Return formatted unseen peer board updates for the current agent + next seq."""
    board = _ACTIVE_BOARD.get()
    if board is None:
        return "", max(0, int(since_seq))
    return board.format_peer_digest(
        since_seq=since_seq,
        for_agent=current_agent_id(),
        limit=limit,
    )


def run_delegated_specs(
    specs: Sequence[DelegationTaskSpec],
    *,
    tool_name: str = "delegate_task",
    parent_project: str = "",
    parent_read_only: bool = False,
) -> tuple[DelegationChildResult, ...]:
    """Validate depth, bind board if absent, and invoke the host delegation executor."""
    if not can_delegate():
        raise PermissionError(depth_refusal_message(tool_name))
    executor = _DELEGATION_EXECUTOR.get()
    if executor is None:
        raise RuntimeError(
            f"{LEGACY_ERROR_PREFIX} {tool_name} has no active delegation executor in this context"
        )
    board = _ACTIVE_BOARD.get() or AgentProgressBoard()
    parent_id = current_agent_id()
    for spec in specs:
        board.emit(
            agent_id=spec.agent_id,
            parent_id=parent_id,
            depth=current_depth() + 1,
            phase="queued",
            tier=spec.tier,
            model=spec.model,
            summary=f"queued task ({spec.role}): {compact_text(spec.task, limit=160)}",
        )
    return executor(
        specs,
        parent_agent_id=parent_id,
        parent_project=parent_project,
        parent_read_only=parent_read_only,
        board=board,
    )


def delegation_status_snapshot() -> Mapping[str, Any]:
    board = _ACTIVE_BOARD.get()
    if board is None:
        return {
            "active": False,
            "depth": current_depth(),
            "agent_id": current_agent_id(),
            "events": [],
            "peer_messages": [],
            "agents": [],
        }
    return {
        "active": True,
        "run_id": board.run_id,
        "depth": current_depth(),
        "agent_id": current_agent_id(),
        "agents": board.agent_states(),
        "events": [ev.to_dict() for ev in board.events(limit=30)],
        "peer_messages": [msg.to_dict() for msg in board.read_peer_messages(limit=30)],
    }


_EPHEMERAL_LOCK = threading.Lock()
_EPHEMERAL_SEQ = 0
_EPHEMERAL_CHILDREN: dict[str, dict[str, Any]] = {}
_EPHEMERAL_EVENTS: list[dict[str, str]] = []


def _next_eph_seq() -> int:
    global _EPHEMERAL_SEQ
    _EPHEMERAL_SEQ += 1
    return _EPHEMERAL_SEQ


def register_ephemeral_child(
    agent_id: str,
    task: str,
    *,
    parent_id: str = "",
    tier: str = "",
    model: str = "",
    origin: str = "",
    project: str = "",
    activity: str = "queued",
) -> None:
    """Register an in-memory depth-1 child run so ``master_status()`` sees it live."""
    now = time.time()
    with _EPHEMERAL_LOCK:
        _EPHEMERAL_CHILDREN[agent_id] = {
            "id": agent_id,
            "role": "delegated-child",
            "parent_id": parent_id,
            "task": task[:240],
            "status": "queued",
            "activity": activity[:200],
            "started_ts": now,
            "updated_ts": now,
            "updated_seq": _next_eph_seq(),
            "finished_ts": None,
            "tier": tier,
            "model": model,
            "origin": origin,
            "project": project,
            "depth": 1,
            "in_model_call": False,
            "cancel_requested": False,
            "ephemeral": True,
        }


def update_ephemeral_child(agent_id: str, **changes: Any) -> None:
    """Update status/activity/origin fields of a registered depth-1 child."""
    with _EPHEMERAL_LOCK:
        row = _EPHEMERAL_CHILDREN.get(agent_id)
        if row is None:
            return
        row.update(changes)
        row["updated_ts"] = time.time()
        row["updated_seq"] = _next_eph_seq()


def finish_ephemeral_child(
    agent_id: str,
    *,
    status: str = "completed",
    activity: str = "",
    error: str = "",
) -> None:
    """Mark a depth-1 child finished and prune old finished ephemeral entries."""
    now = time.time()
    with _EPHEMERAL_LOCK:
        row = _EPHEMERAL_CHILDREN.get(agent_id)
        if row is not None:
            row["status"] = status
            if activity:
                row["activity"] = activity[:200]
            if error:
                row["error"] = error[:240]
            row["in_model_call"] = False
            row["updated_ts"] = now
            row["finished_ts"] = now
            row["updated_seq"] = _next_eph_seq()
        done_rows = sorted(
            (r for r in _EPHEMERAL_CHILDREN.values() if r.get("status") not in ("queued", "running")),
            key=lambda r: int(r.get("updated_seq") or 0),
            reverse=True,
        )
        for stale in done_rows[64:]:
            _EPHEMERAL_CHILDREN.pop(stale["id"], None)


def record_ephemeral_event(agent_id: str, message: str) -> None:
    """Append a bounded event entry for an in-flight depth-1 child."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    with _EPHEMERAL_LOCK:
        _EPHEMERAL_EVENTS.append({
            "ts": stamp,
            "agent_id": agent_id,
            "message": str(message or "")[:240],
        })
        del _EPHEMERAL_EVENTS[:-200]


def merge_ephemeral_snapshot(
    data: dict[str, Any],
    *,
    include_finished: bool = True,
    limit: int = 20,
) -> dict[str, Any]:
    """Merge live depth-1 ephemeral child rows and peer events into a ``master_orchestrator.snapshot``."""
    with _EPHEMERAL_LOCK:
        ephemeral_rows = [dict(r) for r in _EPHEMERAL_CHILDREN.values()]
        ephemeral_events = list(_EPHEMERAL_EVENTS[-200:])
    if ephemeral_rows:
        active_eph = [r for r in ephemeral_rows if r.get("status") in ("queued", "running")]
        shown_eph = ephemeral_rows if include_finished else active_eph
        existing_ids = {str(r.get("id") or "") for r in (data.get("agents") or [])}
        merged_agents = list(data.get("agents") or [])
        for erow in shown_eph:
            if erow["id"] not in existing_ids:
                merged_agents.append(erow)
        merged_agents.sort(key=lambda r: float(r.get("updated_seq") or r.get("updated_ts") or 0), reverse=True)
        bounded_limit = max(1, int(limit or 20))
        data["agents"] = merged_agents[:bounded_limit]
        data["active_agents"] = int(data.get("active_agents") or 0) + len(active_eph)
        data["running_agents"] = int(data.get("running_agents") or 0) + sum(
            1 for r in active_eph if r.get("status") == "running"
        )
        data["queued_agents"] = int(data.get("queued_agents") or 0) + sum(
            1 for r in active_eph if r.get("status") == "queued"
        )
        data["active_model_calls"] = int(data.get("active_model_calls") or 0) + sum(
            1 for r in active_eph if r.get("in_model_call")
        )
        data["total_agents"] = int(data.get("total_agents") or 0) + len(ephemeral_rows)
        data["total_listed"] = len(data["agents"])
    if ephemeral_events:
        data["events"] = (list(data.get("events") or []) + ephemeral_events)[-200:]
    return data


def clear_ephemeral_state() -> None:
    """Reset in-memory ephemeral child rows and events (used by test teardown)."""
    with _EPHEMERAL_LOCK:
        _EPHEMERAL_CHILDREN.clear()
        _EPHEMERAL_EVENTS.clear()


__all__ = [
    "AgentProgressBoard",
    "DelegationExecutor",
    "agent_identity_scope",
    "bind_board",
    "bind_delegation_executor",
    "can_delegate",
    "clear_ephemeral_state",
    "current_agent_id",
    "current_agent_model",
    "current_agent_origin",
    "current_agent_personality",
    "current_agent_role",
    "current_board",
    "current_delegation_executor",
    "current_depth",
    "delegation_status_snapshot",
    "depth_refusal_message",
    "depth_scope",
    "emit_agent_event",
    "finish_ephemeral_child",
    "merge_ephemeral_snapshot",
    "parent_agent_id",
    "post_to_board",
    "read_board_messages",
    "record_ephemeral_event",
    "register_ephemeral_child",
    "report_progress",
    "run_delegated_specs",
    "unseen_peer_board_digest",
    "update_ephemeral_child",
]
