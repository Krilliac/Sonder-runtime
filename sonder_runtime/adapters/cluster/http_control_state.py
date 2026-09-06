"""Authenticated HTTPS transport for external control-state providers.

The client carries exact, bounded control-state events to a provider that owns
replication, quorum, and fencing.  It never elects an owner or turns a
capability declaration into proof.  A provider response is accepted only when
its identity, protocol, scope, and event sequence match the request.
"""
from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from ...domain.cluster_availability import (
    ControlStateEvent,
    FenceReceipt,
    OwnershipScope,
    ReplicatedControlStateCapabilities,
    ReplicationAcknowledgement,
    validate_replication_acknowledgement,
)
from ...domain.common.errors import DependencyUnavailable


_MIN_REQUEST_BYTES = 512
_MAX_REQUEST_BYTES = 8 * 1024 * 1024
_MIN_RESPONSE_BYTES = 512
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_READ_LIMIT = 128


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _default_opener(request: urllib.request.Request, *, timeout: float):
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


def _origin(value: object, *, allow_insecure_loopback: bool) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise ValueError("control-state provider origin must be a bounded URL")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "control-state provider origin must be an HTTP(S) origin without credentials"
        )
    try:
        hostname = parsed.hostname
        is_loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        is_loopback = hostname in {"localhost"}
    if parsed.scheme != "https" and not (allow_insecure_loopback and is_loopback):
        raise ValueError("remote control-state providers must use HTTPS")
    try:
        # Accessing .port rejects malformed ports before any request is made.
        port = parsed.port
    except ValueError as exc:
        raise ValueError("control-state provider origin has an invalid port") from exc
    netloc = parsed.netloc
    if port is None:
        # Keep the configured hostname spelling while making the default port
        # explicit in the normalized origin used by request signing/proxies.
        netloc = f"{netloc}:{443 if parsed.scheme == 'https' else 80}"
    return urllib.parse.urlunsplit((parsed.scheme, netloc, "", "", "")).rstrip("/")


