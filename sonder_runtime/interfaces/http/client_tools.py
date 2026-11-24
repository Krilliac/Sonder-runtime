"""Client-side tool execution broker (`execution_target="client"`).

Manages pending client tool calls created during streamed or non-streamed HTTP
chat turns, verifies principal + session ownership on
``POST /v1/client-tools/<call_id>/result``, enforces bounded payload sizes and
idempotent/one-shot submission semantics, and wakes waiting chat turns.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

TOOL_BACKEND_CLIENT = "client"
_CREDENTIAL_ARG_KEYS: frozenset[str] = frozenset({
    "token",
    "approval",
    "extra_roots",
    "secret",
    "api_key",
    "password",
})


def _sanitize_args(args: Mapping[str, Any] | None) -> dict[str, Any]:
    if not args:
        return {}
    return {str(k): v for k, v in args.items() if str(k) not in _CREDENTIAL_ARG_KEYS}


_RISK_DEFAULT = "execute"
_READ_ONLY_TOOLS = frozenset({
    "file_read", "file_find", "directory_tree", "text_search",
    "git_status", "git_diff", "git_log", "git_branch",
})
_WRITE_TOOLS = frozenset({"file_write", "file_edit", "file_delete", "directory_create"})


def _default_risk(tool_name: str) -> str:
    if tool_name in _READ_ONLY_TOOLS:
        return "read_only"
    if tool_name in _WRITE_TOOLS:
        return "write"
    return _RISK_DEFAULT

MAX_CLIENT_OUTPUT_CHARS = 65_536
MAX_CLIENT_ERROR_CHARS = 4_096
MAX_CLIENT_ARGS_BYTES = 65_536
MAX_PENDING_CALLS_GLOBAL = 256
MAX_PENDING_CALLS_PER_PRINCIPAL = 32
MAX_TOTAL_CALLS = 768
DEFAULT_CLIENT_TOOL_TIMEOUT_SECONDS = 120.0
MAX_CLIENT_TOOL_TIMEOUT_SECONDS = 300.0
DEFAULT_NON_STREAM_HANDOFF_TTL_SECONDS = 300.0
_MAX_COMPLETED_RECORDS = 512
_CALL_ID_RE = re.compile(r"^[0-9a-f]{8,64}$")
_RESULT_ALLOWED_KEYS: frozenset[str] = frozenset({"ok", "output", "error", "denied", "session"})


class ClientToolBrokerCapacityError(RuntimeError):
    """Raised when pending client tool calls exceed global or per-principal limits."""

    status_code = 429
    code = "TOO_MANY_PENDING_CLIENT_CALLS"


class ClientToolArgsTooLargeError(ValueError):
    """Raised when serialized client tool arguments exceed ``MAX_CLIENT_ARGS_BYTES``."""

    status_code = 413
    code = "PAYLOAD_TOO_LARGE"


@dataclass(frozen=True)
class ClientToolPayload:
    ok: bool
    output: str
    error: str
    denied: bool
    digest: str


@dataclass
class ClientToolCall:
    call_id: str
    principal: str
    session: str
    tool: str
    args: dict[str, Any]
    risk: str
    created_at: float
    expires_at: float
    mode: str = "stream"
    event: threading.Event = field(default_factory=threading.Event, repr=False)
    status: str = "awaiting_client_result"
    result: ClientToolPayload | None = None

    def sse_event(self, phase: str | None = None) -> dict[str, Any]:
        """Return the ``sonder_tool`` wire payload for SSE streaming."""
        active_phase = phase or self.status
        state = "running" if active_phase == "awaiting_client_result" else active_phase
        payload: dict[str, Any] = {
            "execution_target": TOOL_BACKEND_CLIENT,
            "backend": TOOL_BACKEND_CLIENT,
            "phase": active_phase,
            "state": state,
            "call_id": self.call_id,
            "tool": self.tool,
            "args": dict(self.args),
            "risk": self.risk,
            "timeout_seconds": max(1, int(round(self.expires_at - self.created_at))),
        }
        if self.session:
            payload["session"] = self.session
        if self.result is not None:
            payload["ok"] = self.result.ok
            if self.result.denied:
                payload["denied"] = True
            if self.result.error:
                payload["error"] = self.result.error[:512]
        return payload

    def handoff_metadata(self) -> dict[str, Any]:
        """Return structured metadata for non-stream ``sonder_receipt.client_tool_handoff``."""
        meta: dict[str, Any] = {
            "execution_target": TOOL_BACKEND_CLIENT,
            "phase": self.status,
            "call_id": self.call_id,
            "tool": self.tool,
            "args": dict(self.args),
            "risk": self.risk,
            "result_endpoint": f"/v1/client-tools/{self.call_id}/result",
            "expires_at": int(self.expires_at),
        }
        if self.session:
            meta["session"] = self.session
        return meta


@dataclass(frozen=True)
class ClientToolSubmitResult:
    status: int
    body: dict[str, Any]


@dataclass(frozen=True)
class ClientToolWaitOutcome:
    call_id: str
    status: str  # "finished" | "denied" | "error" | "expired"
    ok: bool
    output: str
    error: str
    denied: bool
    elapsed_ms: int


def is_valid_client_call_id(call_id: object) -> bool:
    return bool(_CALL_ID_RE.match(str(call_id or "").strip().lower()))


def _canonical_result_digest(ok: bool, output: str, error: str, denied: bool) -> str:
    encoded = json.dumps(
        {"ok": ok, "output": output, "error": error, "denied": denied},
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _shrink_completed_call(call: ClientToolCall, *, clear_args: bool = True) -> None:
    """Drop large result/argument payloads after completion while preserving digest and summary metadata."""
    res = call.result
    if res is not None and (res.output or len(res.error) > 512):
        call.result = ClientToolPayload(
            ok=res.ok,
            output="",
            error=res.error[:512],
            denied=res.denied,
            digest=res.digest,
        )
    if clear_args and call.args:
        call.args = {}


class ClientToolBroker:
    """In-memory per-listener registry of pending and completed client tool calls."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        max_pending_calls: int = MAX_PENDING_CALLS_GLOBAL,
        max_pending_per_principal: int = MAX_PENDING_CALLS_PER_PRINCIPAL,
        max_total_calls: int = MAX_TOTAL_CALLS,
        max_args_bytes: int = MAX_CLIENT_ARGS_BYTES,
    ) -> None:
        self._lock = threading.Lock()
        self._calls: dict[str, ClientToolCall] = {}
        self._clock: Callable[[], float] = clock or time.time
        self._max_pending_calls = max(1, int(max_pending_calls))
        self._max_pending_per_principal = max(1, int(max_pending_per_principal))
        self._max_total_calls = max(self._max_pending_calls, int(max_total_calls))
        self._max_args_bytes = max(256, int(max_args_bytes))

    def create_call(
        self,
        *,
        principal: str,
        session: str,
        tool: str,
        args: Mapping[str, Any] | None,
        risk: str | None = None,
        timeout_seconds: float = DEFAULT_CLIENT_TOOL_TIMEOUT_SECONDS,
        mode: str = "stream",
    ) -> ClientToolCall:
        sanitized_args = _sanitize_args(args)
        try:
            encoded_args = json.dumps(sanitized_args, ensure_ascii=False, default=str).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"client tool args are not serializable: {exc}") from exc
        if len(encoded_args) > self._max_args_bytes:
            raise ClientToolArgsTooLargeError(
                f"client tool args exceed {self._max_args_bytes} bytes"
            )
        now = self._clock()
        ttl = max(1.0, min(float(timeout_seconds), MAX_CLIENT_TOOL_TIMEOUT_SECONDS))
        clean_tool = str(tool or "").strip().lstrip("/")
        clean_principal = str(principal or "").strip()
        call = ClientToolCall(
            call_id=secrets.token_hex(16),
            principal=clean_principal,
            session=str(session or "").strip(),
            tool=clean_tool,
            args=sanitized_args,
            risk=str(risk or _default_risk(clean_tool)),
            created_at=now,
            expires_at=now + ttl,
            mode=mode,
        )
        with self._lock:
            self._prune_locked(now)
            global_pending = 0
            principal_pending = 0
            for item in self._calls.values():
                if item.status == "awaiting_client_result":
                    global_pending += 1
                    if item.principal == clean_principal:
                        principal_pending += 1
            if global_pending >= self._max_pending_calls:
                raise ClientToolBrokerCapacityError(
                    f"too many pending client tool calls (limit {self._max_pending_calls})"
                )
            if principal_pending >= self._max_pending_per_principal:
                raise ClientToolBrokerCapacityError(
                    f"too many pending client tool calls for principal (limit {self._max_pending_per_principal})"
                )
            self._calls[call.call_id] = call
            self._enforce_total_cap_locked()
        return call

    def _enforce_total_cap_locked(self) -> None:
        completed = [
            (item.created_at, cid)
            for cid, item in self._calls.items()
            if item.status != "awaiting_client_result"
        ]
        completed_excess = len(completed) - _MAX_COMPLETED_RECORDS
        total_excess = len(self._calls) - self._max_total_calls
        to_evict = max(completed_excess, total_excess, 0)
        if to_evict <= 0:
            return
        completed.sort(key=lambda pair: pair[0])
        for _, cid in completed[:to_evict]:
            self._calls.pop(cid, None)

    def _prune_locked(self, now: float) -> None:
        to_remove: list[str] = []
        for cid, item in self._calls.items():
            if item.status == "awaiting_client_result":
                if now > item.expires_at + 300.0:
                    to_remove.append(cid)
                elif now > item.expires_at:
                    item.status = "expired"
                    item.event.set()
                    item.args = {}
            else:
                if now - item.created_at > 600.0:
                    to_remove.append(cid)
                elif item.mode != "stream" or now - item.created_at > 30.0:
                    _shrink_completed_call(item, clear_args=True)
        for cid in to_remove:
            self._calls.pop(cid, None)
        self._enforce_total_cap_locked()

    def wait_for_result(
        self,
        call_id: str,
        *,
        timeout_seconds: float | None = None,
        heartbeat_interval: float = 5.0,
        on_wait: Callable[[], None] | None = None,
        is_aborted: Callable[[], bool] | None = None,
    ) -> ClientToolWaitOutcome:
        with self._lock:
            call = self._calls.get(call_id)
        if call is None:
            return ClientToolWaitOutcome(
                call_id=call_id,
                status="expired",
                ok=False,
                output="",
                error="client tool call is no longer registered",
                denied=False,
                elapsed_ms=0,
            )
        started = self._clock()
        deadline = call.expires_at
        if timeout_seconds is not None:
            deadline = min(deadline, started + max(0.05, float(timeout_seconds)))
        while not call.event.is_set():
            if is_aborted is not None and is_aborted():
                with self._lock:
                    if not call.event.is_set():
                        call.status = "expired"
                        call.event.set()
                        call.args = {}
                    else:
                        break
                elapsed = max(0, int(round((self._clock() - started) * 1000)))
                return ClientToolWaitOutcome(
                    call_id=call.call_id,
                    status="expired",
                    ok=False,
                    output="",
                    error="client disconnected while waiting for client tool result",
                    denied=False,
                    elapsed_ms=elapsed,
                )
            now = self._clock()
            remaining = deadline - now
            if remaining <= 0:
                with self._lock:
                    if not call.event.is_set():
                        call.status = "expired"
                        call.event.set()
                        call.args = {}
                    else:
                        break
                elapsed = max(0, int(round((self._clock() - started) * 1000)))
                return ClientToolWaitOutcome(
                    call_id=call.call_id,
                    status="expired",
                    ok=False,
                    output="",
                    error=f"timed out waiting for client tool result after {int(max(1, round(deadline - started)))}s",
                    denied=False,
                    elapsed_ms=elapsed,
                )
            step = min(remaining, max(0.05, float(heartbeat_interval)))
            if call.event.wait(step):
                break
            if is_aborted is not None and is_aborted():
                continue
            if on_wait is not None:
                on_wait()
        elapsed = max(0, int(round((self._clock() - started) * 1000)))
        with self._lock:
            result = call.result
            status = call.status
            if result is not None:
                _shrink_completed_call(call, clear_args=False)
        if result is None:
            return ClientToolWaitOutcome(
                call_id=call.call_id,
                status=status or "expired",
                ok=False,
                output="",
                error="client tool call expired without result",
                denied=False,
                elapsed_ms=elapsed,
            )
        return ClientToolWaitOutcome(
            call_id=call.call_id,
            status=status,
            ok=result.ok,
            output=result.output,
            error=result.error,
            denied=result.denied,
            elapsed_ms=elapsed,
        )

    def submit_result(
        self,
        call_id: str,
        body: Any,
        *,
        principal: str,
        session: str = "",
    ) -> ClientToolSubmitResult:
        cid = str(call_id or "").strip().lower()
        if not is_valid_client_call_id(cid):
            return ClientToolSubmitResult(
                400,
                {
                    "error": {
                        "message": "call_id must be 8 to 64 lowercase hexadecimal characters",
                        "type": "invalid_request",
                        "code": "INVALID_CALL_ID",
                    }
                },
            )
        if not isinstance(body, Mapping):
            return ClientToolSubmitResult(
                400,
                {
                    "error": {
                        "message": "request body must be a JSON object",
                        "type": "invalid_request",
                        "code": "INVALID_REQUEST",
                    }
                },
            )
        unknown = sorted(str(k) for k in set(body) - _RESULT_ALLOWED_KEYS)
        if unknown:
            return ClientToolSubmitResult(
                400,
                {
                    "error": {
                        "message": "unknown field(s): " + ", ".join(unknown),
                        "type": "invalid_request",
                        "code": "INVALID_REQUEST",
                    }
                },
            )

        raw_denied = body.get("denied", False)
        if not isinstance(raw_denied, bool):
            return ClientToolSubmitResult(
                400,
                {
                    "error": {
                        "message": "denied must be a boolean",
                        "type": "invalid_request",
                        "code": "INVALID_REQUEST",
                    }
                },
            )
        raw_error = body.get("error", "")
        if raw_error is None:
            raw_error = ""
        if not isinstance(raw_error, str):
            return ClientToolSubmitResult(
                400,
                {
                    "error": {
                        "message": "error must be a string",
                        "type": "invalid_request",
                        "code": "INVALID_REQUEST",
                    }
                },
            )
        raw_output = body.get("output", "")
        if raw_output is None:
            raw_output = ""
        if not isinstance(raw_output, str):
            return ClientToolSubmitResult(
                400,
                {
                    "error": {
                        "message": "output must be a string",
                        "type": "invalid_request",
                        "code": "INVALID_REQUEST",
                    }
                },
            )
        if len(raw_output) > MAX_CLIENT_OUTPUT_CHARS:
            return ClientToolSubmitResult(
                413,
                {
                    "error": {
                        "message": f"client tool output exceeds {MAX_CLIENT_OUTPUT_CHARS} characters",
                        "type": "invalid_request",
                        "code": "PAYLOAD_TOO_LARGE",
                    }
                },
            )
        if len(raw_error) > MAX_CLIENT_ERROR_CHARS:
            return ClientToolSubmitResult(
                413,
                {
                    "error": {
                        "message": f"client tool error exceeds {MAX_CLIENT_ERROR_CHARS} characters",
                        "type": "invalid_request",
                        "code": "PAYLOAD_TOO_LARGE",
                    }
                },
            )
        raw_ok = body.get("ok")
        if raw_ok is not None and not isinstance(raw_ok, bool):
            return ClientToolSubmitResult(
                400,
                {
                    "error": {
                        "message": "ok must be a boolean",
                        "type": "invalid_request",
                        "code": "INVALID_REQUEST",
                    }
                },
            )
        ok_val = (not raw_denied and not raw_error.strip()) if raw_ok is None else raw_ok
        if raw_denied:
            ok_val = False
        body_session = body.get("session")
        if body_session is not None and not isinstance(body_session, str):
            return ClientToolSubmitResult(
                400,
                {
                    "error": {
                        "message": "session must be a string",
                        "type": "invalid_request",
                        "code": "INVALID_REQUEST",
                    }
                },
            )
        header_session = str(session or "").strip()
        clean_body_session = body_session.strip() if isinstance(body_session, str) else ""
        if header_session and clean_body_session and header_session != clean_body_session:
            return ClientToolSubmitResult(
                400,
                {
                    "error": {
                        "message": "body session conflicts with X-Sonder-Session header",
                        "type": "invalid_request",
                        "code": "INVALID_REQUEST",
                    }
                },
            )
        effective_session = clean_body_session or header_session
        caller_principal = str(principal or "").strip()
        digest = _canonical_result_digest(ok_val, raw_output, raw_error, raw_denied)
        now = self._clock()

        with self._lock:
            self._prune_locked(now)
            call = self._calls.get(cid)
            if call is None:
                return ClientToolSubmitResult(
                    404,
                    {
                        "error": {
                            "message": f"no client tool call {cid} found",
                            "type": "not_found",
                            "code": "CLIENT_TOOL_NOT_FOUND",
                        }
                    },
                )
            if call.principal != caller_principal:
                return ClientToolSubmitResult(
                    403,
                    {
                        "error": {
                            "message": "you do not own this client tool call",
                            "type": "forbidden",
                            "code": "FORBIDDEN",
                        }
                    },
                )
            if call.session and effective_session != call.session:
                return ClientToolSubmitResult(
                    403,
                    {
                        "error": {
                            "message": "session does not match this client tool call",
                            "type": "forbidden",
                            "code": "FORBIDDEN",
                        }
                    },
                )
            if call.result is not None:
                if call.result.digest == digest:
                    return ClientToolSubmitResult(
                        200,
                        {
                            "object": "client_tool_result",
                            "call_id": call.call_id,
                            "tool": call.tool,
                            "status": call.status,
                            "accepted": True,
                            "idempotent": True,
                        },
                    )
                return ClientToolSubmitResult(
                    409,
                    {
                        "error": {
                            "message": f"client tool call {call.call_id} already received a different result",
                            "type": "conflict",
                            "code": "CLIENT_TOOL_ALREADY_SUBMITTED",
                        }
                    },
                )
            if call.status == "expired" or now > call.expires_at:
                call.status = "expired"
                call.event.set()
                call.args = {}
                return ClientToolSubmitResult(
                    410,
                    {
                        "error": {
                            "message": f"client tool call {call.call_id} has expired",
                            "type": "invalid_request",
                            "code": "CLIENT_TOOL_EXPIRED",
                        }
                    },
                )
            call.result = ClientToolPayload(
                ok=ok_val,
                output=raw_output,
                error=raw_error,
                denied=raw_denied,
                digest=digest,
            )
            call.status = "denied" if raw_denied else ("finished" if ok_val else "error")
            if call.mode != "stream":
                _shrink_completed_call(call, clear_args=True)
            call.event.set()
            return ClientToolSubmitResult(
                200,
                {
                    "object": "client_tool_result",
                    "call_id": call.call_id,
                    "tool": call.tool,
                    "status": call.status,
                    "accepted": True,
                    "idempotent": False,
                },
            )


