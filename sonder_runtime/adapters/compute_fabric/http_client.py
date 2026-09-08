"""Strict HTTPS client for authenticated compute-node observations."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import hashlib
import hmac
import http.client
import ipaddress
import json
import re
import ssl
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


class PinnedHttpsClientError(RuntimeError):
    """Stable, redacted failure for the fixed mobility HTTPS transport."""


@dataclass(frozen=True)
class PinnedHttpsResponse:
    """A bounded response after a direct, leaf-pinned HTTPS exchange."""

    status: int
    body: bytes


def _direct_https_connection(host: str, port: int, timeout: float, context):
    """Create one direct connection; unlike urllib this never consults proxies."""
    return http.client.HTTPSConnection(host, port=port, timeout=timeout, context=context)


class PinnedHttpsClient:
    """One fixed origin whose TLS leaf is checked before authentication.

    This deliberately has no URL input per request, no proxy configuration, and
    no redirect follow-up.  The caller supplies headers lazily so a bearer is
    not resolved until after the DER leaf matches its configured pin.
    """

    _MAX_ORIGIN_LENGTH = 512

    def __init__(
        self,
        origin: str,
        certificate_sha256: str,
        *,
        timeout_seconds: float,
        connection_factory: Callable[..., Any] = _direct_https_connection,
    ) -> None:
        self._host, self._port = self._parse_origin(origin)
        if (
            not isinstance(certificate_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", certificate_sha256) is None
        ):
            raise PinnedHttpsClientError("MOBILITY_CONFIG")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not 0 < float(timeout_seconds) <= 30
        ):
            raise PinnedHttpsClientError("MOBILITY_CONFIG")
        if not callable(connection_factory):
            raise PinnedHttpsClientError("MOBILITY_CONFIG")
        self._certificate_sha256 = certificate_sha256
        self._timeout_seconds = float(timeout_seconds)
        self._connection_factory = connection_factory

    @classmethod
    def _parse_origin(cls, origin: object) -> tuple[str, int]:
        """Parse only the root-only HTTPS origin admitted by mobility config."""
        if (
            not isinstance(origin, str)
            or not 1 <= len(origin) <= cls._MAX_ORIGIN_LENGTH
            or not origin.startswith("https://")
            or any(ord(character) < 33 or ord(character) > 126 for character in origin)
        ):
            raise PinnedHttpsClientError("MOBILITY_CONFIG")
        authority = origin[len("https://") :]
        if authority.endswith("/"):
            authority = authority[:-1]
        if (
            not authority
            or len(authority) > cls._MAX_ORIGIN_LENGTH
            or any(character in authority for character in "/?#@")
        ):
            raise PinnedHttpsClientError("MOBILITY_CONFIG")
        if authority.startswith("["):
            closing = authority.find("]")
            if closing < 1 or authority[closing + 1 : closing + 2] != ":":
                raise PinnedHttpsClientError("MOBILITY_CONFIG")
            host, port_text = authority[1:closing], authority[closing + 2 :]
            try:
                if not isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address):
                    raise PinnedHttpsClientError("MOBILITY_CONFIG")
            except ValueError:
                raise PinnedHttpsClientError("MOBILITY_CONFIG") from None
        else:
            host, separator, port_text = authority.rpartition(":")
            if (
                not separator
                or not host
                or ":" in host
                or not cls._valid_host(host)
            ):
                raise PinnedHttpsClientError("MOBILITY_CONFIG")
        if (
            not port_text.isascii()
            or not port_text.isdecimal()
            or len(port_text) > 5
        ):
            raise PinnedHttpsClientError("MOBILITY_CONFIG")
        try:
            port = int(port_text)
        except (TypeError, ValueError, OverflowError):
            raise PinnedHttpsClientError("MOBILITY_CONFIG") from None
        if not 1 <= port <= 65_535:
            raise PinnedHttpsClientError("MOBILITY_CONFIG")
        return host, port

    @staticmethod
    def _valid_host(host: str) -> bool:
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            labels = host.split(".")
            return bool(labels) and all(
                re.fullmatch(
                    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label
                )
                is not None
                for label in labels
            )

    @staticmethod
    def _response_length(headers: object, body: bytes) -> bool:
        try:
            get_all = getattr(headers, "get_all", None)
            if callable(get_all):
                values = get_all("Content-Length") or ()
                return len(values) == 1 and values[0] == str(len(body))
            get = getattr(headers, "get", None)
            return callable(get) and get("Content-Length") == str(len(body))
        except Exception:
            return False

    @staticmethod
    def _target_path(path: object) -> str:
        if (
            not isinstance(path, str)
            or not 1 <= len(path) <= 4096
            or not path.startswith("/")
            or any(ord(character) < 33 or ord(character) > 126 for character in path)
            or any(character in path for character in ("?", "#", "\\"))
        ):
            raise PinnedHttpsClientError("MOBILITY_PROTOCOL")
        return path

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None,
        headers_supplier: Callable[[], dict[str, str]],
        response_limit: int,
    ) -> PinnedHttpsResponse:
        """Make one direct request after pinning the live TLS leaf certificate."""
        if (
            not isinstance(method, str)
            or method not in {"GET", "POST", "PUT"}
            or (body is not None and not isinstance(body, bytes))
            or not callable(headers_supplier)
            or type(response_limit) is not int
            or not 1 <= response_limit <= 1024 * 1024
        ):
            raise PinnedHttpsClientError("MOBILITY_PROTOCOL")
        path = self._target_path(path)
        connection = None
        try:
            try:
                context = ssl.create_default_context()
                connection = self._connection_factory(
                    self._host, self._port, self._timeout_seconds, context
                )
                connection.connect()
            except ssl.SSLError:
                raise PinnedHttpsClientError("MOBILITY_TLS") from None
            except Exception:
                raise PinnedHttpsClientError("MOBILITY_UNAVAILABLE") from None

            try:
                certificate = connection.sock.getpeercert(binary_form=True)
                digest = hashlib.sha256(certificate).hexdigest()
            except Exception:
                raise PinnedHttpsClientError("MOBILITY_TLS") from None
            if not hmac.compare_digest(digest, self._certificate_sha256):
                raise PinnedHttpsClientError("MOBILITY_TLS")

            # This call stays after the pin check.  In particular, a failed
            # handshake or leaf mismatch cannot make a credential observable.
            try:
                headers = headers_supplier()
            except Exception:
                raise PinnedHttpsClientError("MOBILITY_CREDENTIAL") from None
            if (
                not isinstance(headers, dict)
                or any(
                    not isinstance(name, str)
                    or not isinstance(value, str)
                    or "\r" in name
                    or "\n" in name
                    or "\r" in value
                    or "\n" in value
                    for name, value in headers.items()
                )
            ):
                raise PinnedHttpsClientError("MOBILITY_CREDENTIAL")
            try:
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
                status = getattr(response, "status", None)
            except ssl.SSLError:
                raise PinnedHttpsClientError("MOBILITY_TLS") from None
            except Exception:
                raise PinnedHttpsClientError("MOBILITY_UNAVAILABLE") from None
            if type(status) is not int:
                raise PinnedHttpsClientError("MOBILITY_PROTOCOL")
            if 300 <= status < 400:
                raise PinnedHttpsClientError("MOBILITY_REDIRECT")
            try:
                raw = response.read(response_limit + 1)
            except Exception:
                raise PinnedHttpsClientError("MOBILITY_UNAVAILABLE") from None
            if not isinstance(raw, bytes):
                raise PinnedHttpsClientError("MOBILITY_PROTOCOL")
            if len(raw) > response_limit or not self._response_length(
                getattr(response, "headers", None), raw
            ):
                raise PinnedHttpsClientError("MOBILITY_LENGTH")
            return PinnedHttpsResponse(status=status, body=raw)
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass


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
        if not isinstance(raw, bytes):
            raise DependencyUnavailable("compute snapshot response is not bytes")
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
        if not isinstance(raw, bytes):
            raise DependencyUnavailable("compute job response is not bytes")
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
        if not isinstance(raw, bytes):
            raise DependencyUnavailable("compute artifact response is not bytes")
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


__all__ = [
    "HttpsComputeJobTransport",
    "HttpsComputeSnapshotSource",
    "PinnedHttpsClient",
    "PinnedHttpsClientError",
    "PinnedHttpsResponse",
]
