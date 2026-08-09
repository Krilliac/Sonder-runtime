"""Host-owned, fail-closed foundation for calling explicitly allowed MCP tools.

This module deliberately does not expose an MCP server surface or provide a
general-purpose HTTP client.  A host adapter must supply a reconnect-per-call
transport which connects only to the addresses validated in ``McpCallRequest``.
That keeps DNS pinning, TLS, redirects, and connection lifecycle at an adapter
boundary that can be audited before remote MCP is enabled.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Protocol

from sonder_runtime.application.context import OperationContext
from sonder_runtime.application.ports.event_sink import EventSink


_KNOWN_CAPABILITIES = frozenset({"read", "filesystem", "network", "mutate", "process"})
_SECRET_CONFIG_KEYS = frozenset({
    "api_key", "authorization", "credential", "headers", "password", "secret", "token"
})
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_MAX_TIMEOUT_SECONDS = 60.0
_MAX_RESULT_BYTES = 1_048_576
_MAX_ARGUMENT_BYTES = 65_536
_NUMERIC_HOST_PART = re.compile(r"^(?:0[xX][0-9A-Fa-f]+|0[0-7]*|[0-9]+)$")


class ExternalMcpError(RuntimeError):
    """A safe-to-report external MCP failure with no upstream exception text."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ExternalMcpToolPolicy:
    name: str
    read_only: bool = True
    capabilities: tuple[str, ...] = ("read",)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _SAFE_NAME.fullmatch(self.name):
            raise ValueError("external MCP tool names must be non-empty and canonical")
        if type(self.read_only) is not bool:
            raise ValueError("read_only must be a Boolean")
        capabilities = frozenset(self.capabilities)
        if len(capabilities) != len(self.capabilities):
            raise ValueError("external MCP tool capabilities must be unique")
        if not capabilities or not capabilities <= _KNOWN_CAPABILITIES:
            raise ValueError("external MCP tool capabilities are empty or unknown")
        if self.read_only and "mutate" in capabilities:
            raise ValueError("read-only external MCP tools cannot request mutate")
        if not self.read_only and "mutate" not in capabilities:
            raise ValueError("writable external MCP tools must explicitly request mutate")


@dataclass(frozen=True)
class ExternalMcpServerPolicy:
    name: str
    endpoint: str
    tools: tuple[ExternalMcpToolPolicy, ...]
    credential_env: str | None = None
    allow_remote: bool = False
    timeout_seconds: float = 15.0
    max_result_bytes: int = 131_072
    transport: str = "streamable_http"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _SAFE_NAME.fullmatch(self.name):
            raise ValueError("external MCP server names must be non-empty and canonical")
        if self.transport != "streamable_http":
            raise ValueError("only the non-executable streamable_http transport is supported")
        if type(self.allow_remote) is not bool:
            raise ValueError("allow_remote must be a Boolean")
        if not self.tools:
            raise ValueError("external MCP servers require an explicit tool allowlist")
        names = [tool.name for tool in self.tools]
        if len(set(names)) != len(names):
            raise ValueError("external MCP tool names must be unique per server")
        if self.credential_env is not None and not _ENV_NAME.fullmatch(self.credential_env):
            raise ValueError("credential_env must be a canonical environment variable name")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 0 < self.timeout_seconds <= _MAX_TIMEOUT_SECONDS
        ):
            raise ValueError("external MCP timeout is outside the supported bound")
        if (
            isinstance(self.max_result_bytes, bool)
            or not isinstance(self.max_result_bytes, int)
            or not 0 < self.max_result_bytes <= _MAX_RESULT_BYTES
        ):
            raise ValueError("external MCP result limit is outside the supported bound")
        _parse_endpoint(self.endpoint)


@dataclass(frozen=True)
class McpCallRequest:
    """Validated transport request. ``credential`` must never be logged."""

    endpoint: str
    resolved_addresses: tuple[str, ...]
    server_name: str
    tool_name: str
    arguments: Mapping[str, Any]
    timeout_seconds: float
    max_result_bytes: int
    follow_redirects: bool = False
    credential: str | None = field(default=None, repr=False, compare=False)


