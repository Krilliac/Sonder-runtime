"""Authenticated, bounded HTTPS transport for memory replication batches.

The sink is deliberately an adapter around the application replication
contract.  It does not discover peers, elect an owner, retry an ambiguous
write, or infer a quorum.  The coordinator owns those policy decisions; this
adapter only sends one digest-bound batch and validates the receiver receipt.
"""
from __future__ import annotations

import ipaddress
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from ...application.memory.replication import MemoryReplicationSink
from ...domain.common.errors import DependencyUnavailable
from ...domain.memory.replication import (
    MemoryReplicaReceipt,
    MemoryReplicationBatch,
    MemoryReplicationError,
)


_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MIN_REQUEST_BYTES = 1024
_MAX_REQUEST_BYTES = 64 * 1024 * 1024
_MIN_RESPONSE_BYTES = 1024
_MAX_RESPONSE_BYTES = 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _default_opener(request: urllib.request.Request, *, timeout: float):
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise ValueError(f"{field} must be a bounded stable identity")
    return value


def _api_key(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        raise ValueError("memory replication API key must be 1..512 characters")
    if any(ord(char) < 0x21 or ord(char) > 0x7E for char in value):
        raise ValueError("memory replication API key must contain printable ASCII")
    return value


def _origin(value: object) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("memory replication origin must be a bounded URL")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("memory replication origin must be an HTTP(S) URL without credentials")
    # Plain HTTP is retained only for an explicitly loopback test/development
    # peer.  A remote node must use TLS so a bearer key cannot be replayed or
    # modified in transit.
    if parsed.scheme != "https":
        try:
            if not ipaddress.ip_address(parsed.hostname).is_loopback:
                raise ValueError("remote memory replication origin must use HTTPS")
        except ValueError:
            if parsed.hostname not in {"localhost"}:
                raise ValueError("remote memory replication origin must use HTTPS") from None
    return value.rstrip("/")


class HttpsMemoryReplicationSink(MemoryReplicationSink):
    """Send one authoritative batch to an explicitly configured peer."""

    def __init__(
        self,
        *,
        identity: str,
        origin: str,
        api_key: str,
        timeout_seconds: float = 5.0,
        max_request_bytes: int = 8 * 1024 * 1024,
        max_response_bytes: int = 64 * 1024,
        opener: Callable[..., Any] = _default_opener,
    ) -> None:
        self.identity = _identity(identity, "replica identity")
        self._origin = _origin(origin)
        self._api_key = _api_key(api_key)
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or not 0 < timeout_seconds <= 30:
            raise ValueError("memory replication timeout must be within 0..30 seconds")
        if type(max_request_bytes) is not int or not _MIN_REQUEST_BYTES <= max_request_bytes <= _MAX_REQUEST_BYTES:
            raise ValueError(
                f"memory replication request bound must be within {_MIN_REQUEST_BYTES}..{_MAX_REQUEST_BYTES} bytes"
            )
        if type(max_response_bytes) is not int or not _MIN_RESPONSE_BYTES <= max_response_bytes <= _MAX_RESPONSE_BYTES:
            raise ValueError(
                f"memory replication response bound must be within {_MIN_RESPONSE_BYTES}..{_MAX_RESPONSE_BYTES} bytes"
            )
        if not callable(opener):
            raise TypeError("memory replication opener must be callable")
        self._timeout = float(timeout_seconds)
        self._max_request_bytes = max_request_bytes
        self._max_response_bytes = max_response_bytes
        self._opener = opener

    def apply(self, batch: MemoryReplicationBatch) -> MemoryReplicaReceipt:
        if not isinstance(batch, MemoryReplicationBatch):
            raise TypeError("memory replication batch is required")
        body = json.dumps(
            {
                "object": "memory_replication_batch",
                "batch": batch.as_dict(),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        if len(body) > self._max_request_bytes:
            raise DependencyUnavailable("memory replication request bound exceeded")
        request = urllib.request.Request(
            self._origin + "/v1/memory/replication/batches",
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                status = int(getattr(response, "status", 0))
                if 300 <= status < 400:
                    raise DependencyUnavailable("memory replication redirect response rejected")
                if status not in {200, 202}:
                    raise DependencyUnavailable(f"memory replication HTTP status {status}")
                raw = response.read(self._max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            raise DependencyUnavailable(f"memory replication HTTP status {exc.code}") from exc
        except DependencyUnavailable:
            raise
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise DependencyUnavailable(
                f"memory replication request failed: {type(exc).__name__}"
            ) from exc
        if not isinstance(raw, bytes):
            raise DependencyUnavailable("memory replication response is not bytes")
        if len(raw) > self._max_response_bytes:
            raise DependencyUnavailable("memory replication response exceeds size bound")
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DependencyUnavailable("memory replication response is not valid JSON") from exc
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"object", "receipt"}
            or envelope.get("object") != "memory_replication_receipt"
        ):
            raise DependencyUnavailable("memory replication response envelope is invalid")
        try:
            receipt = MemoryReplicaReceipt.from_dict(envelope["receipt"])
        except (MemoryReplicationError, TypeError, ValueError) as exc:
            raise DependencyUnavailable(str(exc)) from exc
        if receipt.replica_id != self.identity:
            raise DependencyUnavailable("memory replication receipt identity mismatch")
        if receipt.source_id != batch.source_id:
            raise DependencyUnavailable("memory replication receipt source mismatch")
        if receipt.source_epoch != batch.source_epoch:
            raise DependencyUnavailable("memory replication receipt epoch mismatch")
        if receipt.next_sequence != batch.next_sequence:
            raise DependencyUnavailable("memory replication receipt sequence mismatch")
        if receipt.batch_digest != batch.digest:
            raise DependencyUnavailable("memory replication receipt digest mismatch")
        if receipt.durable is not True:
            raise DependencyUnavailable("memory replication receipt is not durable")
        if receipt.inserted_records > len(batch.records):
            raise DependencyUnavailable("memory replication receipt count mismatch")
        return receipt


__all__ = ["HttpsMemoryReplicationSink"]