def _api_key(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        raise ValueError("control-state provider API key must be 1..512 characters")
    if any(ord(char) < 0x21 or ord(char) > 0x7E for char in value):
        raise ValueError("control-state provider API key must contain printable ASCII")
    return value


def _bounded_read_cursor(value: object) -> int:
    if type(value) is not int or not 0 <= value <= (1 << 63) - 1:
        raise ValueError("after_sequence must be a bounded non-negative integer")
    return value


def _bounded_limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_READ_LIMIT:
        raise ValueError(f"limit must be within 1..{_MAX_READ_LIMIT}")
    return value


class HttpsControlStateProvider:
    """Call an explicitly configured external replication/fencing provider.

    ``capabilities`` is a declaration supplied by the provider integration;
    it is useful for admission and status, but it is never treated as runtime
    evidence.  The provider must still return an acknowledgement or fence
    receipt bound to the exact request.  Plain HTTP is accepted only for an
    explicitly enabled loopback test endpoint.
    """

    def __init__(
        self,
        *,
        origin: str,
        api_key: str,
        capabilities: ReplicatedControlStateCapabilities,
        timeout_seconds: float = 5.0,
        max_request_bytes: int = 2 * 1024 * 1024,
        max_response_bytes: int = 256 * 1024,
        allow_insecure_loopback: bool = False,
        opener: Callable[..., Any] = _default_opener,
    ) -> None:
        if not isinstance(capabilities, ReplicatedControlStateCapabilities):
            raise TypeError("control-state provider capabilities are required")
        if not isinstance(timeout_seconds, (int, float)) or isinstance(
            timeout_seconds, bool
        ) or not 0 < timeout_seconds <= 30:
            raise ValueError("control-state provider timeout must be within 0..30 seconds")
        if (
            type(max_request_bytes) is not int
            or not _MIN_REQUEST_BYTES <= max_request_bytes <= _MAX_REQUEST_BYTES
        ):
            raise ValueError(
                f"control-state request bound must be within {_MIN_REQUEST_BYTES}..{_MAX_REQUEST_BYTES} bytes"
            )
        if (
            type(max_response_bytes) is not int
            or not _MIN_RESPONSE_BYTES <= max_response_bytes <= _MAX_RESPONSE_BYTES
        ):
            raise ValueError(
                f"control-state response bound must be within {_MIN_RESPONSE_BYTES}..{_MAX_RESPONSE_BYTES} bytes"
            )
        if not callable(opener):
            raise TypeError("control-state provider opener must be callable")
        self.capabilities = capabilities
        self.provider_id = capabilities.provider_id
        self.protocol_version = capabilities.protocol_version
        self._origin = _origin(origin, allow_insecure_loopback=allow_insecure_loopback)
        self._api_key = _api_key(api_key)
        self._timeout = float(timeout_seconds)
        self._max_request_bytes = max_request_bytes
        self._max_response_bytes = max_response_bytes
        self._opener = opener

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, object] | None = None,
        query: Mapping[str, object] | None = None,
    ) -> object:
        if not path.startswith("/") or "?" in path or "#" in path:
            raise ValueError("control-state provider path must be a fixed origin-relative path")
        url = self._origin + path
        if query:
            url += "?" + urllib.parse.urlencode(
                [(str(key), str(value)) for key, value in query.items()]
            )
        encoded: bytes | None = None
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "X-Sonder-Control-State-Provider": self.provider_id,
        }
        if body is not None:
            encoded = json.dumps(
                body,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("utf-8")
            if len(encoded) > self._max_request_bytes:
                raise DependencyUnavailable("control-state provider request bound exceeded")
            headers.update(
                {
                    "Content-Type": "application/json",
                    "Content-Length": str(len(encoded)),
                }
            )
        request = urllib.request.Request(
            url,
            data=encoded,
            headers=headers,
            method=method,
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                status = int(getattr(response, "status", 0))
                if 300 <= status < 400:
                    raise DependencyUnavailable(
                        "control-state provider redirect response rejected"
                    )
                if status not in {200, 202}:
                    raise DependencyUnavailable(
                        f"control-state provider HTTP status {status}"
                    )
                raw = response.read(self._max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            raise DependencyUnavailable(
                f"control-state provider HTTP status {exc.code}"
            ) from exc
        except DependencyUnavailable:
            raise
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise DependencyUnavailable(
                f"control-state provider request failed: {type(exc).__name__}"
            ) from exc
        if not isinstance(raw, bytes):
            raise DependencyUnavailable("control-state provider response is not bytes")
        if len(raw) > self._max_response_bytes:
            raise DependencyUnavailable("control-state provider response exceeds size bound")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DependencyUnavailable("control-state provider response is not valid JSON") from exc

    @staticmethod
    def _envelope(value: object, *, object_name: str, payload_name: str) -> object:
        if (
            not isinstance(value, dict)
            or set(value) != {"object", payload_name}
            or value.get("object") != object_name
        ):
            raise DependencyUnavailable("control-state provider response envelope is invalid")
        return value[payload_name]

    def append(self, event: ControlStateEvent) -> ReplicationAcknowledgement:
        """Append one exact event and validate the provider acknowledgement."""
        if not isinstance(event, ControlStateEvent):
            raise TypeError("control-state event is required")
        raw = self._request(
            "POST",
            "/v1/control-state/events",
            body={"object": "control_state_event", "event": event.as_dict()},
        )
        # Keep the outer request envelope strict as well as the response DTO.
        acknowledgement_value = self._envelope(
            raw,
            object_name="replication_acknowledgement",
            payload_name="acknowledgement",
        )
        try:
            acknowledgement = ReplicationAcknowledgement.from_dict(
                acknowledgement_value
            )
        except (TypeError, ValueError) as exc:
            raise DependencyUnavailable(
                "control-state provider acknowledgement is invalid"
            ) from exc
        if (
            acknowledgement.provider_id != self.provider_id
            or acknowledgement.protocol_version != self.protocol_version
            or acknowledgement.event_id != event.event_id
            or acknowledgement.cluster_id != event.cluster_id
            or acknowledgement.owner_epoch != event.owner_epoch
            or acknowledgement.sequence != event.sequence
        ):
            raise DependencyUnavailable("control-state provider acknowledgement does not match event")
        if not acknowledgement.durable:
            raise DependencyUnavailable("control-state provider acknowledgement is not durable")
        decision = validate_replication_acknowledgement(
            event,
            acknowledgement,
            self.capabilities,
            minimum_data_replicas=1,
        )
        if not decision.accepted:
            raise DependencyUnavailable(
                f"control-state provider acknowledgement rejected: {decision.reason}"
            )
        return acknowledgement

    def read(
        self,
        cluster_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 128,
    ) -> tuple[ControlStateEvent, ...]:
        """Read a bounded, strictly increasing event page."""
        # Let the domain identity grammar remain the canonical boundary.
        probe = ControlStateEvent(
            event_id="probe",
            cluster_id=cluster_id,
            resource_kind="job",
            resource_id="probe",
            owner_id="probe",
            owner_epoch=1,
            sequence=1,
            payload_digest="0" * 64,
            protocol_version=self.protocol_version,
        )
        _bounded_read_cursor(after_sequence)
        _bounded_limit(limit)
        raw = self._request(
            "GET",
            "/v1/control-state/events",
            query={
                "cluster_id": probe.cluster_id,
                "after_sequence": after_sequence,
                "limit": limit,
            },
        )
        events_value = self._envelope(
            raw,
            object_name="control_state_events",
            payload_name="events",
        )
        if not isinstance(events_value, list) or len(events_value) > limit:
            raise DependencyUnavailable("control-state provider event page is invalid")
        events: list[ControlStateEvent] = []
        previous = after_sequence
        for value in events_value:
            try:
                event = ControlStateEvent.from_dict(value)
            except (TypeError, ValueError) as exc:
                raise DependencyUnavailable("control-state provider event is invalid") from exc
            if (
                event.cluster_id != probe.cluster_id
                or event.protocol_version != self.protocol_version
                or event.sequence <= previous
            ):
                raise DependencyUnavailable("control-state provider event page is not ordered")
            events.append(event)
            previous = event.sequence
        return tuple(events)

    def fence(self, ownership: OwnershipScope) -> FenceReceipt:
        """Request an external fence for one exact ownership scope."""
        if not isinstance(ownership, OwnershipScope):
            raise TypeError("ownership scope is required")
        raw = self._request(
            "POST",
            "/v1/control-state/fence",
            body={"object": "owner_fence_request", "ownership": ownership.as_dict()},
        )
        receipt_value = self._envelope(
            raw,
            object_name="fence_receipt",
            payload_name="receipt",
        )
        try:
            receipt = FenceReceipt.from_dict(receipt_value)
        except (TypeError, ValueError) as exc:
            raise DependencyUnavailable("control-state provider fence receipt is invalid") from exc
        if (
            receipt.provider_id != self.provider_id
            or receipt.protocol_version != self.protocol_version
            or receipt.cluster_id != ownership.cluster_id
            or receipt.resource_kind != ownership.resource_kind
            or receipt.resource_id != ownership.resource_id
            or receipt.previous_owner_id != ownership.owner_id
            or receipt.previous_owner_epoch != ownership.epoch
        ):
            raise DependencyUnavailable("control-state provider fence receipt does not match scope")
        if not receipt.external:
            raise DependencyUnavailable("control-state provider fence receipt is not external")
        return receipt


__all__ = ["HttpsControlStateProvider"]