class ExternalMcpTransport(Protocol):
    """One-shot transport: open, initialize, call, and close for every invoke.

    Implementations must dial only ``resolved_addresses``, preserve the URL
    hostname for TLS verification, reject redirects, enforce the byte limit
    while reading (the service checks it again), and discard the session after
    returning.  These rules are intentionally explicit because a generic SDK
    HTTP client cannot by itself prevent DNS rebinding or redirect SSRF.
    """

    def invoke(self, request: McpCallRequest) -> Awaitable[Any]: ...


@dataclass(frozen=True)
class ExternalMcpReceipt:
    receipt_id: str
    server: str
    tool: str
    capabilities: tuple[str, ...]
    ok: bool
    elapsed_ms: int
    result_bytes: int
    structured: bool
    error_code: str | None = None


@dataclass(frozen=True)
class ExternalMcpCallResult:
    value: Any
    receipt: ExternalMcpReceipt


SecretResolver = Callable[[str], str | None]


class ExternalMcpBridge:
    """Calls only configured tools through a host-injected one-shot transport."""

    def __init__(
        self,
        servers: tuple[ExternalMcpServerPolicy, ...],
        *,
        transport: ExternalMcpTransport,
        events: EventSink,
        secret_resolver: SecretResolver,
        enabled_capabilities: frozenset[str] = frozenset({"read"}),
    ) -> None:
        if len({server.name for server in servers}) != len(servers):
            raise ValueError("external MCP server names must be unique")
        if not enabled_capabilities <= _KNOWN_CAPABILITIES:
            raise ValueError("enabled external MCP capabilities contain unknown values")
        self._servers = {server.name: server for server in servers}
        self._transport = transport
        self._events = events
        self._secret_resolver = secret_resolver
        self._enabled_capabilities = enabled_capabilities

    def safe_manifest(self) -> dict[str, Any]:
        """Return configured authority without endpoint or credential material."""
        return {
            "enabled_capabilities": sorted(self._enabled_capabilities),
            "servers": [
                {
                    "name": server.name,
                    "remote_allowed": server.allow_remote,
                    "credential_configured": server.credential_env is not None,
                    "tools": [
                        {
                            "name": tool.name,
                            "read_only": tool.read_only,
                            "capabilities": sorted(tool.capabilities),
                        }
                        for tool in server.tools
                    ],
                }
                for server in self._servers.values()
            ],
        }

    async def call(
        self,
        server_name: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        context: OperationContext,
    ) -> ExternalMcpCallResult:
        started = time.monotonic()
        receipt_id = uuid.uuid4().hex
        policy: ExternalMcpToolPolicy | None = None
        audit_server = "<denied>"
        result_bytes = 0
        structured = False
        try:
            if context.cancellation.cancelled or context.expired:
                raise ExternalMcpError("CONTEXT_EXPIRED", "operation is cancelled or expired")
            server = self._servers.get(server_name)
            if server is None:
                raise ExternalMcpError("SERVER_NOT_ALLOWED", "external MCP server is not allowed")
            audit_server = server.name
            policy = next((item for item in server.tools if item.name == tool_name), None)
            if policy is None:
                raise ExternalMcpError("TOOL_NOT_ALLOWED", "external MCP tool is not allowed")
            required = frozenset(policy.capabilities)
            if not required <= self._enabled_capabilities:
                raise ExternalMcpError(
                    "CAPABILITY_NOT_ENABLED", "external MCP tool capability is not enabled"
                )
            if not isinstance(arguments, Mapping):
                raise ExternalMcpError("INVALID_ARGUMENTS", "tool arguments must be an object")
            try:
                normalised_arguments = dict(arguments)
            except Exception as exc:
                raise ExternalMcpError(
                    "INVALID_ARGUMENTS", "tool arguments could not be normalized"
                ) from exc
            argument_bytes = _json_size(normalised_arguments, "INVALID_ARGUMENTS")
            if argument_bytes > _MAX_ARGUMENT_BYTES:
                raise ExternalMcpError("ARGUMENTS_TOO_LARGE", "tool arguments exceed the limit")

            deadline = started + server.timeout_seconds
            if context.deadline_monotonic is not None:
                deadline = min(deadline, context.deadline_monotonic)
            parsed, addresses, remote = await _await_stage(
                asyncio.to_thread(_resolve_endpoint, server.endpoint),
                context=context,
                deadline=deadline,
            )
            if remote and (not server.allow_remote or not context.cloud_allowed):
                raise ExternalMcpError(
                    "REMOTE_NOT_CONSENTED", "remote MCP requires server policy and cloud consent"
                )
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise ExternalMcpError("CONTEXT_EXPIRED", "operation deadline has expired")

            credential = None
            if server.credential_env is not None:
                try:
                    credential = await _await_stage(
                        asyncio.to_thread(self._secret_resolver, server.credential_env),
                        context=context,
                        deadline=deadline,
                    )
                except ExternalMcpError:
                    raise
                except Exception as exc:
                    raise ExternalMcpError(
                        "CREDENTIAL_UNAVAILABLE", "external MCP credential is unavailable"
                    ) from exc
                if not isinstance(credential, str) or not credential:
                    raise ExternalMcpError(
                        "CREDENTIAL_UNAVAILABLE", "external MCP credential is unavailable"
                    )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ExternalMcpError("TIMEOUT", "external MCP call timed out")
            request = McpCallRequest(
                endpoint=urllib.parse.urlunsplit(parsed),
                resolved_addresses=addresses,
                server_name=server.name,
                tool_name=policy.name,
                arguments=normalised_arguments,
                timeout_seconds=remaining,
                max_result_bytes=server.max_result_bytes,
                credential=credential,
            )
            try:
                raw_result = await _await_stage(
                    self._transport.invoke(request),
                    context=context,
                    deadline=deadline,
                )
            except ExternalMcpError:
                raise
            except Exception as exc:
                # Upstream text may contain headers, URLs, arguments, or secrets.
                raise ExternalMcpError("TRANSPORT_ERROR", "external MCP transport failed") from exc

            try:
                value, structured, is_error = _normalise_result(raw_result)
            except Exception as exc:
                raise ExternalMcpError(
                    "INVALID_RESULT", "external MCP returned an invalid result"
                ) from exc
            result_bytes = _json_size(value, "INVALID_RESULT")
            if result_bytes > server.max_result_bytes:
                raise ExternalMcpError("RESULT_TOO_LARGE", "external MCP result exceeds the limit")
            if is_error:
                raise ExternalMcpError("UPSTREAM_TOOL_ERROR", "external MCP tool reported an error")
            receipt = self._receipt(
                receipt_id, audit_server, policy, True, started, result_bytes, structured, None,
                context,
            )
            return ExternalMcpCallResult(value=value, receipt=receipt)
        except ExternalMcpError as exc:
            self._receipt(
                receipt_id, audit_server, policy, False, started, result_bytes, structured,
                exc.code, context,
            )
            raise

    def _receipt(
        self,
        receipt_id: str,
        server: str,
        policy: ExternalMcpToolPolicy | None,
        ok: bool,
        started: float,
        result_bytes: int,
        structured: bool,
        error_code: str | None,
        context: OperationContext,
    ) -> ExternalMcpReceipt:
        receipt = ExternalMcpReceipt(
            receipt_id=receipt_id,
            server=server,
            tool=policy.name if policy else "<denied>",
            capabilities=tuple(sorted(policy.capabilities)) if policy else (),
            ok=ok,
            elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
            result_bytes=result_bytes,
            structured=structured,
            error_code=error_code,
        )
        try:
            self._events.emit(
                "EXTERNAL_MCP_CALL",
                summary="external MCP call completed" if ok else "external MCP call denied or failed",
                detail={
                    "receipt_id": receipt.receipt_id,
                    "server": receipt.server,
                    "tool": receipt.tool,
                    "capabilities": list(receipt.capabilities),
                    "ok": receipt.ok,
                    "elapsed_ms": receipt.elapsed_ms,
                    "result_bytes": receipt.result_bytes,
                    "structured": receipt.structured,
                    "error_code": receipt.error_code,
                },
                severity="INFO" if ok else "WARNING",
                correlation_id=context.correlation_id,
            )
        except Exception:
            # EventSink's contract is observational: a sink outage must not
            # turn a completed call into an apparent failure that callers may
            # retry.  The returned receipt remains the authoritative result.
            pass
        return receipt


