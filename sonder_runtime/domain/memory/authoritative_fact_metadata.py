"""Validated, explicit metadata for a journaled fact and its derived indexes."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .replication import MemoryReplicationError


@dataclass(frozen=True)
class AuthoritativeFactMetadata:
    """Caller supplied entity and decision labels; never inferred from text."""

    entities: tuple[str, ...] = ()
    decision: dict[str, str] | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    supersedes: str | None = None
    provenance: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.entities) is not tuple or len(self.entities) > 16 or any(
            not isinstance(item, str) or not item.strip() or len(item) > 160
            for item in self.entities
        ):
            raise MemoryReplicationError("entity metadata must be bounded explicit identifiers")
        if len(set(self.entities)) != len(self.entities):
            raise MemoryReplicationError("entity metadata identifiers must be unique")
        if self.decision is not None:
            if type(self.decision) is not dict or set(self.decision) != {"id", "value"} or any(
                not isinstance(value, str) or not value.strip() or len(value) > 2048
                for value in self.decision.values()
            ):
                raise MemoryReplicationError("decision metadata must contain bounded id and value")
        for name in ("valid_from", "valid_until"):
            value = getattr(self, name)
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip() or len(value) > 64:
                raise MemoryReplicationError(f"{name} metadata is invalid")
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise MemoryReplicationError(f"{name} metadata is invalid") from exc
            if parsed.tzinfo is None:
                raise MemoryReplicationError(f"{name} must include a timezone")
            object.__setattr__(self, name, parsed.astimezone(timezone.utc).isoformat())
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            raise MemoryReplicationError("valid_until must follow valid_from")
        if self.supersedes is not None and (
            not isinstance(self.supersedes, str) or not self.supersedes.strip()
            or len(self.supersedes) > 160
        ):
            raise MemoryReplicationError("supersedes metadata is invalid")
        if type(self.provenance) is not tuple or len(self.provenance) > 16 or any(
            not isinstance(item, str) or not item.strip() or len(item) > 256
            for item in self.provenance
        ):
            raise MemoryReplicationError("provenance metadata is invalid")
        if (self.entities or self.decision) and not self.provenance:
            raise MemoryReplicationError("indexed facts require explicit provenance")

    @classmethod
    def from_payload(cls, value: object) -> "AuthoritativeFactMetadata":
        fields = {"entities", "decision", "valid_from", "valid_until", "supersedes", "provenance"}
        if type(value) is not dict or set(value) != fields:
            raise MemoryReplicationError("authoritative metadata fields are incomplete or unsupported")
        entities, provenance = value["entities"], value["provenance"]
        if not isinstance(entities, (list, tuple)) or not isinstance(provenance, (list, tuple)):
            raise MemoryReplicationError("authoritative metadata lists are invalid")
        return cls(
            entities=tuple(entities), decision=value["decision"],
            valid_from=value["valid_from"], valid_until=value["valid_until"],
            supersedes=value["supersedes"], provenance=tuple(provenance),
        )

    def as_payload(self) -> dict[str, object]:
        # The caller may still hold a mutable decision dict.  Validate again
        # and take an independent snapshot at the write boundary.
        self.__post_init__()
        return {
            "entities": self.entities,
            "decision": dict(self.decision) if self.decision is not None else None,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "supersedes": self.supersedes,
            "provenance": self.provenance,
        }


__all__ = ["AuthoritativeFactMetadata"]
