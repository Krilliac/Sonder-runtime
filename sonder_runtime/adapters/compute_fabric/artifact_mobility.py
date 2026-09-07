"""Pinned, fixed-peer mobility-v1 transport with no dispatch composition.

This adapter only verifies and speaks to the one peer named in typed mobility
configuration.  It does not own a source, journal, retry loop, route, or any
automatic operation.  A later application service must supply immutable source
records and durable operation ownership before it invokes these methods.
"""
from __future__ import annotations

from collections.abc import Callable
import hashlib
import hmac
import json
import math
import re
from typing import Any

from ...application.artifacts.transfer import (
    MOBILITY_V1_VERSION,
    TransferError,
    recipient_mobility_attestation,
)
from ...platform.artifact_mobility_config import artifact_mobility_errors
from ...platform.artifact_mobility_source_config import artifact_mobility_source_errors
from ...platform.config import SonderConfig
from .http_client import PinnedHttpsClient, PinnedHttpsClientError


_ATTESTATION_FIELDS = frozenset(
    {
        "protocol_version",
        "receiver_identity_id",
        "principal_id",
        "project_id",
        "authorized_source_owner_id",
        "grant_id",
        "grant_revision",
        "can_write",
        "max_object_bytes",
        "sha256",
    }
)
_ENVELOPE_FIELDS = frozenset(
    {
        "protocol_version",
        "recipient_attestation",
        "command_id",
        "spec",
        "receipt",
    }
)
_RECEIPT_FIELDS = frozenset(
    {"transfer_id", "state", "offset", "chunk_bytes", "expires_at", "revision"}
)
_SEALED_RECEIPT_FIELDS = _RECEIPT_FIELDS | {"artifact"}
_ACK_FIELDS = frozenset({"offset", "next_offset", "chunk_sha256", "revision"})
_MOBILITY_STATES = frozenset({"open", "verifying", "sealed", "aborted", "failed"})
_DIGEST = re.compile(r"[0-9a-f]{64}")
_CAPABILITY = re.compile(r"[0-9a-f]{64}")
_TRANSFER_ID = re.compile(r"[0-9a-f]{32}")
_COMMAND_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_URL_LIKE_CREDENTIAL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_CONTROL_RESPONSE_LIMIT = 32 * 1024
_MAX_CHUNK_BYTES = 1024 * 1024
_TRANSPORT_CODES = frozenset(
    {
        "MOBILITY_CONFIG",
        "MOBILITY_TLS",
        "MOBILITY_UNAVAILABLE",
        "MOBILITY_CREDENTIAL",
        "MOBILITY_REDIRECT",
        "MOBILITY_LENGTH",
        "MOBILITY_PROTOCOL",
    }
)


def _fail(code: str) -> None:
    raise TransferError(code)


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        _fail("MOBILITY_PROTOCOL")