def policies_from_mapping(raw: Mapping[str, Any]) -> tuple[ExternalMcpServerPolicy, ...]:
    """Parse host configuration while rejecting inline secret material and unknown keys."""
    if not isinstance(raw, Mapping):
        raise ValueError("external MCP config must be an object")
    _reject_secret_keys(raw)
    if set(raw) != {"servers"} or not isinstance(raw.get("servers"), list):
        raise ValueError("external MCP config must contain only a servers list")
    servers = []
    server_keys = {
        "name", "endpoint", "tools", "credential_env", "allow_remote",
        "timeout_seconds", "max_result_bytes", "transport",
    }
    tool_keys = {"name", "read_only", "capabilities"}
    for item in raw["servers"]:
        if not isinstance(item, Mapping) or not set(item) <= server_keys:
            raise ValueError("external MCP server config has unknown fields")
        tools_raw = item.get("tools")
        if not isinstance(tools_raw, list):
            raise ValueError("external MCP server tools must be a list")
        tools = []
        for tool in tools_raw:
            if not isinstance(tool, Mapping) or not set(tool) <= tool_keys:
                raise ValueError("external MCP tool config has unknown fields")
            values = dict(tool)
            if "capabilities" in values:
                if not isinstance(values["capabilities"], list):
                    raise ValueError("external MCP tool capabilities must be a list")
                values["capabilities"] = tuple(values["capabilities"])
            tools.append(ExternalMcpToolPolicy(**values))
        values = dict(item)
        values["tools"] = tuple(tools)
        servers.append(ExternalMcpServerPolicy(**values))
    return tuple(servers)


