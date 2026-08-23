"""Stable, JSON-safe request/result contracts for the public Python SDK.

These DTOs deliberately contain no caller-controlled permission fields.  A host
adapter resolves identity, scope, approval, and permission policy before a
request reaches the runtime tool gateway.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any, Mapping

from ...application.errors import SonderError


SDK_PROTOCOL_VERSION = "1.0"
SUPPORTED_SDK_PROTOCOL_VERSIONS = (SDK_PROTOCOL_VERSION,)
MAX_REQUEST_BYTES = 64_000
MAX_RESULT_BYTES = 1_024_000
_DIGEST = re.compile(r"^[0-9a-fA-F]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class SdkContractError(ValueError):
    """A malformed or incompatible public SDK contract."""

    code = "SDK_CONTRACT_INVALID"


def _json_size(value: Any, *, label: str, limit: int) -> int:
    try:
        encoded = json.dumps(
            value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SdkContractError(f"{label} must be JSON serializable") from exc
    if len(encoded) > limit:
        raise SdkContractError(f"{label} exceeds {limit} bytes")
    return len(encoded)


def _keys(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unexpected = sorted(set(value) - allowed)
    if unexpected:
        raise SdkContractError(
            f"{label} contains unknown field(s): {', '.join(unexpected)}"
        )


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise SdkContractError(f"{label} must be a SHA-256 hex digest")
    return value.lower()


@dataclass(frozen=True, slots=True)
class SdkDiagnostic:
    """Machine-readable validation or compatibility diagnostic."""

    code: str
    message: str
    path: str = ""

    def __post_init__(self) -> None:
        if (
            not isinstance(self.code, str)
            or not _IDENTIFIER.fullmatch(self.code)
            or not isinstance(self.message, str)
            or not self.message.strip()
            or not isinstance(self.path, str)
        ):
            raise SdkContractError("diagnostic code and message are required")

    def as_dict(self) -> dict[str, str]:
        result = {"code": self.code, "message": self.message}
        if self.path:
            result["path"] = self.path
        return result


@dataclass(frozen=True, slots=True)
class SdkError:
    """Stable error envelope shared by in-process and remote SDK transports."""

    code: str
    message: str
    retryable: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.code, str)
            or not _IDENTIFIER.fullmatch(self.code)
            or not isinstance(self.message, str)
            or not self.message.strip()
        ):
            raise SdkContractError("SDK error code and message are required")
        if not isinstance(self.retryable, bool) or not isinstance(self.details, Mapping):
            raise SdkContractError("SDK error retryable/details fields are invalid")
        _json_size(dict(self.details), label="SDK error details", limit=MAX_REQUEST_BYTES)

    @classmethod
    def from_exception(cls, error: Exception) -> "SdkError":
        """Map known runtime errors without leaking unexpected exception text."""
        if isinstance(error, SdkContractError):
            return cls(code=error.code, message=str(error), retryable=False)
        if isinstance(error, SonderError):
            return cls(
                code=str(error.code),
                message=str(error) or error.code.replace("_", " ").lower(),
                retryable=bool(error.retryable),
            )
        return cls(
            code="INTERNAL_FAILURE",
            message="the SDK request failed inside the runtime",
            retryable=False,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SdkError":
        if not isinstance(value, Mapping):
            raise SdkContractError("SDK error must be an object")
        _keys(value, {"code", "message", "retryable", "details"}, "SDK error")
        details = value.get("details", {})
        if not isinstance(details, Mapping):
            raise SdkContractError("SDK error details must be an object")
        return cls(
            code=value.get("code", ""),
            message=value.get("message", ""),
            retryable=value.get("retryable", False),
            details=dict(details),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "details": dict(self.details),
            "message": self.message,
            "retryable": self.retryable,
        }


@dataclass(frozen=True, slots=True)
class SdkRequest:
    """One versioned tool request bound to a discovered catalog digest."""

    request_id: str
    tool: str
    arguments: Mapping[str, Any]
    catalog_digest: str
    version: str = SDK_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or self.version not in SUPPORTED_SDK_PROTOCOL_VERSIONS:
            raise SdkContractError(f"unsupported SDK protocol version: {self.version!r}")
        if not isinstance(self.request_id, str) or not _IDENTIFIER.fullmatch(self.request_id):
            raise SdkContractError("request_id must be a bounded identifier")
        if not isinstance(self.tool, str) or not _IDENTIFIER.fullmatch(self.tool):
            raise SdkContractError("tool must be a bounded identifier")
        if not isinstance(self.arguments, Mapping):
            raise SdkContractError("arguments must be an object")
        object.__setattr__(self, "catalog_digest", _digest(self.catalog_digest, "catalog_digest"))
        _json_size(self.as_dict(), label="SDK request", limit=MAX_REQUEST_BYTES)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SdkRequest":
        if not isinstance(value, Mapping):
            raise SdkContractError("SDK request must be an object")
        _keys(
            value,
            {"arguments", "catalog_digest", "request_id", "tool", "version"},
            "SDK request",
        )
        arguments = value.get("arguments", {})
        if not isinstance(arguments, Mapping):
            raise SdkContractError("SDK request arguments must be an object")
        return cls(
            request_id=value.get("request_id", ""),
            tool=value.get("tool", ""),
            arguments=dict(arguments),
            catalog_digest=value.get("catalog_digest", ""),
            version=value.get("version", ""),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "arguments": dict(self.arguments),
            "catalog_digest": self.catalog_digest,
            "request_id": self.request_id,
            "tool": self.tool,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class SdkResult:
    """JSON-safe success or structured failure for exactly one request."""

    request_id: str
    ok: bool
    output: Any = None
    error: SdkError | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    version: str = SDK_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or self.version not in SUPPORTED_SDK_PROTOCOL_VERSIONS:
            raise SdkContractError(f"unsupported SDK protocol version: {self.version!r}")
        if not isinstance(self.request_id, str) or not _IDENTIFIER.fullmatch(self.request_id):
            raise SdkContractError("result request_id must be a bounded identifier")
        if not isinstance(self.ok, bool) or not isinstance(self.metadata, Mapping):
            raise SdkContractError("result ok/metadata fields are invalid")
        if self.ok == (self.error is not None):
            raise SdkContractError("successful results cannot contain errors and failures must")
        _json_size(self.as_dict(), label="SDK result", limit=MAX_RESULT_BYTES)

    @classmethod
    def success(
        cls, request_id: str, output: Any, *, metadata: Mapping[str, Any] | None = None
    ) -> "SdkResult":
        return cls(request_id, True, output, metadata=metadata or {})

    @classmethod
    def failure(
        cls, request_id: str, error: SdkError, *, metadata: Mapping[str, Any] | None = None
    ) -> "SdkResult":
        return cls(request_id, False, error=error, metadata=metadata or {})

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SdkResult":
        if not isinstance(value, Mapping):
            raise SdkContractError("SDK result must be an object")
        _keys(value, {"error", "metadata", "ok", "output", "request_id", "version"}, "SDK result")
        raw_error = value.get("error")
        metadata = value.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise SdkContractError("SDK result metadata must be an object")
        return cls(
            request_id=value.get("request_id", ""),
            ok=value.get("ok"),
            output=value.get("output"),
            error=SdkError.from_dict(raw_error) if raw_error is not None else None,
            metadata=dict(metadata),
            version=value.get("version", ""),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": self.error.as_dict() if self.error is not None else None,
            "metadata": dict(self.metadata),
            "ok": self.ok,
            "output": self.output,
            "request_id": self.request_id,
            "version": self.version,
        }


__all__ = [
    "MAX_REQUEST_BYTES", "MAX_RESULT_BYTES", "SDK_PROTOCOL_VERSION",
    "SUPPORTED_SDK_PROTOCOL_VERSIONS", "SdkContractError", "SdkDiagnostic",
    "SdkError", "SdkRequest", "SdkResult",
]
