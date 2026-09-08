"""Bounded coordinator for explicitly configured authoritative memory replicas.

The coordinator composes an authoritative journal with injected sinks.  It
does not discover peers, elect an owner, exchange data over a network, or
claim high availability.  A replica counts only when it returns a receipt
whose source, epoch, cursor, digest, and durability flag exactly match the
transferred batch.
"""
from __future__ import annotations

from dataclasses import dataclass
import hmac
import json
import re
from typing import Protocol, Sequence, runtime_checkable

from ...domain.memory.replication import (
    MemoryReplicationBatch,
    MemoryReplicationError,
    MemoryReplicaReceipt,
)


_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_REPLICAS = 64
_MAX_BATCH_RECORDS = 1024


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise MemoryReplicationError(f"{field} must be a bounded stable identity")
    return value


def _non_negative(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MemoryReplicationError(f"{field} must be a non-negative integer")
    return value


def _positive(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise MemoryReplicationError(f"{field} must be a positive integer")
    return value


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise MemoryReplicationError(f"{field} must be a SHA-256 digest")
    return value


@runtime_checkable
class MemoryReplicationSink(Protocol):
    """One explicitly configured durable replica sink."""

    identity: str

    def apply(self, batch: MemoryReplicationBatch) -> MemoryReplicaReceipt:
        """Apply a batch and return evidence of durable persistence."""


class SQLiteMemoryReplicationSink:
    """Adapt one SQLite journal to the coordinator's receipt contract."""

    def __init__(self, identity: str, journal) -> None:
        self.identity = _identity(identity, "replica identity")
        if not hasattr(journal, "apply") or not hasattr(journal, "source_id"):
            raise TypeError("journal must provide source_id and apply(batch)")
        if journal.source_id != self.identity:
            raise MemoryReplicationError(
                "replica identity must match the journal source identity"
            )
        self.journal = journal

    def apply(self, batch: MemoryReplicationBatch) -> MemoryReplicaReceipt:
        if not isinstance(batch, MemoryReplicationBatch):
            raise TypeError("memory replication batch is required")
        inserted = self.journal.apply(batch)
        return MemoryReplicaReceipt(
            replica_id=self.identity,
            source_id=batch.source_id,
            source_epoch=batch.source_epoch,
            next_sequence=batch.next_sequence,
            batch_digest=batch.digest,
            durable=True,
            inserted_records=inserted,
        )


@dataclass(frozen=True, slots=True)
class MemoryReplicationOutcome:
    """Auditable result for one bounded replication attempt."""

    source_id: str
    source_epoch: int
    after_sequence: int
    next_sequence: int
    batch_digest: str
    status: str
    inserted_records: int
    replica_ids: tuple[str, ...]
    failed_replica_ids: tuple[str, ...]
    failure_reasons: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _identity(self.source_id, "source_id"))
        object.__setattr__(self, "source_epoch", _positive(self.source_epoch, "source_epoch"))
        object.__setattr__(self, "after_sequence", _non_negative(self.after_sequence, "after_sequence"))
        object.__setattr__(self, "next_sequence", _non_negative(self.next_sequence, "next_sequence"))
        object.__setattr__(self, "batch_digest", _digest(self.batch_digest, "batch_digest"))
        if self.status not in {"empty", "replicated", "pending"}:
            raise MemoryReplicationError("replication status is unsupported")
        object.__setattr__(self, "inserted_records", _non_negative(self.inserted_records, "inserted_records"))
        for field in ("replica_ids", "failed_replica_ids", "failure_reasons"):
            if type(getattr(self, field)) is not tuple:
                raise MemoryReplicationError(f"{field} must be a tuple")
        for replica_id in self.replica_ids:
            _identity(replica_id, "replica identity")
        for replica_id in self.failed_replica_ids:
            _identity(replica_id, "failed replica identity")
        for replica_id, reason in self.failure_reasons:
            _identity(replica_id, "failed replica identity")
            _identity(reason, "failure reason")
        if not self.replica_ids or self.replica_ids[0] != self.source_id:
            raise MemoryReplicationError("source must lead replica evidence")
        if len(set(self.replica_ids)) != len(self.replica_ids):
            raise MemoryReplicationError("durable replica identities must be unique")
        if len(set(self.failed_replica_ids)) != len(self.failed_replica_ids):
            raise MemoryReplicationError("failed replica identities must be unique")
        if tuple(replica_id for replica_id, _reason in self.failure_reasons) != self.failed_replica_ids:
            raise MemoryReplicationError("failure reasons must cover failed replicas in order")
        if set(self.replica_ids) & set(self.failed_replica_ids):
            raise MemoryReplicationError("replica cannot be both durable and failed")


class MemoryReplicationCoordinator:
    """Replicate one exported page to explicitly injected replica sinks.

    ``minimum_data_replicas`` includes the authoritative source itself.  A
    source with no records returns ``empty`` and never treats an empty export
    as a peer acknowledgement.
    """

    def __init__(
        self,
        source,
        sinks: Sequence[MemoryReplicationSink],
        *,
        minimum_data_replicas: int = 2,
        limit: int = 256,
        project: str | None = None,
    ) -> None:
        if not hasattr(source, "source_id") or not callable(getattr(source, "export", None)):
            raise TypeError("source must provide source_id and export(...)" )
        self.source = source
        self.source_id = _identity(source.source_id, "source_id")
        if type(sinks) is not tuple:
            raise TypeError("sinks must be an explicit tuple")
        if not 0 <= len(sinks) <= _MAX_REPLICAS:
            raise MemoryReplicationError(f"sinks must contain 0..{_MAX_REPLICAS} replicas")
        identities: list[str] = []
        for sink in sinks:
            identity = _identity(getattr(sink, "identity", None), "replica identity")
            if identity == self.source_id:
                raise MemoryReplicationError("source cannot also be a replica sink")
            if identity in identities:
                raise MemoryReplicationError("replica identities must be unique")
            if not callable(getattr(sink, "apply", None)):
                raise TypeError("replica sink must provide apply(batch)")
            identities.append(identity)
        if isinstance(minimum_data_replicas, bool) or not isinstance(minimum_data_replicas, int):
            raise MemoryReplicationError("minimum_data_replicas must be an integer")
        if not 1 <= minimum_data_replicas <= len(sinks) + 1:
            raise MemoryReplicationError("minimum_data_replicas exceeds configured replicas")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_BATCH_RECORDS:
            raise MemoryReplicationError(f"limit must be within 1..{_MAX_BATCH_RECORDS}")
        if project is not None and (not isinstance(project, str) or not project):
            raise MemoryReplicationError("project must be a non-empty scope")
        self.sinks = sinks
        self._sink_identities = tuple(identities)
        self.minimum_data_replicas = minimum_data_replicas
        self.limit = limit
        self.project = project

    def replicate(self, *, after_sequence: int = 0) -> MemoryReplicationOutcome:
        after_sequence = _non_negative(after_sequence, "after_sequence")
        export_kwargs = {"after_sequence": after_sequence, "limit": self.limit}
        if self.project is not None:
            export_kwargs["project"] = self.project
        batch = self.source.export(**export_kwargs)
        if not isinstance(batch, MemoryReplicationBatch):
            raise TypeError("source export must return MemoryReplicationBatch")
        if batch.source_id != self.source_id:
            raise MemoryReplicationError("source export identity does not match source")
        if batch.after_sequence != after_sequence:
            raise MemoryReplicationError("source export cursor does not match request")
        if len(batch.records) > self.limit:
            raise MemoryReplicationError("source export exceeds coordinator batch limit")
        if not batch.records:
            return MemoryReplicationOutcome(
                source_id=batch.source_id,
                source_epoch=batch.source_epoch,
                after_sequence=batch.after_sequence,
                next_sequence=batch.next_sequence,
                batch_digest=batch.digest,
                status="empty",
                inserted_records=0,
                replica_ids=(batch.source_id,),
                failed_replica_ids=(),
                failure_reasons=(),
            )

        durable = [batch.source_id]
        failed: list[str] = []
        reasons: list[tuple[str, str]] = []
        inserted_records = 0
        for expected_identity, sink in zip(self._sink_identities, self.sinks):
            identity = expected_identity
            if getattr(sink, "identity", None) != expected_identity:
                failed.append(identity)
                reasons.append((identity, "sink_identity_changed"))
                continue
            try:
                receipt = sink.apply(batch)
            except Exception:
                failed.append(identity)
                reasons.append((identity, "sink_failure"))
                continue
            reason = self._receipt_failure(batch, identity, receipt)
            if reason is not None:
                failed.append(identity)
                reasons.append((identity, reason))
                continue
            durable.append(identity)
            inserted_records += receipt.inserted_records

        status = "replicated" if len(durable) >= self.minimum_data_replicas else "pending"
        return MemoryReplicationOutcome(
            source_id=batch.source_id,
            source_epoch=batch.source_epoch,
            after_sequence=batch.after_sequence,
            next_sequence=batch.next_sequence,
            batch_digest=batch.digest,
            status=status,
            inserted_records=inserted_records,
            replica_ids=tuple(durable),
            failed_replica_ids=tuple(failed),
            failure_reasons=tuple(reasons),
        )

    @staticmethod
    def _receipt_failure(
        batch: MemoryReplicationBatch,
        identity: str,
        receipt: object,
    ) -> str | None:
        if not isinstance(receipt, MemoryReplicaReceipt):
            return "invalid_receipt"
        if receipt.replica_id != identity:
            return "receipt_identity_mismatch"
        if receipt.source_id != batch.source_id:
            return "receipt_source_mismatch"
        if receipt.source_epoch != batch.source_epoch:
            return "receipt_epoch_mismatch"
        if receipt.next_sequence != batch.next_sequence:
            return "receipt_sequence_mismatch"
        if receipt.batch_digest != batch.digest:
            return "receipt_digest_mismatch"
        if receipt.durable is not True:
            return "receipt_not_durable"
        if receipt.inserted_records > len(batch.records):
            return "receipt_inserted_count_mismatch"
        return None


class MemoryReplicationReceiver:
    """Authenticate and apply one bounded batch on an explicit peer.

    The receiver is an application boundary so HTTP adapters never need to
    import domain records or transport libraries.  Peer admission is explicit:
    only configured source identities can write to the injected sink.
    """

    def __init__(
        self,
        sink: MemoryReplicationSink,
        *,
        api_key: str,
        accepted_source_ids: tuple[str, ...],
        max_body_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        if not callable(getattr(sink, "apply", None)):
            raise TypeError("memory replication sink must provide apply(batch)")
        self.sink = sink
        self.identity = _identity(getattr(sink, "identity", None), "replica identity")
        if not isinstance(api_key, str) or not 1 <= len(api_key) <= 512:
            raise ValueError("memory replication API key must be 1..512 characters")
        if any(ord(char) < 0x21 or ord(char) > 0x7E for char in api_key):
            raise ValueError("memory replication API key must contain printable ASCII")
        self._api_key = api_key
        if type(accepted_source_ids) is not tuple or not 1 <= len(accepted_source_ids) <= _MAX_REPLICAS:
            raise ValueError(
                f"accepted_source_ids must contain 1..{_MAX_REPLICAS} identities"
            )
        normalized = tuple(
            _identity(item, "accepted source identity")
            for item in accepted_source_ids
        )
        if len(set(normalized)) != len(normalized):
            raise ValueError("accepted_source_ids must not contain duplicates")
        if self.identity in normalized:
            raise ValueError("receiver identity cannot be an accepted source")
        if type(max_body_bytes) is not int or not 1024 <= max_body_bytes <= 64 * 1024 * 1024:
            raise ValueError(
                "memory replication body bound must be within 1024..67108864 bytes"
            )
        self.accepted_source_ids = normalized
        self.max_body_bytes = max_body_bytes

    def receive(self, authorization: str, payload: object) -> MemoryReplicaReceipt:
        expected = "Bearer " + self._api_key
        if (
            not isinstance(authorization, str)
            or len(authorization) > 520
            or not hmac.compare_digest(authorization, expected)
        ):
            raise PermissionError("memory replication authentication is required")
        if not isinstance(payload, dict) or set(payload) != {"object", "batch"}:
            raise MemoryReplicationError("memory replication request envelope is invalid")
        if payload.get("object") != "memory_replication_batch":
            raise MemoryReplicationError("memory replication request envelope is invalid")
        try:
            batch = MemoryReplicationBatch.from_dict(payload["batch"])
        except (MemoryReplicationError, TypeError, ValueError) as exc:
            raise MemoryReplicationError(str(exc)) from exc
        if batch.source_id not in self.accepted_source_ids:
            raise PermissionError("memory replication source is not accepted")
        try:
            receipt = self.sink.apply(batch)
        except (MemoryReplicationError, PermissionError):
            raise
        except Exception as exc:
            raise RuntimeError("memory replication sink is unavailable") from exc
        if not isinstance(receipt, MemoryReplicaReceipt):
            raise MemoryReplicationError("memory replication sink returned an invalid receipt")
        if (
            receipt.replica_id != self.identity
            or receipt.source_id != batch.source_id
            or receipt.source_epoch != batch.source_epoch
            or receipt.next_sequence != batch.next_sequence
            or receipt.batch_digest != batch.digest
            or receipt.durable is not True
            or receipt.inserted_records > len(batch.records)
        ):
            raise MemoryReplicationError(
                "memory replication sink receipt does not match the batch"
            )
        return receipt

    def receive_bytes(self, authorization: str, body: bytes) -> MemoryReplicaReceipt:
        if not isinstance(body, bytes) or len(body) > self.max_body_bytes:
            raise MemoryReplicationError("memory replication request exceeds the body bound")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MemoryReplicationError("memory replication request is not valid JSON") from exc
        return self.receive(authorization, payload)


__all__ = [
    "MemoryReplicationCoordinator",
    "MemoryReplicationOutcome",
    "MemoryReplicationReceiver",
    "MemoryReplicationSink",
    "SQLiteMemoryReplicationSink",
]