async def _await_stage(
    awaitable: Awaitable[Any],
    *,
    context: OperationContext,
    deadline: float,
) -> Any:
    """Await one bridge stage under the call's absolute deadline and cancellation.

    DNS and secret resolvers are synchronous host adapters, so callers wrap them
    in ``asyncio.to_thread`` before entering here. Cancelling that await cannot
    forcibly stop an operating-system resolver thread, but it does release the
    request and event loop immediately. Transports are required to be ordinary
    cancellation-cooperative async implementations.
    """
    task = asyncio.ensure_future(awaitable)

    async def watch_cancellation() -> None:
        while not context.cancellation.cancelled:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.01, remaining))

    cancellation = asyncio.create_task(watch_cancellation())
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        task.cancel()
        cancellation.cancel()
        await asyncio.gather(task, cancellation, return_exceptions=True)
        raise ExternalMcpError("TIMEOUT", "external MCP call timed out")
    done, _pending = await asyncio.wait(
        {task, cancellation}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED,
    )
    if task in done:
        cancellation.cancel()
        await asyncio.gather(cancellation, return_exceptions=True)
        if context.cancellation.cancelled:
            await asyncio.gather(task, return_exceptions=True)
            raise ExternalMcpError("CONTEXT_EXPIRED", "operation is cancelled or expired")
        if time.monotonic() >= deadline:
            await asyncio.gather(task, return_exceptions=True)
            raise ExternalMcpError("TIMEOUT", "external MCP call timed out")
        return await task

    task.cancel()
    cancellation.cancel()
    await asyncio.gather(task, cancellation, return_exceptions=True)
    if context.cancellation.cancelled:
        raise ExternalMcpError("CONTEXT_EXPIRED", "operation is cancelled or expired")
    raise ExternalMcpError("TIMEOUT", "external MCP call timed out")


