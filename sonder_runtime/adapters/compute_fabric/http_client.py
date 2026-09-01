"""Strict HTTPS client for authenticated compute-node observations."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ...application.compute_fabric.jobs import (
    MAX_COMPUTE_ARTIFACT_BYTES,
    RemoteArtifactPayload,
    RemoteArtifactReceipt,
    RemoteJobEnvelope,
    RemoteJobReceipt,
    validate_remote_job_receipt,
)
from ...application.compute_fabric.wire import (
    job_envelope_to_wire,
    job_receipt_from_wire,
    snapshot_from_wire,
)
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


class HttpsComputeJobTransport:
    """Authenticated, bounded and redirect-free remote job transport."""

    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: float = 5.0,
        max_response_bytes: int = 64 * 1024,
        opener: Callable[..., Any] = _default_opener,
    ) -> None:
        if not isinstance(api_key, str) or not api_key:
            raise ValueError("compute job API key is required")
        if not 0 < timeout_seconds <= 30:
            raise ValueError("compute job timeout must be within 0..30 seconds")
        if not 1024 <= max_response_bytes <= 1024 * 1024:
            raise ValueError("compute job response bound must be within 1KiB..1MiB")
        self._api_key = api_key
        self._timeout = float(timeout_seconds)
        self._max_response_bytes = max_response_bytes
        self._opener = opener

    @staticmethod
    def _origin(node: ComputeNode) -> str:
        if node.local or not node.origin:
            raise ValueError("HTTPS compute transport requires a configured remote node")
        return node.origin.rstrip("/")

    def _request(
        self,
        node: ComputeNode,
        *,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        allowed_statuses: frozenset[int] = frozenset({200}),
        not_found_none: bool = False,
    ) -> RemoteJobReceipt | None:
        data = None
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        if body is not None:
            data = json.dumps(
                body, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self._origin(node) + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                status = int(getattr(response, "status", 0))
                if 300 <= status < 400:
                    raise DependencyUnavailable("compute job redirect response rejected")
                if status == 404 and not_found_none:
                    return None
                if status not in allowed_statuses:
                    raise DependencyUnavailable(f"compute job HTTP status {status}")
                raw = response.read(self._max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and not_found_none:
                return None
            raise DependencyUnavailable(f"compute job HTTP status {exc.code}") from exc
        except DependencyUnavailable:
            raise
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise DependencyUnavailable(
                f"compute job request failed: {type(exc).__name__}"
            ) from exc
        if len(raw) > self._max_response_bytes:
            raise DependencyUnavailable("compute job response exceeds size bound")
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DependencyUnavailable("compute job response is not valid JSON") from exc
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"object", "job"}
            or envelope.get("object") != "compute_job"
        ):
            raise DependencyUnavailable("compute job response envelope is invalid")
        try:
            receipt = job_receipt_from_wire(envelope["job"])
        except (TypeError, ValueError) as exc:
            raise DependencyUnavailable(str(exc)) from exc
        return validate_remote_job_receipt(receipt, worker_id=node.node_id)

    def submit(
        self, node: ComputeNode, envelope: RemoteJobEnvelope
    ) -> RemoteJobReceipt:
        receipt = self._request(
            node,
            method="POST",
            path="/v1/compute/jobs",
            body=job_envelope_to_wire(envelope),
            allowed_statuses=frozenset({200, 202}),
        )
        assert receipt is not None
        return validate_remote_job_receipt(
            receipt,
            worker_id=node.node_id,
            controller_job_id=envelope.controller_job_id,
            idempotency_key=envelope.idempotency_key,
            request_sha256=envelope.request_sha256,
        )

    def status(self, node: ComputeNode, remote_job_id: str) -> RemoteJobReceipt:
        receipt = self._request(
            node,
            method="GET",
            path="/v1/compute/jobs/" + urllib.parse.quote(remote_job_id, safe=""),
        )
        assert receipt is not None
        return validate_remote_job_receipt(
            receipt, worker_id=node.node_id, remote_job_id=remote_job_id,
        )

    def by_idempotency(
        self, node: ComputeNode, idempotency_key: str
    ) -> RemoteJobReceipt | None:
        receipt = self._request(
            node,
            method="GET",
            path=(
                "/v1/compute/jobs/by-idempotency/"
                + urllib.parse.quote(idempotency_key, safe="")
            ),
            not_found_none=True,
        )
        if receipt is None:
            return None
        return validate_remote_job_receipt(
            receipt, worker_id=node.node_id, idempotency_key=idempotency_key,
        )

    def cancel(
        self, node: ComputeNode, remote_job_id: str, *, reason: str
    ) -> RemoteJobReceipt:
        receipt = self._request(
            node,
            method="POST",
            path=(
                "/v1/compute/jobs/"
                + urllib.parse.quote(remote_job_id, safe="")
                + "/cancel"
            ),
            body={"reason": reason},
        )
        assert receipt is not None
        return validate_remote_job_receipt(
            receipt, worker_id=node.node_id, remote_job_id=remote_job_id,
        )

    def fetch_artifact(
        self,
        node: ComputeNode,
        remote_job_id: str,
        expected: RemoteArtifactReceipt,
    ) -> RemoteArtifactPayload:
        if not isinstance(expected, RemoteArtifactReceipt):
            raise TypeError("expected artifact receipt is required")
        if expected.size_bytes > MAX_COMPUTE_ARTIFACT_BYTES:
            raise DependencyUnavailable("compute artifact exceeds transport size bound")
        path = (
            "/v1/compute/jobs/"
            + urllib.parse.quote(remote_job_id, safe="")
            + "/artifacts/"
            + urllib.parse.quote(expected.name, safe="")
        )
        request = urllib.request.Request(
            self._origin(node) + path,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Accept": expected.mime_type,
            },
            method="GET",
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                status = int(getattr(response, "status", 0))
                if 300 <= status < 400:
                    raise DependencyUnavailable("compute artifact redirect response rejected")
                if status != 200:
                    raise DependencyUnavailable(f"compute artifact HTTP status {status}")
                headers = getattr(response, "headers", {})
                raw = response.read(expected.size_bytes + 1)
        except DependencyUnavailable:
            raise
        except urllib.error.HTTPError as exc:
            raise DependencyUnavailable(f"compute artifact HTTP status {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise DependencyUnavailable(
                f"compute artifact request failed: {type(exc).__name__}"
            ) from exc
        if len(raw) != expected.size_bytes:
            raise DependencyUnavailable("compute artifact size differs from its receipt")
        content_length = headers.get("Content-Length")
        content_type = str(headers.get("Content-Type", "")).split(";", 1)[0]
        digest = headers.get("X-Sonder-Artifact-Sha256")
        if content_length != str(expected.size_bytes):
            raise DependencyUnavailable("compute artifact length header differs from its receipt")
        if content_type != expected.mime_type:
            raise DependencyUnavailable("compute artifact type differs from its receipt")
        if digest != expected.sha256:
            raise DependencyUnavailable("compute artifact digest header differs from its receipt")
        try:
            return RemoteArtifactPayload(expected, raw)
        except ValueError as exc:
            raise DependencyUnavailable(
                "compute artifact content differs from its receipt"
            ) from exc


__all__ = ["HttpsComputeJobTransport", "HttpsComputeSnapshotSource"]