def handle_client_tool_result_post(
    handler: Any,
    path: str,
    req: Any,
    context: Mapping[str, Any],
    *,
    broker: ClientToolBroker,
    principal: str,
) -> None:
    """Serve ``POST /v1/client-tools/<call_id>/result``."""
    if not context.get("authorized"):
        handler._send_auth_error()
        return
    parts = [p for p in path[len("/v1/client-tools/"):].strip("/").split("/") if p]
    if len(parts) != 2 or parts[1] != "result":
        handler._send_not_found()
        return
    session_id = str(handler.headers.get("X-Sonder-Session", "") or "").strip()
    result = broker.submit_result(parts[0], req, principal=principal, session=session_id)
    handler._send_json_payload(result.body, status=result.status)


def merge_client_tool_receipt(
    receipt: dict[str, Any],
    *,
    tool_turn: Any,
    slash_records: list[dict[str, Any]],
    slash_handoffs: list[dict[str, Any]],
) -> None:
    """Attach client tool records and non-stream handoff metadata to ``sonder_receipt``."""
    if tool_turn is not None and getattr(tool_turn, "records", None):
        receipt["tools"] = [record.to_receipt() for record in tool_turn.records]
    if slash_records:
        existing = receipt.setdefault("tools", [])
        if isinstance(existing, list):
            existing.extend(slash_records)
    handoffs = list(slash_handoffs)
    if tool_turn is not None and getattr(tool_turn, "client_handoffs", None):
        handoffs.extend(tool_turn.client_handoffs)
    if handoffs:
        receipt["client_tool_handoff"] = handoffs[0] if len(handoffs) == 1 else handoffs


__all__ = [
    "ClientToolArgsTooLargeError",
    "ClientToolBroker",
    "ClientToolBrokerCapacityError",
    "ClientToolCall",
    "ClientToolPayload",
    "ClientToolSubmitResult",
    "ClientToolWaitOutcome",
    "DEFAULT_CLIENT_TOOL_TIMEOUT_SECONDS",
    "DEFAULT_NON_STREAM_HANDOFF_TTL_SECONDS",
    "MAX_CLIENT_ARGS_BYTES",
    "MAX_CLIENT_ERROR_CHARS",
    "MAX_CLIENT_OUTPUT_CHARS",
    "MAX_CLIENT_TOOL_TIMEOUT_SECONDS",
    "MAX_PENDING_CALLS_GLOBAL",
    "MAX_PENDING_CALLS_PER_PRINCIPAL",
    "MAX_TOTAL_CALLS",
    "handle_client_tool_result_post",
    "is_valid_client_call_id",
    "merge_client_tool_receipt",
]
