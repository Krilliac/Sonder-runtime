"""HTTP host for the portable client schema and reconnect contract.

The application owns the schema, the resumable-stream semantics and the
reconnect planner (``application/protocol``); the application graph composes
one deny-by-default :class:`ProtocolApplicationFacade` from the served tool
catalog.  This module is the hosting interface the protocol port expects:

* it supplies the authorization decisions, since only the HTTP adapter knows
  whether a request authenticated.  A request-scoped view may reconnect only
  when its request was authorized; only this host may open a stream.
* it owns one real stream, ``control.<instance>``, and is its only producer.
  ``<instance>`` is a random identifier minted when the host is built, and
  the schema route advertises the full stream id.  Each event
  is a ``control.snapshot`` carrying the runtime's process-global permission
  mode.  The host publishes when it observes the mode differ from the last
  published value -- on a mode change through the API, on a mode read, and
  before every reconnect -- so a change made elsewhere (for example in the
  REPL) is recorded the next time any client looks, not at the instant it
  happened.  The stream is in memory and its sequence starts again at 1
  whenever a host is built (a process restart, or a new application graph).
  Because each host's stream has a fresh id, a cursor kept from an earlier
  host names a stream that no longer exists and is ``rejected`` as unknown,
  which tells the client to refetch the schema, learn the current stream id
  and resume from watermark 0.  Without the fresh id, an old watermark at or
  below the new stream's watermark would resume silently with no events and
  leave the client holding a stale mode.

Sockets, authentication, CORS and response writing stay in ``serve.py``.
"""
from __future__ import annotations

import logging
import threading
import uuid
from typing import Any, Callable, Mapping

from sonder_runtime.application.protocol.events import ProtocolEventType
from sonder_runtime.application.protocol.facade import ProtocolApplicationFacade
from sonder_runtime.application.protocol.mobile_parity import (
    decode_reconnect_request,
    encode_client_schema,
    encode_reconnect_response,
)

logger = logging.getLogger(__name__)

CONTROL_STREAM_PREFIX = "control"
HOST_CLIENT_ID = "sonder-http-host"
SCHEMA_ROUTE = "/v1/client/schema"
RECONNECT_ROUTE = "/v1/client/reconnect"


class _HostAuthorization:
    """Only this host may create its stream; it never reconnects as a client."""

    def authorize(self, operation: str, client_id: str) -> bool:
        return operation == "protocol.stream.create" and client_id == HOST_CLIENT_ID


class _RequestAuthorization:
    """One request's decision: reconnect iff the HTTP layer authenticated it."""

    def __init__(self, authenticated: bool) -> None:
        self._authenticated = authenticated is True

    def authorize(self, operation: str, client_id: str) -> bool:
        del client_id
        return self._authenticated and operation == "protocol.reconnect"


class ClientProtocolHost:
    """Serve the schema and reconnect plans over one host-owned stream."""

    def __init__(
        self,
        protocol: ProtocolApplicationFacade,
        *,
        control_state: Callable[[], Mapping[str, Any]],
        capacity: int = 256,
    ) -> None:
        if not isinstance(protocol, ProtocolApplicationFacade):
            raise TypeError("protocol must be a ProtocolApplicationFacade")
        if not callable(control_state):
            raise TypeError("control_state must be callable")
        self._protocol = protocol
        self._graph = protocol.graph
        self._control_state = control_state
        self._publisher = ProtocolApplicationFacade(
            self._graph, authorization=_HostAuthorization(),
        )
        self._stream_id = "%s.%s" % (CONTROL_STREAM_PREFIX, uuid.uuid4().hex)
        self._stream = self._publisher.open_stream(
            self._stream_id, client_id=HOST_CLIENT_ID, capacity=capacity,
        )
        self._lock = threading.Lock()
        self._last: dict[str, Any] | None = None
        self.observe()

    @property
    def protocol(self) -> ProtocolApplicationFacade:
        return self._protocol

    @property
    def stream_id(self) -> str:
        """This host's control stream id; a new host never reuses one."""
        return self._stream_id

    def schema_payload(self) -> dict[str, Any]:
        """The schema envelope plus the streams this host currently serves.

        ``schema`` is the exact envelope a client caches and whose digest it
        advertises on reconnect; it depends only on the catalog.  ``streams``
        names the live stream ids, which change whenever the host is rebuilt,
        so they sit beside the schema and never enter its digest.
        """
        payload = encode_client_schema(self._graph.schema)
        payload["streams"] = [{
            "stream_id": self._stream_id,
            "event_types": [ProtocolEventType.CONTROL_SNAPSHOT.value],
        }]
        return payload

    def observe(self) -> bool:
        """Publish the current control state if it differs from the last one.

        Returns whether an event was published.  When the stream's retained
        history is full, the last published state is folded into a snapshot
        first, so a client far behind receives the snapshot plus the tail
        instead of a gap.  The state is read under the lock, so concurrent
        observers publish in the order they read and the newest event always
        names the state the last observer saw.
        """
        with self._lock:
            state = dict(self._control_state())
            if state == self._last:
                return False
            if self._last is not None and (
                len(self._stream.retained_events()) >= self._stream.capacity
            ):
                self._stream.publish_snapshot(dict(self._last))
            self._publisher.publish(
                self._stream_id, ProtocolEventType.CONTROL_SNAPSHOT, state,
                event_id=uuid.uuid4().hex,
            )
            self._last = state
            return True

    def reconnect(self, body: Any, *, authenticated: bool) -> dict[str, Any]:
        """Decode, authorize and plan one reconnect; return the wire response.

        Raises ``MobileWireError`` for a malformed body and
        ``ProtocolAuthorizationError`` when the request is not authorized.
        """
        request = decode_reconnect_request(body)
        view = ProtocolApplicationFacade(
            self._graph, authorization=_RequestAuthorization(authenticated),
        )
        self.observe()
        return encode_reconnect_response(view.reconnect(request))


__all__ = [
    "CONTROL_STREAM_PREFIX", "ClientProtocolHost", "HOST_CLIENT_ID",
    "RECONNECT_ROUTE", "SCHEMA_ROUTE",
]
