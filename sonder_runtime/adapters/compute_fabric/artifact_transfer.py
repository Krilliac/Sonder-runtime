"""Configured HTTPS chunk transport and resumable streaming into a scoped cache."""

import hashlib
import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request

from ...application.artifacts.transfer import (
    ArtifactRange,
    TransferError,
    check_digest,
    bounded_int,
)
from ...domain.compute_fabric import ComputeNode
from .http_client import _NoRedirect


class HttpsArtifactTransferPeer:
    def __init__(self, node: ComputeNode, *, credential_provider, timeout_seconds=10):
        if not isinstance(node, ComputeNode) or node.local or not node.origin:
            raise TransferError("INVALID_PEER")
        if not callable(credential_provider) or not 0 < timeout_seconds <= 30:
            raise TransferError("INVALID_PEER")
        self.origin = node.origin.rstrip("/")
        self._credentials = credential_provider
        self._timeout = timeout_seconds
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

    @classmethod
    def for_test_loopback(cls, origin, *, credential_provider):
        """Explicit test-only HTTP loopback transport; never selected from host config."""
        parsed = urllib.parse.urlsplit(origin)
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = False
        if (
            parsed.scheme != "http"
            or not loopback
            or not parsed.port
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise TransferError("INVALID_TEST_ORIGIN")
        result = object.__new__(cls)
        result.origin, result._credentials, result._timeout = (
            origin,
            credential_provider,
            10,
        )
        result._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )
        return result

    def _request(
        self, method, path, *, payload=None, body=None, headers=None, binary=False
    ):
        credential = self._credentials()
        if (
            not isinstance(credential, str)
            or not credential
            or any(c in credential for c in "\r\n")
        ):
            raise TransferError("UNAVAILABLE")
        metadata = {
            "Authorization": "Bearer " + credential,
            "Accept": "application/json",
        }
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode()
            if len(body) > 32768:
                raise TransferError("INVALID_BOUND")
            metadata["Content-Type"] = "application/json"
        metadata.update(headers or {})
        request = urllib.request.Request(
            self.origin + path, method=method, data=body, headers=metadata
        )
        limit = 1024 * 1024 if binary else 32768
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                if response.status not in (200, 202):
                    raise TransferError("PEER_STATUS")
                raw = response.read(limit + 1)
                response_headers = response.headers
        except (urllib.error.URLError, OSError, ValueError):
            raise TransferError("PEER_UNAVAILABLE") from None
        if not isinstance(raw, bytes):
            raise TransferError("PEER_PROTOCOL")
        if len(raw) > limit or response_headers.get("Content-Length") != str(len(raw)):
            raise TransferError("PEER_LENGTH")
        if binary:
            return raw, response_headers
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            raise TransferError("PEER_PROTOCOL") from None
        if not isinstance(result, dict):
            raise TransferError("PEER_PROTOCOL")
        return result

    @staticmethod
    def _id(value):
        import re

        if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{32}", value):
            raise TransferError("PEER_IDENTITY")
        return value

    def begin(self, spec, command_id):
        return self._request(
            "POST",
            "/v1/artifact-transfers",
            payload={"spec": spec, "command_id": command_id},
        )

    def inspect(self, transfer_id):
        return self._request("GET", "/v1/artifact-transfers/" + self._id(transfer_id))

    def append(self, transfer_id, offset, digest, body):
        return self._request(
            "PUT",
            "/v1/artifact-transfers/"
            + self._id(transfer_id)
            + "/chunks/"
            + str(offset),
            body=body,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Sonder-Chunk-Sha256": digest,
            },
        )

    def seal(self, transfer_id, command_id):
        return self._request(
            "POST",
            "/v1/artifact-transfers/" + self._id(transfer_id) + "/seal",
            payload={"command_id": command_id},
        )

    def artifact(self, artifact_id):
        return self._request("GET", "/v1/artifacts/" + self._id(artifact_id))

    def read_range(self, artifact_id, offset, length):
        bounded_int(offset, 0, 64 * 1024**3)
        bounded_int(length, 1, 1024 * 1024)
        raw, headers = self._request(
            "GET",
            "/v1/artifacts/"
            + self._id(artifact_id)
            + f"/bytes?offset={offset}&length={length}",
            binary=True,
        )
        try:
            actual_offset = int(headers["X-Sonder-Offset"])
            size = int(headers["X-Sonder-Size"])
            sha = headers["X-Sonder-Artifact-Sha256"]
            chunk = headers["X-Sonder-Chunk-Sha256"]
            check_digest(sha)
            check_digest(chunk)
        except (KeyError, TypeError, ValueError):
            raise TransferError("PEER_PROTOCOL") from None
        if (
            actual_offset != offset
            or size < offset
            or len(raw) != min(length, size - offset)
            or hashlib.sha256(raw).hexdigest() != chunk
        ):
            raise TransferError("PEER_DIGEST")
        return ArtifactRange(artifact_id, sha, size, offset, raw, chunk)


