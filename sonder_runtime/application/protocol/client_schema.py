"""Runtime-derived client/SDK schema and reconnect parity contracts.

The application owns the portable shape of a client contract.  Interface
adapters (including Flutter) only serialize this shape and transport the
resume request; this module deliberately imports no SDK, provider, HTTP, or
Flutter package.

The schema is derived from a :class:`GeneratedCatalogs` bundle.  Its digest is
computed over the complete client and SDK projections plus the resumable
stream contract, so a client can fail closed when it has stale generated
metadata.  Reconnect planning delegates history semantics to
``ResumableStream`` and reports a snapshot requirement or schema refresh
explicitly instead of pretending that a partial replay is complete.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from .resumable_streams import ResumableStream, ResumeBatch, StreamGap


SCHEMA_NAME = "sonder-client-sdk-schema-v1"
DEFAULT_STREAM_CONTRACT: Mapping[str, Any] = {
    "kind": "snapshot-plus-events",
    "sequence": "strictly-increasing",
    "watermark": "opaque-monotonic-integer",
    "duplicate_policy": "event-id-deduplicated",
    "batch_limit": 256,
    "gap_policy": "request-snapshot",
}


class ClientSchemaError(ValueError):
    """Raised when a generated schema or parity request is invalid."""


class SchemaFreshness(str, Enum):
    CURRENT = "current"
    STALE = "stale"
    INVALID = "invalid"


class ResumeDisposition(str, Enum):
    RESUMED = "resumed"
    REFRESH_SCHEMA = "refresh_schema"
    REQUEST_SNAPSHOT = "request_snapshot"
    REJECTED = "rejected"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _plain(value: Any) -> Any:
    """Convert mappings/sequences to JSON-safe deterministic plain values."""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return value


def _require_digest(value: Any, label: str = "schema digest") -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ClientSchemaError(f"{label} must be a SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ClientSchemaError(f"{label} must be a SHA-256 hex digest") from exc
    return value.lower()


def _catalog_parts(catalogs: Any) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    """Read a CatalogBundle or equivalent mapping without coupling to it."""
    if hasattr(catalogs, "client"):
        parts = (catalogs.client, catalogs.mcp, catalogs.openai, catalogs.cli)
    elif isinstance(catalogs, Mapping):
        parts = tuple(catalogs.get(name, {}) for name in ("client", "mcp", "openai", "cli"))
    else:
        raise ClientSchemaError("catalogs must be a generated catalog bundle or mapping")
    if any(not isinstance(part, Mapping) for part in parts):
        raise ClientSchemaError("catalog projections must be objects")
    return parts  # type: ignore[return-value]


@dataclass(frozen=True)
class ClientSchema:
    """Immutable, portable schema advertised to clients and SDKs."""

    schema: str
    schema_version: str
    source_catalog_digest: str
    client: Mapping[str, Any]
    sdk: Mapping[str, Any]
    stream: Mapping[str, Any]
    digest: str

    def __post_init__(self) -> None:
        if self.schema != SCHEMA_NAME:
            raise ClientSchemaError(f"unsupported client schema: {self.schema!r}")
        _require_digest(self.source_catalog_digest, "source catalog digest")
        _require_digest(self.digest)
        if not self.schema_version:
            raise ClientSchemaError("schema_version is required")
        if not isinstance(self.client, Mapping) or not isinstance(self.sdk, Mapping):
            raise ClientSchemaError("client and sdk projections must be objects")
        if not isinstance(self.stream, Mapping):
            raise ClientSchemaError("stream contract must be an object")

    def unsigned(self) -> dict[str, Any]:
        return {
            "client": _plain(self.client),
            "schema": self.schema,
            "schema_version": self.schema_version,
            "sdk": _plain(self.sdk),
            "source_catalog_digest": self.source_catalog_digest,
            "stream": _plain(self.stream),
        }

    def as_dict(self) -> dict[str, Any]:
        result = self.unsigned()
        result["digest"] = self.digest
        return result

    def freshness(self, advertised_digest: str | None) -> "FreshnessResult":
        return check_schema_freshness(advertised_digest, self)


@dataclass(frozen=True)
class FreshnessResult:
    state: SchemaFreshness
    expected_digest: str
    advertised_digest: str | None
    reason: str

    @property
    def current(self) -> bool:
        return self.state is SchemaFreshness.CURRENT


def build_client_schema(
    catalogs: Any,
    *,
    stream_contract: Mapping[str, Any] | None = None,
) -> ClientSchema:
    """Build the client and SDK projections from runtime-generated catalogs."""
    client, mcp, openai, cli = _catalog_parts(catalogs)
    source_digest = getattr(catalogs, "digest", None)
    if source_digest is None and isinstance(catalogs, Mapping):
        source_digest = catalogs.get("digest")
    if source_digest is None:
        # Equivalent mappings may omit the convenience digest; derive the
        # source identity from all projections rather than trusting a client.
        source_digest = _digest({"client": client, "cli": cli, "mcp": mcp, "openai": openai})
    source_digest = _require_digest(source_digest, "source catalog digest")
    client_projection = _plain(client)
    sdk_projection = {
        "commands": _plain(cli.get("commands", ())),
        "events": _plain(client.get("events", ())),
        "mcp": _plain(mcp),
        "openai": _plain(openai),
        "tools": _plain(client.get("tools", ())),
    }
    stream = dict(DEFAULT_STREAM_CONTRACT)
    if stream_contract is not None:
        if not isinstance(stream_contract, Mapping):
            raise ClientSchemaError("stream_contract must be an object")
        stream.update(_plain(stream_contract))
    unsigned = {
        "client": client_projection,
        "schema": SCHEMA_NAME,
        "schema_version": str(client.get("schema_version", "1")),
        "sdk": sdk_projection,
        "source_catalog_digest": source_digest,
        "stream": stream,
    }
    return ClientSchema(
        schema=SCHEMA_NAME,
        schema_version=unsigned["schema_version"],
        source_catalog_digest=source_digest,
        client=client_projection,
        sdk=sdk_projection,
        stream=stream,
        digest=_digest(unsigned),
    )


def check_schema_freshness(advertised_digest: str | None, schema: ClientSchema) -> FreshnessResult:
    """Compare a client digest without accepting malformed values as current."""
    if advertised_digest is None:
        return FreshnessResult(SchemaFreshness.STALE, schema.digest, None, "client did not advertise a schema digest")
    try:
        normalized = _require_digest(advertised_digest)
    except ClientSchemaError:
        return FreshnessResult(SchemaFreshness.INVALID, schema.digest, str(advertised_digest), "client advertised an invalid schema digest")
    if normalized != schema.digest:
        return FreshnessResult(SchemaFreshness.STALE, schema.digest, normalized, "client schema digest differs from runtime")
    return FreshnessResult(SchemaFreshness.CURRENT, schema.digest, normalized, "schema digest matches runtime")


@dataclass(frozen=True)
class ResumeCursor:
    stream_id: str
    watermark: int

    def __post_init__(self) -> None:
        if not self.stream_id or isinstance(self.watermark, bool) or self.watermark < 0:
            raise ClientSchemaError("resume cursor requires a stream id and non-negative watermark")


@dataclass(frozen=True)
class ReconnectRequest:
    client_id: str
    schema_digest: str | None = None
    cursors: tuple[ResumeCursor, ...] = ()
    batch_limit: int = 256

    def __post_init__(self) -> None:
        if not self.client_id.strip():
            raise ClientSchemaError("client_id is required")
        if self.batch_limit < 1 or self.batch_limit > 256:
            raise ClientSchemaError("batch_limit must be between 1 and 256")
        if len({cursor.stream_id for cursor in self.cursors}) != len(self.cursors):
            raise ClientSchemaError("reconnect cursors must contain one entry per stream")


@dataclass(frozen=True)
class ResumeResult:
    stream_id: str
    disposition: ResumeDisposition
    batch: ResumeBatch | None = None
    reason: str = ""


@dataclass(frozen=True)
class ReconnectResponse:
    freshness: FreshnessResult
    results: tuple[ResumeResult, ...]

    @property
    def resumed(self) -> bool:
        return self.freshness.current and all(
            result.disposition is ResumeDisposition.RESUMED for result in self.results
        )


class ClientParityContract:
    """Provider-neutral reconnect planner for mobile, desktop, and SDK clients."""

    def __init__(self, schema: ClientSchema, streams: Mapping[str, ResumableStream]) -> None:
        if not isinstance(schema, ClientSchema):
            raise TypeError("schema must be a ClientSchema")
        if any(key != stream.stream_id for key, stream in streams.items()):
            raise ClientSchemaError("stream mapping keys must match stream ids")
        self.schema = schema
        self._streams = dict(streams)

    def reconnect(self, request: ReconnectRequest) -> ReconnectResponse:
        freshness = self.schema.freshness(request.schema_digest)
        if not freshness.current:
            return ReconnectResponse(
                freshness,
                tuple(ResumeResult(cursor.stream_id, ResumeDisposition.REFRESH_SCHEMA,
                                   reason=freshness.reason) for cursor in request.cursors),
            )
        results = []
        for cursor in request.cursors:
            stream = self._streams.get(cursor.stream_id)
            if stream is None:
                results.append(ResumeResult(cursor.stream_id, ResumeDisposition.REJECTED,
                                            reason="unknown stream"))
                continue
            try:
                batch = stream.resume(cursor.watermark, limit=request.batch_limit)
            except StreamGap:
                results.append(ResumeResult(cursor.stream_id, ResumeDisposition.REQUEST_SNAPSHOT,
                                            reason="watermark predates retained history"))
            except ValueError as exc:
                results.append(ResumeResult(cursor.stream_id, ResumeDisposition.REJECTED,
                                            reason=str(exc)))
            else:
                results.append(ResumeResult(cursor.stream_id, ResumeDisposition.RESUMED, batch=batch))
        return ReconnectResponse(freshness, tuple(results))


__all__ = [
    "ClientParityContract", "ClientSchema", "ClientSchemaError", "DEFAULT_STREAM_CONTRACT",
    "FreshnessResult", "ReconnectRequest", "ReconnectResponse", "ResumeCursor", "ResumeDisposition",
    "ResumeResult", "SCHEMA_NAME", "SchemaFreshness", "build_client_schema", "check_schema_freshness",
]
