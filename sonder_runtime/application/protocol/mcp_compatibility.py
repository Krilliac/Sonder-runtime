"""Provider-neutral MCP compatibility contracts.

This module deliberately contains no MCP SDK, provider, socket, DNS, or HTTP
dependency.  It models the application decision made after an MCP client has
advertised its capabilities; an interface adapter owns serialising that
decision onto the negotiated MCP transport.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping


class McpNegotiationError(ValueError):
    """Raised when a client advertises an unsupported or invalid contract."""


@dataclass(frozen=True)
class LegacyMcpContract:
    """A deliberately narrow legacy contract still supported at the boundary."""

    name: str
    version: str
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name or not self.version:
            raise ValueError("legacy MCP contract requires name and version")
        if any(not isinstance(item, str) or not item for item in self.capabilities):
            raise ValueError("legacy MCP capabilities must be non-empty strings")
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("legacy MCP capabilities must be unique")


@dataclass(frozen=True)
class McpNegotiation:
    """Immutable result of local MCP capability negotiation."""

    server_version: str
    client_version: str
    agreed_version: str
    capabilities: tuple[str, ...] = ()
    legacy_contract: LegacyMcpContract | None = None


class McpCompatibility:
    """Negotiates supported MCP versions without performing provider I/O."""

    def __init__(
        self,
        *,
        server_version: str = "2.0",
        supported_versions: tuple[str, ...] = ("2.0",),
        legacy_contracts: tuple[LegacyMcpContract, ...] = (),
        capabilities: tuple[str, ...] = (),
    ) -> None:
        if not server_version or not supported_versions:
            raise ValueError("MCP compatibility requires a server and supported version")
        if server_version not in supported_versions:
            raise ValueError("server version must be supported")
        if len(set(supported_versions)) != len(supported_versions):
            raise ValueError("supported MCP versions must be unique")
        if len(set(capabilities)) != len(capabilities):
            raise ValueError("MCP capabilities must be unique")
        self._server_version = server_version
        self._supported_versions = tuple(supported_versions)
        self._legacy_contracts = tuple(legacy_contracts)
        self._capabilities = tuple(capabilities)

    @property
    def supported_versions(self) -> tuple[str, ...]:
        return self._supported_versions

    @property
    def legacy_contracts(self) -> tuple[LegacyMcpContract, ...]:
        return self._legacy_contracts

    def negotiate(
        self,
        client_versions: tuple[str, ...],
        *,
        client_capabilities: tuple[str, ...] = (),
        legacy_contract: str | None = None,
    ) -> McpNegotiation:
        """Choose the first server-preferred common version.

        A legacy declaration must name an explicitly configured contract; it
        never silently downgrades a client to an unknown protocol.
        """
        if not client_versions:
            raise McpNegotiationError("client advertised no MCP versions")
        agreed = next((v for v in self._supported_versions if v in client_versions), None)
        selected_legacy = None
        if agreed is None and legacy_contract is not None:
            selected_legacy = next(
                (c for c in self._legacy_contracts if c.name == legacy_contract), None
            )
            if selected_legacy is not None:
                agreed = selected_legacy.version
        if agreed is None:
            raise McpNegotiationError("no supported MCP protocol version")
        capabilities = tuple(
            capability for capability in self._capabilities if capability in client_capabilities
        )
        return McpNegotiation(
            server_version=self._server_version,
            client_version=client_versions[0],
            agreed_version=agreed,
            capabilities=capabilities,
            legacy_contract=selected_legacy,
        )


NotificationCallback = Callable[[str, Mapping[str, Any]], None]


class SubscriptionNotificationRouter:
    """Routes negotiated notifications to local subscribers only."""

    def __init__(self) -> None:
        self._subscriptions: dict[str, dict[str, NotificationCallback]] = {}

    def subscribe(self, connection_id: str, event: str, callback: NotificationCallback) -> None:
        if not connection_id or not event or not callable(callback):
            raise ValueError("connection, event, and callback are required")
        self._subscriptions.setdefault(connection_id, {})[event] = callback

    def unsubscribe(self, connection_id: str, event: str | None = None) -> None:
        if event is None:
            self._subscriptions.pop(connection_id, None)
        else:
            events = self._subscriptions.get(connection_id)
            if events is not None:
                events.pop(event, None)
                if not events:
                    self._subscriptions.pop(connection_id, None)

    def publish(self, event: str, payload: Mapping[str, Any]) -> int:
        """Deliver one notification and return the number of local recipients."""
        delivered = 0
        for events in tuple(self._subscriptions.values()):
            callback = events.get(event)
            if callback is not None:
                callback(event, payload)
                delivered += 1
        return delivered

    def subscriber_count(self, event: str | None = None) -> int:
        return sum(1 for events in self._subscriptions.values() if event is None or event in events)

