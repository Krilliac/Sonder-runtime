"""Strict HTTPS client for authenticated compute-node observations."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import json
import time
import urllib.error
import urllib.request
from typing import Any

from ...application.compute_fabric.wire import snapshot_from_wire
from ...domain.common.errors import DependencyUnavailable
from ...domain.compute_fabric import ComputeNode, NodeSnapshot


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _default_opener(request: urllib.request.Request, *, timeout: float):
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


class HttpsComputeSnapshotSource:
    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: float = 2.0,
        max_response_bytes: int = 64 * 1024,
        opener: Callable[..., Any] = _default_opener,
    ) -> None:
        if not isinstance(api_key, str) or not api_key:
            raise ValueError("compute snapshot API key is required")
        if not 0 < timeout_seconds <= 30:
            raise ValueError("compute snapshot timeout must be within 0..30 seconds")
        if not 1024 <= max_response_bytes <= 1024 * 1024:
            raise ValueError("compute snapshot response bound must be within 1KiB..1MiB")
        self._api_key = api_key
        self._timeout = float(timeout_seconds)
        self._max_response_bytes = max_response_bytes
        self._opener = opener

    def snapshot(self, node: ComputeNode, *, now: datetime) -> NodeSnapshot:
        if node.local or not node.origin:
            raise ValueError("HTTPS snapshot source requires a configured remote node")
        request = urllib.request.Request(
            node.origin.rstrip("/") + "/v1/compute/snapshot",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Accept": "application/json",
            },
            method="GET",
        )
        started = time.monotonic()
        try:
            with self._opener(request, timeout=self._timeout) as response:
                status = int(getattr(response, "status", 0))
                if 300 <= status < 400:
                    raise DependencyUnavailable("compute snapshot redirect response rejected")
                if status != 200:
                    raise DependencyUnavailable(
                        f"compute snapshot HTTP status {status}"
                    )
                raw = response.read(self._max_response_bytes + 1)
        except DependencyUnavailable:
            raise
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise DependencyUnavailable(
                f"compute snapshot request failed: {type(exc).__name__}"
            ) from exc
        if len(raw) > self._max_response_bytes:
            raise DependencyUnavailable("compute snapshot response exceeds size bound")
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DependencyUnavailable("compute snapshot response is not valid JSON") from exc
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"object", "snapshot"}
            or envelope.get("object") != "compute_snapshot"
        ):
            raise DependencyUnavailable("compute snapshot response envelope is invalid")
        elapsed_ms = max(0.0, (time.monotonic() - started) * 1000.0)
        try:
            return snapshot_from_wire(
                node,
                envelope["snapshot"],
                now=now,
                round_trip_ms=elapsed_ms,
            )
        except (TypeError, ValueError) as exc:
            raise DependencyUnavailable(str(exc)) from exc


__all__ = ["HttpsComputeSnapshotSource"]
