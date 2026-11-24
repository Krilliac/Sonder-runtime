"""HTTP handler, authentication, host-policy integration, and configuration helpers."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from http.server import BaseHTTPRequestHandler
from typing import Any, Protocol, cast

from sonder_runtime.adapters.launcher.process_inspection import is_loopback
from sonder_runtime.adapters.launcher.state_store import (
    OPERATION_ID_PATTERN,
    TERMINAL_PHASES,
    LauncherConflictError,
)
from sonder_runtime.adapters.launcher_idempotency import (
    normalize_idempotency_key as _raw_normalize_idempotency_key,
    valid_command_replay as _raw_valid_command_replay,
)
from sonder_runtime.application.lifecycle import (
    normalize_context_size as _raw_normalize_context_size,
)


normalize_context_size = cast(Callable[[Any], str], _raw_normalize_context_size)
normalize_idempotency_key = cast(Callable[[Any], str], _raw_normalize_idempotency_key)
valid_command_replay = cast(Callable[[Any], bool], _raw_valid_command_replay)


DEFAULT_MAX_BODY = 16_384
DEFAULT_BODY_READ_TIMEOUT_SECONDS = 5.0


class LauncherControllerProtocol(Protocol):
    """Minimal controller surface required by the launcher HTTP handler."""

    def status(self) -> dict[str, Any]: ...
    def operation(self, operation_id: str) -> dict[str, Any] | None: ...
    def operation_payload(self, operation: dict[str, Any]) -> dict[str, Any]: ...
    def submit(
        self,
        action: str,
        context_size: str = "8192",
        idempotency_key: str = "",
    ) -> tuple[dict[str, Any], bool]: ...


class CommandJournalProtocol(Protocol):
    """Minimal command-recovery journal surface used by HTTP command dispatch."""

    def acknowledge(self, client_id: str, command_id: str) -> dict[str, Any]: ...
    def receive(
        self,
        client_id: str,
        command_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]: ...
    def complete(
        self,
        client_id: str,
        command_id: str,
        result: dict[str, Any],
    ) -> Any: ...
    def mark_uncertain(self, client_id: str, command_id: str) -> Any: ...


class LauncherServerProtocol(Protocol):
    """Attributes exposed on ``LauncherServer`` to request handlers."""

    controller: LauncherControllerProtocol
    token: str
    allowed_hosts: tuple[str, ...]
    local_names: frozenset[str]
    command_journal: CommandJournalProtocol | None


HostDecisionFn = Callable[..., str | None]


def generate_token() -> str:
    """Generate a high-entropy URL-safe launcher bearer token."""
    return secrets.token_urlsafe(32)


def validate_configuration(
    host: str,
    token: str,
    cert: str = "",
    key: str = "",
    allow_insecure_http_for_development: bool = False,
) -> None:
    """Enforce authentication and TLS requirements for non-loopback bindings."""
    loopback = is_loopback(host)
    if not loopback and len(token) < 24:
        raise ValueError(
            "LAN launcher binding requires SONDER_LAUNCHER_TOKEN with at least 24 characters"
        )
    if bool(cert) != bool(key):
        raise ValueError("both TLS certificate and key are required")
    if (
        not loopback
        and not cert
        and not allow_insecure_http_for_development
    ):
        raise ValueError(
            "non-loopback launcher binding requires TLS; use --cert and --key "
            "or the explicit development-only insecure HTTP override"
        )


def unauthenticated_loopback_warning(host: str, token: str) -> str:
    """Return the startup warning for a token-less loopback launcher, or ``""``.

    A loopback launcher without a token stays allowed (the operator decision
    for single-user desktops), but every local process and user on the machine
    can then start, stop and restart Sonder through it. That must be visible
    at startup, not only in the docs.
    """
    if token or not is_loopback(host):
        return ""
    return (
        "WARNING: the Sonder launcher is running WITHOUT authentication "
        "(no SONDER_LAUNCHER_TOKEN). Any local process or user on this machine "
        "can control Sonder through %s. Set SONDER_LAUNCHER_TOKEN (see "
        "--generate-token) unless this is a single-user machine." % host
    )


class LauncherHTTPHandler(BaseHTTPRequestHandler):
    """Strict HTTP routing, host validation, auth, and command-journal handling."""

    server_version = "SonderLauncher/1"
    timeout = 10.0

    # Subclasses (e.g. runtime.sonder_launcher.LauncherHandler) inject the
    # host-policy helper and command-journal exception types without crossing
    # adapter -> interfaces/legacy layer boundaries.
    host_decision_fn: HostDecisionFn | None = None
    host_not_allowed_remedy: str = ""
    command_conflict_type: type[Exception] = RuntimeError
    command_journal_error_type: type[Exception] = RuntimeError
    conflict_error_type: type[LauncherConflictError] = LauncherConflictError
    max_body_bytes: int = DEFAULT_MAX_BODY
    body_read_timeout_seconds: float = DEFAULT_BODY_READ_TIMEOUT_SECONDS
    terminal_phases: Sequence[str] = TERMINAL_PHASES

    def _launcher_server(self) -> LauncherServerProtocol:
        return self.server  # type: ignore[return-value]

    def _max_body(self) -> int:
        return int(self.max_body_bytes)

    def _body_read_timeout(self) -> float:
        return float(self.body_read_timeout_seconds)

    def _host_decision(
        self,
        host_header: str | None,
        *,
        allowed_hosts: Sequence[str],
        local_names: Sequence[str] | frozenset[str],
        credentials_required: bool,
    ) -> str | None:
        decision_fn = type(self).host_decision_fn
        if decision_fn is None:
            return host_header or ""
        return decision_fn(
            host_header,
            allowed_hosts=allowed_hosts,
            local_names=local_names,
            credentials_required=credentials_required,
        )

    def log_message(self, format: str, *args: Any) -> None:
        if os.environ.get("SONDER_LAUNCHER_QUIET") != "1":
            super().log_message(format, *args)

    def _authorized(self) -> bool:
        expected = self._launcher_server().token
        if not expected:
            return is_loopback(self.client_address[0])
        supplied = self.headers.get("Authorization", "")
        if supplied.lower().startswith("bearer "):
            supplied = supplied[7:].strip()
        else:
            supplied = self.headers.get("X-Sonder-Launcher-Token", "").strip()
        return bool(supplied) and hmac.compare_digest(supplied, expected)

    def _send(
        self,
        payload: Mapping[str, Any],
        status: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _host_refused(self) -> bool:
        """Answer 421 HOST_NOT_ALLOWED for a name a rebinding page could use."""
        server = self._launcher_server()
        values = self.headers.get_all("Host") or []
        if len(values) > 1:
            decision: str | None = None
        else:
            decision = self._host_decision(
                values[0] if values else None,
                allowed_hosts=getattr(server, "allowed_hosts", ()),
                local_names=getattr(server, "local_names", ()),
                credentials_required=bool(server.token),
            )
        if decision is not None:
            return False
        self.close_connection = True
        self._send(
            {
                "ok": False,
                "error": "host is not allowed for this launcher",
                "code": "HOST_NOT_ALLOWED",
                "remedy": self.host_not_allowed_remedy.replace(
                    "[server].allowed_hosts / SONDER_ALLOWED_HOSTS",
                    "SONDER_LAUNCHER_ALLOWED_HOSTS",
                ),
            },
            421,
            headers={"Connection": "close"},
        )
        return True

    def _auth(self) -> bool:
        if self._authorized():
            return True
        self._send({"ok": False, "error": "launcher authentication required"}, 401)
        return False

    def _command_identity(self, *, required: bool = False) -> tuple[str, str]:
        client_id = self.headers.get("X-Sonder-Client-Id", "").strip()
        command_id = self.headers.get("X-Sonder-Command-Id", "").strip()
        if bool(client_id) != bool(command_id):
            raise ValueError(
                "X-Sonder-Client-Id and X-Sonder-Command-Id must be sent together"
            )
        if required and not client_id:
            raise ValueError(
                "X-Sonder-Client-Id and X-Sonder-Command-Id are required"
            )
        return client_id, command_id

    @staticmethod
    def _command_idempotency_key(client_id: str, command_id: str) -> str:
        digest = hashlib.sha256(
            (client_id + "\0" + command_id).encode("utf-8")
        ).hexdigest()
        return "command:" + digest

    def do_GET(self) -> None:
        if self._host_refused():
            return
        path = self.path.split("?", 1)[0].rstrip("/")
        is_status = path in {"", "/v1/launcher/status"}
        operation_id = ""
        operation_prefix = "/v1/launcher/operations/"
        if path.startswith(operation_prefix):
            suffix = path[len(operation_prefix):]
            if "/" not in suffix:
                operation_id = suffix.lower()
        if not is_status and not OPERATION_ID_PATTERN.fullmatch(operation_id):
            self._send({"ok": False, "error": "not found"}, 404)
            return
        if not self._auth():
            return
        server = self._launcher_server()
        if is_status:
            self._send(server.controller.status())
            return
        operation = server.controller.operation(operation_id)
        if not operation:
            self._send({"ok": False, "error": "operation not found"}, 404)
            return
        self._send(server.controller.operation_payload(operation))

    def _read_post_json(self) -> dict[str, Any] | None:
        if self.headers.get("Transfer-Encoding"):
            self._send({"ok": False, "error": "transfer encoding is not supported"}, 400)
            return None
        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            self._send({"ok": False, "error": "Content-Type must be application/json"}, 415)
            return None
        raw_length = self.headers.get("Content-Length")
        if raw_length is None or not raw_length.isdigit():
            self._send({"ok": False, "error": "valid Content-Length is required"}, 411)
            return None
        try:
            length = int(raw_length)
        except ValueError:
            self._send({"ok": False, "error": "invalid content length"}, 400)
            return None
        if length < 0 or length > self._max_body():
            self._send({"ok": False, "error": "request too large"}, 413)
            return None
        previous_timeout = self.connection.gettimeout()
        try:
            self.connection.settimeout(self._body_read_timeout())
            raw_body = self.rfile.read(length)
        except (OSError, TimeoutError):
            self._send({"ok": False, "error": "request body is incomplete"}, 400)
            return None
        finally:
            try:
                self.connection.settimeout(previous_timeout)
            except OSError:
                pass
        if len(raw_body) != length:
            self._send({"ok": False, "error": "request body is incomplete"}, 400)
            return None
        try:
            body = json.loads(raw_body or b"{}")
        except (ValueError, UnicodeDecodeError):
            self._send({"ok": False, "error": "invalid JSON"}, 400)
            return None
        if not isinstance(body, dict):
            self._send({"ok": False, "error": "JSON body must be an object"}, 400)
            return None
        return body

    def _mark_journal_uncertain(
        self,
        journal: CommandJournalProtocol,
        client_id: str,
        command_id: str,
    ) -> None:
        journal_errors = (ValueError, OSError, self.command_journal_error_type)
        try:
            journal.mark_uncertain(client_id, command_id)
        except journal_errors:
            pass

    def do_POST(self) -> None:
        if self._host_refused():
            return
        path = self.path.split("?", 1)[0].rstrip("/")
        action = path.rsplit("/", 1)[-1]
        is_ack = path == "/v1/launcher/commands/ack"
        if not is_ack and path not in {
            "/v1/launcher/start",
            "/v1/launcher/stop",
            "/v1/launcher/restart",
        }:
            self._send({"ok": False, "error": "not found"}, 404)
            return
        if not self._auth():
            return
        body = self._read_post_json()
        if body is None:
            return
        unexpected = sorted(set(body) - (set() if is_ack else {"context_size"}))
        if unexpected:
            self._send(
                {
                    "ok": False,
                    "error": "unsupported request field(s): %s"
                    % ", ".join(unexpected),
                },
                400,
            )
            return
        try:
            client_id, command_id = self._command_identity(required=is_ack)
            context_size = normalize_context_size(body.get("context_size") or "8192")
            idempotency_key = normalize_idempotency_key(
                self.headers.get("Idempotency-Key", "")
            )
            if client_id and idempotency_key:
                raise ValueError(
                    "tracked commands must not also send Idempotency-Key"
                )
        except ValueError as exc:
            self._send({"ok": False, "error": str(exc)}, 400)
            return

        server = self._launcher_server()
        journal = server.command_journal
        journal_errors = (ValueError, OSError, self.command_journal_error_type)
        if is_ack:
            if journal is None:
                self._send(
                    {"ok": False, "error": "command recovery is unavailable"},
                    503,
                )
                return
            try:
                acknowledged = journal.acknowledge(client_id, command_id)
            except journal_errors as exc:
                self._send({"ok": False, "error": str(exc)}, 409)
                return
            self._send({"ok": True, **acknowledged})
            return

        command_tracked = bool(client_id)
        if command_tracked:
            if journal is None:
                self._send(
                    {"ok": False, "error": "command recovery is unavailable"},
                    503,
                )
                return
            try:
                receipt = journal.receive(
                    client_id,
                    command_id,
                    {"action": action, "context_size": context_size},
                )
            except self.command_conflict_type as exc:
                self._send({"ok": False, "error": str(exc)}, 409)
                return
            except journal_errors as exc:
                self._send(
                    {"ok": False, "error": "command was not dispatched: %s" % exc},
                    503,
                )
                return
            if receipt["state"] == "completed":
                replay = receipt["result"]
                if not valid_command_replay(replay):
                    self._send(
                        {
                            "ok": False,
                            "command_state": "completed",
                            "dispatch_allowed": False,
                            "error": "stored command result is invalid",
                        },
                        503,
                    )
                    return
                self._send(
                    replay["payload"], replay["status"], replay.get("headers")
                )
                return
            if not receipt["dispatch"]:
                uncertain = receipt["state"] == "uncertain"
                acknowledged_state = receipt["state"] == "acknowledged"
                self._send(
                    {
                        "ok": False,
                        "command_state": receipt["state"],
                        "dispatch_allowed": False,
                        "error": (
                            "command outcome is uncertain after recovery; inspect the "
                            "launcher operation ledger before issuing a new command"
                            if uncertain
                            else (
                                "command result was already acknowledged"
                                if acknowledged_state
                                else "command is already pending"
                            )
                        ),
                    },
                    409 if uncertain or acknowledged_state else 202,
                    {"Retry-After": "1"},
                )
                return
            if not idempotency_key:
                idempotency_key = self._command_idempotency_key(
                    client_id, command_id
                )

        try:
            operation, created = server.controller.submit(
                action, context_size, idempotency_key
            )
        except self.conflict_error_type as exc:
            payload = server.controller.status()
            payload.update({"ok": False, "error": str(exc)})
            if exc.operation:
                payload.update({
                    "operation_id": exc.operation["id"],
                    "operation_phase": exc.operation["phase"],
                    "operation": exc.operation,
                })
            if command_tracked and journal is not None:
                try:
                    journal.complete(
                        client_id,
                        command_id,
                        {"status": 409, "payload": payload, "headers": {}},
                    )
                except journal_errors:
                    self._mark_journal_uncertain(journal, client_id, command_id)
                    self._send(
                        {
                            "ok": False,
                            "command_state": "uncertain",
                            "dispatch_allowed": False,
                            "error": (
                                "launcher conflict was not dispatched, but its "
                                "replay result could not be made durable"
                            ),
                        },
                        503,
                    )
                    return
            self._send(payload, 409)
            return
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            if command_tracked and journal is not None:
                self._mark_journal_uncertain(journal, client_id, command_id)
            self._send(
                {"ok": False, "error": "launcher operation could not be queued: %s" % exc},
                503,
            )
            return

        try:
            payload = server.controller.operation_payload(operation)
            payload.update({
                "accepted": created,
                "idempotent_replay": not created,
            })
        except (OSError, RuntimeError, sqlite3.Error):
            if command_tracked and journal is not None:
                self._mark_journal_uncertain(journal, client_id, command_id)
            self._send(
                {
                    "ok": False,
                    "operation_id": operation.get("id", ""),
                    "command_state": "uncertain" if command_tracked else "untracked",
                    "dispatch_allowed": False,
                    "error": (
                        "launcher operation was queued but its response could not "
                        "be constructed"
                    ),
                },
                503,
            )
            return

        terminal = operation["phase"] in self.terminal_phases
        status_code = 200 if terminal else 202
        response_headers = {
            "Location": "/v1/launcher/operations/%s" % operation["id"]
        }
        if command_tracked and journal is not None:
            try:
                journal.complete(
                    client_id,
                    command_id,
                    {
                        "status": status_code,
                        "payload": payload,
                        "headers": response_headers,
                    },
                )
            except journal_errors as exc:
                self._mark_journal_uncertain(journal, client_id, command_id)
                self._send(
                    {
                        "ok": False,
                        "operation_id": operation["id"],
                        "operation_phase": operation["phase"],
                        "command_state": "uncertain",
                        "dispatch_allowed": False,
                        "error": (
                            "launcher operation was queued but its replay result could "
                            "not be made durable: %s" % exc
                        ),
                    },
                    503,
                    response_headers,
                )
                return
        self._send(payload, status_code, response_headers)