class ConfiguredArtifactMobilityPeer:
    """Direct, authenticated mobility-v1 peer for one operator-configured origin.

    The configuration is deliberately private in representation.  This object
    never accepts a caller-selected URL, source path, source scope, grant, or
    receiver identity.  It has no source authority and is intentionally not
    composed into HTTP, CLI, MCP, REPL, or automatic background work.
    """

    def __init__(
        self,
        config: SonderConfig,
        *,
        credential_provider: Callable[[str], str],
        connection_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not isinstance(config, SonderConfig) or not callable(credential_provider):
            _fail("MOBILITY_CONFIG")
        try:
            errors = [
                *artifact_mobility_source_errors(config),
                *artifact_mobility_errors(config),
            ]
        except Exception:
            _fail("MOBILITY_CONFIG")
        section = config.artifact_mobility
        source = config.artifact_mobility_source
        if errors or not section.enabled or not source.enabled:
            _fail("MOBILITY_CONFIG")
        try:
            transport_kwargs = {
                "timeout_seconds": section.attempt_timeout_seconds,
            }
            if connection_factory is not None:
                transport_kwargs["connection_factory"] = connection_factory
            self._transport = PinnedHttpsClient(
                section.destination_origin,
                section.destination_tls_certificate_sha256,
                **transport_kwargs,
            )
        except PinnedHttpsClientError:
            _fail("MOBILITY_CONFIG")
        self._credential_provider = credential_provider
        self._credential_id = section.destination_credential_id
        self._attestation_pin = section.expected_recipient_attestation_sha256
        self._source_owner_id = source.source_owner_id
        self._max_object_bytes = min(
            section.max_object_bytes, source.max_object_bytes
        )

    def __repr__(self) -> str:
        return "ConfiguredArtifactMobilityPeer(configured=True)"

    @staticmethod
    def _validate_spec(spec: object, *, maximum: int) -> dict:
        if not isinstance(spec, dict) or set(spec) != {
            "sha256", "size_bytes", "media_type"
        }:
            _fail("MOBILITY_SPEC")
        digest = spec.get("sha256")
        size = spec.get("size_bytes")
        media_type = spec.get("media_type")
        if (
            not isinstance(digest, str)
            or _DIGEST.fullmatch(digest) is None
            or type(size) is not int
            or not 0 <= size <= maximum
            or not isinstance(media_type, str)
            or not 1 <= len(media_type) <= 128
            or any(ord(character) < 32 for character in media_type)
        ):
            _fail("MOBILITY_SPEC")
        return {"sha256": digest, "size_bytes": size, "media_type": media_type}

    @staticmethod
    def _validate_command(command_id: object) -> str:
        if not isinstance(command_id, str) or _COMMAND_ID.fullmatch(command_id) is None:
            _fail("MOBILITY_ENVELOPE")
        return command_id

    @staticmethod
    def _validate_transfer_id(transfer_id: object) -> str:
        if not isinstance(transfer_id, str) or _TRANSFER_ID.fullmatch(transfer_id) is None:
            _fail("MOBILITY_ENVELOPE")
        return transfer_id

    @staticmethod
    def _validate_capability(capability: object) -> str:
        if not isinstance(capability, str) or _CAPABILITY.fullmatch(capability) is None:
            _fail("MOBILITY_ENVELOPE")
        return capability

    def _credential_headers(self, extra: dict[str, str]) -> dict[str, str]:
        """Resolve the bearer only after the transport has validated the leaf."""
        try:
            credential = self._credential_provider(self._credential_id)
        except Exception:
            _fail("MOBILITY_CREDENTIAL")
        if (
            not isinstance(credential, str)
            or not 32 <= len(credential) <= 512
            or any(ord(character) < 33 or ord(character) > 126 for character in credential)
            or "://" in credential
            or credential.startswith("//")
            or _URL_LIKE_CREDENTIAL.match(credential) is not None
        ):
            _fail("MOBILITY_CREDENTIAL")
        return {
            "Authorization": "Bearer " + credential,
            "Accept": "application/json",
            **extra,
        }

    @staticmethod
    def _raise_transport_error(error: PinnedHttpsClientError) -> None:
        """Map even an injected transport seam to one non-disclosing code."""
        args = getattr(error, "args", ())
        code = args[0] if len(args) == 1 and isinstance(args[0], str) else None
        _fail(code if code in _TRANSPORT_CODES else "MOBILITY_UNAVAILABLE")

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        headers: dict[str, str] | None = None,
        accepted_statuses: frozenset[int] = frozenset({200}),
    ) -> dict:
        if payload is None:
            body = None
        else:
            body = _canonical_json(payload)
            if len(body) > _CONTROL_RESPONSE_LIMIT:
                _fail("MOBILITY_PROTOCOL")
        extra = dict(headers or {})
        if payload is not None:
            extra["Content-Type"] = "application/json"
        try:
            response = self._transport.request(
                method,
                path,
                body=body,
                headers_supplier=lambda: self._credential_headers(extra),
                response_limit=_CONTROL_RESPONSE_LIMIT,
            )
        except PinnedHttpsClientError as error:
            self._raise_transport_error(error)
        if response.status not in accepted_statuses:
            self._raise_remote_error(response.body)
        try:
            value = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            _fail("MOBILITY_PROTOCOL")
        if not isinstance(value, dict):
            _fail("MOBILITY_PROTOCOL")
        return value

    @staticmethod
    def _raise_remote_error(body: bytes) -> None:
        """Expose only the three dispatch-relevant receiver admissions."""
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            _fail("MOBILITY_PEER_STATUS")
        error = value.get("error") if isinstance(value, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        if code in {"QUOTA", "CAPACITY", "FORBIDDEN"}:
            _fail("MOBILITY_" + code)
        _fail("MOBILITY_PEER_STATUS")

    @staticmethod
    def _valid_expiry(value: object) -> bool:
        if type(value) is int:
            return 0 <= value <= 2**63 - 1
        return (
            type(value) is float
            and math.isfinite(value)
            and 0 <= value <= 2**63 - 1
        )

    def _attestation(self, spec: dict) -> dict:
        response = self._request_json(
            "GET", "/v1/artifact-transfers/recipient-attestation"
        )
        if set(response) != _ATTESTATION_FIELDS:
            _fail("MOBILITY_ATTESTATION")
        supplied_digest = response.get("sha256")
        if not isinstance(supplied_digest, str) or _DIGEST.fullmatch(supplied_digest) is None:
            _fail("MOBILITY_ATTESTATION")
        fields = {name: response[name] for name in _ATTESTATION_FIELDS - {"sha256"}}
        try:
            canonical = recipient_mobility_attestation(fields)
        except TransferError:
            _fail("MOBILITY_ATTESTATION")
        if (
            not hmac.compare_digest(supplied_digest, canonical["sha256"])
            or not hmac.compare_digest(canonical["sha256"], self._attestation_pin)
            or canonical["authorized_source_owner_id"] != self._source_owner_id
            or canonical["can_write"] is not True
        ):
            _fail("MOBILITY_ATTESTATION")
        if spec["size_bytes"] > canonical["max_object_bytes"]:
            _fail("MOBILITY_LIMIT")
        return canonical

    def recipient_attestation(self, spec: dict) -> dict:
        """Fetch and validate a current recipient contract for one immutable spec."""
        immutable_spec = self._validate_spec(spec, maximum=self._max_object_bytes)
        return self._attestation(immutable_spec)

    @staticmethod
    def _same_json(left: object, right: object) -> bool:
        try:
            return hmac.compare_digest(_canonical_json(left), _canonical_json(right))
        except TransferError:
            return False

    def _validate_receipt(
        self,
        receipt: object,
        *,
        spec: dict,
        expected_transfer_id: str | None,
    ) -> dict:
        if not isinstance(receipt, dict):
            _fail("MOBILITY_ENVELOPE")
        state = receipt.get("state")
        expected_fields = (
            _SEALED_RECEIPT_FIELDS if state == "sealed" else _RECEIPT_FIELDS
        )
        if set(receipt) != expected_fields:
            _fail("MOBILITY_ENVELOPE")
        transfer_id = self._validate_transfer_id(receipt.get("transfer_id"))
        if expected_transfer_id is not None and transfer_id != expected_transfer_id:
            _fail("MOBILITY_ENVELOPE")
        offset = receipt.get("offset")
        chunk_bytes = receipt.get("chunk_bytes")
        revision = receipt.get("revision")
        expires_at = receipt.get("expires_at")
        if (
            not isinstance(state, str)
            or state not in _MOBILITY_STATES
            or type(offset) is not int
            or not 0 <= offset <= spec["size_bytes"]
            or type(chunk_bytes) is not int
            or not 65536 <= chunk_bytes <= _MAX_CHUNK_BYTES
            or type(revision) is not int
            or not 1 <= revision <= 2**63 - 1
            or not self._valid_expiry(expires_at)
        ):
            _fail("MOBILITY_ENVELOPE")
        if state == "sealed":
            artifact = receipt["artifact"]
            if (
                not isinstance(artifact, dict)
                or set(artifact) != {"artifact_id", "sha256", "size_bytes", "media_type"}
                or artifact["artifact_id"] != transfer_id
                or not self._same_json(
                    {name: artifact[name] for name in ("sha256", "size_bytes", "media_type")},
                    spec,
                )
            ):
                _fail("MOBILITY_ENVELOPE")
        return receipt

    def _validate_envelope(
        self,
        envelope: object,
        *,
        attestation: dict,
        spec: dict,
        command_id: str,
        expected_transfer_id: str | None = None,
    ) -> dict:
        if not isinstance(envelope, dict) or set(envelope) != _ENVELOPE_FIELDS:
            _fail("MOBILITY_ENVELOPE")
        if (
            envelope.get("protocol_version") != MOBILITY_V1_VERSION
            or envelope.get("command_id") != command_id
            or not self._same_json(envelope.get("recipient_attestation"), attestation)
            or not self._same_json(envelope.get("spec"), spec)
        ):
            _fail("MOBILITY_ENVELOPE")
        return {
            "protocol_version": MOBILITY_V1_VERSION,
            "recipient_attestation": attestation,
            "command_id": command_id,
            "spec": spec,
            "receipt": self._validate_receipt(
                envelope.get("receipt"),
                spec=spec,
                expected_transfer_id=expected_transfer_id,
            ),
        }

    @staticmethod
    def _mobility_headers(capability: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {
            "X-Sonder-Artifact-Mobility-Version": MOBILITY_V1_VERSION,
            "X-Sonder-Artifact-Mobility-Receipt-Capability": capability,
            **(extra or {}),
        }

    def begin(self, spec: dict, command_id: str, receipt_capability: str) -> dict:
        immutable_spec = self._validate_spec(spec, maximum=self._max_object_bytes)
        command = self._validate_command(command_id)
        capability = self._validate_capability(receipt_capability)
        attestation = self._attestation(immutable_spec)
        envelope = self._request_json(
            "POST",
            "/v1/artifact-transfers",
            payload={"spec": immutable_spec, "command_id": command},
            headers=self._mobility_headers(capability),
            accepted_statuses=frozenset({200, 202}),
        )
        return self._validate_envelope(
            envelope,
            attestation=attestation,
            spec=immutable_spec,
            command_id=command,
        )

    def inspect_receipt(
        self,
        transfer_id: str,
        command_id: str,
        spec: dict,
        receipt_capability: str,
    ) -> dict:
        identity = self._validate_transfer_id(transfer_id)
        command = self._validate_command(command_id)
        immutable_spec = self._validate_spec(spec, maximum=self._max_object_bytes)
        capability = self._validate_capability(receipt_capability)
        attestation = self._attestation(immutable_spec)
        envelope = self._request_json(
            "POST",
            "/v1/artifact-transfers/" + identity + "/mobility-receipt",
            payload={"command_id": command},
            headers=self._mobility_headers(capability),
            accepted_statuses=frozenset({200, 202}),
        )
        return self._validate_envelope(
            envelope,
            attestation=attestation,
            spec=immutable_spec,
            command_id=command,
            expected_transfer_id=identity,
        )

    def append(
        self,
        envelope: dict,
        immutable_spec: dict,
        body: bytes,
        receipt_capability: str,
    ) -> dict:
        """Append exactly the next validated chunk after a fresh attestation."""
        capability = self._validate_capability(receipt_capability)
        if not isinstance(envelope, dict) or set(envelope) != _ENVELOPE_FIELDS:
            _fail("MOBILITY_ENVELOPE")
        immutable_spec = self._validate_spec(
            immutable_spec, maximum=self._max_object_bytes
        )
        command = self._validate_command(envelope.get("command_id"))
        # Fetching this before deriving the write target makes an identity/grant
        # rotation fail without a chunk request or artifact byte on the wire.
        attestation = self._attestation(immutable_spec)
        checked = self._validate_envelope(
            envelope,
            attestation=attestation,
            spec=immutable_spec,
            command_id=command,
        )
        receipt = checked["receipt"]
        if receipt["state"] != "open":
            _fail("MOBILITY_ENVELOPE")
        offset = receipt["offset"]
        expected_length = min(
            receipt["chunk_bytes"], immutable_spec["size_bytes"] - offset
        )
        if (
            not isinstance(body, bytes)
            or expected_length <= 0
            or len(body) != expected_length
        ):
            _fail("MOBILITY_ENVELOPE")
        digest = hashlib.sha256(body).hexdigest()
        try:
            response = self._transport.request(
                "PUT",
                "/v1/artifact-transfers/"
                + receipt["transfer_id"]
                + "/chunks/"
                + str(offset),
                body=body,
                headers_supplier=lambda: self._credential_headers(
                    self._mobility_headers(
                        capability,
                        {
                            "Content-Type": "application/octet-stream",
                            "X-Sonder-Chunk-Sha256": digest,
                        },
                    )
                ),
                response_limit=_CONTROL_RESPONSE_LIMIT,
            )
        except PinnedHttpsClientError as error:
            self._raise_transport_error(error)
        if response.status != 200:
            self._raise_remote_error(response.body)
        try:
            ack = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            _fail("MOBILITY_PROTOCOL")
        if (
            not isinstance(ack, dict)
            or set(ack) != _ACK_FIELDS
            or ack.get("offset") != offset
            or ack.get("next_offset") != offset + len(body)
            or ack.get("chunk_sha256") != digest
            or type(ack.get("revision")) is not int
            or ack["revision"] != receipt["revision"] + 1
        ):
            _fail("MOBILITY_ENVELOPE")
        return ack

    def seal(
        self,
        envelope: dict,
        immutable_spec: dict,
        seal_command_id: str,
        receipt_capability: str,
    ) -> dict:
        capability = self._validate_capability(receipt_capability)
        if not isinstance(envelope, dict) or set(envelope) != _ENVELOPE_FIELDS:
            _fail("MOBILITY_ENVELOPE")
        immutable_spec = self._validate_spec(
            immutable_spec, maximum=self._max_object_bytes
        )
        command = self._validate_command(envelope.get("command_id"))
        seal_command = self._validate_command(seal_command_id)
        attestation = self._attestation(immutable_spec)
        checked = self._validate_envelope(
            envelope,
            attestation=attestation,
            spec=immutable_spec,
            command_id=command,
        )
        transfer_id = checked["receipt"]["transfer_id"]
        result = self._request_json(
            "POST",
            "/v1/artifact-transfers/" + transfer_id + "/seal",
            payload={"command_id": seal_command},
            headers=self._mobility_headers(capability),
            accepted_statuses=frozenset({200, 202}),
        )
        return self._validate_envelope(
            result,
            attestation=attestation,
            spec=immutable_spec,
            command_id=command,
            expected_transfer_id=transfer_id,
        )


__all__ = ["ConfiguredArtifactMobilityPeer"]