def _reject_secret_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).strip().lower() in _SECRET_CONFIG_KEYS:
                raise ValueError("external MCP credentials must not appear in config")
            _reject_secret_keys(child)
    elif isinstance(value, list):
        for child in value:
            _reject_secret_keys(child)


def _parse_endpoint(endpoint: str) -> urllib.parse.SplitResult:
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("external MCP endpoint must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("external MCP endpoint userinfo is not allowed")
    if parsed.query or parsed.fragment:
        raise ValueError("external MCP endpoint query and fragment are not allowed")
    if "%" in parsed.hostname:
        raise ValueError("scoped or percent-encoded endpoint hosts are not allowed")
    host = parsed.hostname.rstrip(".")
    parts = host.split(".")
    if (
        ":" not in host
        and parts
        and all(_NUMERIC_HOST_PART.fullmatch(part or "") for part in parts)
    ):
        try:
            ipaddress.IPv4Address(host)
        except ValueError as exc:
            raise ValueError("non-canonical numeric endpoint hosts are not allowed") from exc
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("external MCP endpoint port is invalid") from exc
    return parsed


def _resolve_endpoint(
    endpoint: str,
) -> tuple[urllib.parse.SplitResult, tuple[str, ...], bool]:
    parsed = _parse_endpoint(endpoint)
    host = (parsed.hostname or "").rstrip(".").lower()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if host in {"localhost", "localhost.localdomain"}:
        addresses = _resolve_addresses(host, port)
    else:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            addresses = _resolve_addresses(host, port)
        else:
            addresses = (address.compressed,)
    ip_values = tuple(ipaddress.ip_address(address) for address in addresses)
    loopback = all(address.is_loopback for address in ip_values)
    public = all(_is_public(address) for address in ip_values)
    if not loopback and not public:
        raise ExternalMcpError(
            "ENDPOINT_BLOCKED", "external MCP endpoint resolves to a blocked network"
        )
    if loopback and parsed.scheme != "http" and parsed.scheme != "https":
        raise ExternalMcpError("ENDPOINT_BLOCKED", "loopback MCP transport is invalid")
    if public and parsed.scheme != "https":
        raise ExternalMcpError("ENDPOINT_BLOCKED", "remote MCP requires HTTPS")
    return parsed, addresses, public


def _resolve_addresses(host: str, port: int) -> tuple[str, ...]:
    try:
        rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise ExternalMcpError("ENDPOINT_UNRESOLVED", "external MCP endpoint did not resolve") from exc
    addresses = set()
    for row in rows:
        raw = str(row[4][0]).split("%", 1)[0]
        try:
            addresses.add(ipaddress.ip_address(raw).compressed)
        except ValueError as exc:
            raise ExternalMcpError("ENDPOINT_UNRESOLVED", "endpoint resolved incorrectly") from exc
    if not addresses:
        raise ExternalMcpError("ENDPOINT_UNRESOLVED", "external MCP endpoint did not resolve")
    return tuple(sorted(addresses))


def _is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return (
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_reserved
        and not address.is_unspecified
        and not address.is_multicast
    )


def _normalise_result(result: Any) -> tuple[Any, bool, bool]:
    structured = _field(result, "structuredContent", None)
    if structured is None:
        structured = _field(result, "structured_content", None)
    is_error = bool(_field(result, "isError", _field(result, "is_error", False)))
    if structured is not None:
        return structured, True, is_error
    content = _field(result, "content", ()) or ()
    text_parts = []
    for block in content:
        block_type = _field(block, "type", None)
        text = _field(block, "text", None)
        if block_type == "text" and isinstance(text, str):
            text_parts.append(text)
    return {"text": "\n".join(text_parts)}, False, is_error


def _field(value: Any, name: str, default: Any) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _json_size(value: Any, error_code: str) -> int:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except Exception as exc:
        raise ExternalMcpError(error_code, "external MCP data must be finite JSON") from exc
    return len(encoded)