class ArtifactTransferClient:
    """No arbitrary paths: callers own the source stream and scoped destination service."""

    def __init__(self, peer):
        self.peer = peer

    @staticmethod
    def _spec(spec):
        check_digest(spec["sha256"])
        bounded_int(spec["size_bytes"], 0, 64 * 1024**3)

    def upload(self, stream, spec, command_id, *, max_chunks=None):
        self._spec(spec)
        receipt = self.peer.begin(spec, command_id)
        identity = HttpsArtifactTransferPeer._id(receipt["transfer_id"])
        offset = receipt["offset"]
        bounded_int(offset, 0, spec["size_bytes"])
        chunk_bytes = receipt["chunk_bytes"]
        bounded_int(chunk_bytes, 1, 1024 * 1024)
        stream.seek(offset)
        count = 0
        while offset < spec["size_bytes"]:
            if max_chunks is not None and count >= max_chunks:
                return self.peer.inspect(identity)
            data = stream.read(min(chunk_bytes, spec["size_bytes"] - offset))
            if not isinstance(data, bytes) or len(data) != min(
                chunk_bytes, spec["size_bytes"] - offset
            ):
                raise TransferError("SOURCE_LENGTH")
            digest = hashlib.sha256(data).hexdigest()
            ack = self.peer.append(identity, offset, digest, data)
            if (
                ack.get("offset") != offset
                or ack.get("next_offset") != offset + len(data)
                or ack.get("chunk_sha256") != digest
            ):
                raise TransferError("PEER_RECEIPT")
            offset += len(data)
            count += 1
        return self.peer.seal(
            identity, "seal:" + hashlib.sha256(command_id.encode()).hexdigest()
        )

    def download(self, expected, destination, command_id, context, *, max_chunks=None):
        self._spec(expected)
        identity = HttpsArtifactTransferPeer._id(expected["artifact_id"])
        if self.peer.artifact(identity) != expected:
            raise TransferError("PEER_IDENTITY")
        spec = {key: expected[key] for key in ("sha256", "size_bytes", "media_type")}
        local = destination.begin_upload(spec, command_id, context)
        offset = local["offset"]
        count = 0
        while offset < spec["size_bytes"]:
            if max_chunks is not None and count >= max_chunks:
                return destination.inspect_upload(local["transfer_id"], context)
            length = min(local["chunk_bytes"], spec["size_bytes"] - offset)
            chunk = self.peer.read_range(identity, offset, length)
            if (
                chunk.sha256 != spec["sha256"]
                or chunk.size_bytes != spec["size_bytes"]
                or chunk.offset != offset
                or chunk.length != length
                or hashlib.sha256(chunk.body).hexdigest() != chunk.chunk_sha256
            ):
                raise TransferError("PEER_DIGEST")
            destination.append_chunk(
                local["transfer_id"], offset, chunk.chunk_sha256, chunk.body, context
            )
            offset += length
            count += 1
        return destination.seal_upload(
            local["transfer_id"],
            "seal:" + hashlib.sha256(command_id.encode()).hexdigest(),
            context,
        )
