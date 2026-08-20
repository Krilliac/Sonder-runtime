"""Trust and provenance boundaries for retrieved prompt material.

Retrieved memory, web pages, and tool results are data, not policy.  This
module gives each item an immutable provenance record and carries that record
through context assembly and durable replay.  It intentionally does not
interpret instructions found in untrusted content or mutate a memory/policy
store.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from typing import Iterable, Mapping, Sequence


MAX_CONTENT_LENGTH = 1_000_000
MAX_ITEMS_PER_CONTEXT = 256
MAX_PROVENANCE_DEPTH = 16


class ProvenanceError(ValueError):
    """Raised when provenance is missing, malformed, or tampered with."""


class SourceKind(str, Enum):
    RETRIEVED_MEMORY = "retrieved_memory"
    TOOL_RESULT = "tool_result"
    WEB_RESULT = "web_result"


class TrustLabel(str, Enum):
    UNTRUSTED = "untrusted"
    USER_CONFIRMED = "user_confirmed"
    INDEPENDENTLY_VERIFIED = "independently_verified"


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProvenanceError(f"{name} must not be empty")
    return value.strip()


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class Provenance:
    """Immutable origin metadata retained with every prompt item."""

    source_kind: SourceKind
    source_id: str
    origin: str
    content_digest: str
    trust: TrustLabel = TrustLabel.UNTRUSTED
    parent_ids: tuple[str, ...] = ()
    observed_at: str = ""

    def __post_init__(self) -> None:
        _text(self.source_id, "source_id")
        _text(self.origin, "origin")
        if len(self.content_digest) != 64 or any(c not in "0123456789abcdef" for c in self.content_digest):
            raise ProvenanceError("content_digest must be a SHA-256 hex digest")
        if len(self.parent_ids) > MAX_PROVENANCE_DEPTH:
            raise ProvenanceError("provenance chain is too deep")
        if not self.observed_at:
            object.__setattr__(self, "observed_at", datetime.now(timezone.utc).isoformat())

    def as_dict(self) -> dict[str, object]:
        return {
            "source_kind": self.source_kind.value,
            "source_id": self.source_id,
            "origin": self.origin,
            "content_digest": self.content_digest,
            "trust": self.trust.value,
            "parent_ids": list(self.parent_ids),
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class PromptItem:
    """Prompt-visible data fenced by its provenance label."""

    content: str
    provenance: Provenance

    def __post_init__(self) -> None:
        if not isinstance(self.content, str) or len(self.content) > MAX_CONTENT_LENGTH:
            raise ProvenanceError("content exceeds the provenance boundary")
        if _digest(self.content) != self.provenance.content_digest:
            raise ProvenanceError("content does not match provenance digest")

    @property
    def is_untrusted(self) -> bool:
        return self.provenance.trust is TrustLabel.UNTRUSTED

    def as_dict(self) -> dict[str, object]:
        return {"content": self.content, "provenance": self.provenance.as_dict()}


@dataclass(frozen=True)
class PromotionDecision:
    allowed: bool
    reasons: tuple[str, ...]
    source_digest: str


@dataclass(frozen=True)
class ContextPacket:
    """Deterministic context plus all source provenance needed for replay."""

    items: tuple[PromptItem, ...]
    packet_digest: str

    @classmethod
    def create(cls, items: Sequence[PromptItem]) -> "ContextPacket":
        values = tuple(items)
        if len(values) > MAX_ITEMS_PER_CONTEXT:
            raise ProvenanceError("context contains too many provenance items")
        payload = [item.as_dict() for item in values]
        return cls(values, _digest(_canonical(payload)))

    def as_dict(self) -> dict[str, object]:
        return {"items": [item.as_dict() for item in self.items], "packet_digest": self.packet_digest}

    def to_json(self) -> str:
        return _canonical(self.as_dict())


class PromptProvenanceBoundary:
    """Construct, validate, and replay trust-labelled prompt material."""

    def ingest(
        self,
        source_kind: SourceKind | str,
        source_id: str,
        content: str,
        *,
        origin: str,
        parent_ids: Iterable[str] = (),
        observed_at: str | None = None,
    ) -> PromptItem:
        try:
            kind = source_kind if isinstance(source_kind, SourceKind) else SourceKind(source_kind)
        except ValueError as exc:
            raise ProvenanceError("unknown prompt source kind") from exc
        content = _text(content, "content")
        parents = tuple(dict.fromkeys(_text(parent, "parent_id") for parent in parent_ids))
        provenance = Provenance(kind, source_id, origin, _digest(content), TrustLabel.UNTRUSTED, parents, observed_at or "")
        return PromptItem(content, provenance)

    def evaluate_promotion(
        self,
        item: PromptItem,
        *,
        explicit_confirmation: bool = False,
        independent_evidence: Sequence[str] = (),
    ) -> PromotionDecision:
        """Require an explicit, auditable handoff before memory/policy promotion."""
        reasons: list[str] = []
        if item.is_untrusted and not explicit_confirmation:
            reasons.append("untrusted content requires explicit confirmation")
        if not independent_evidence:
            reasons.append("independent evidence is required")
        if any(not isinstance(value, str) or not value.strip() for value in independent_evidence):
            reasons.append("independent evidence identifiers must be non-empty")
        return PromotionDecision(not reasons, tuple(reasons), item.provenance.content_digest)

    def assemble_context(self, items: Sequence[PromptItem]) -> ContextPacket:
        return ContextPacket.create(items)

    def replay_context(self, serialized: str | Mapping[str, object]) -> ContextPacket:
        try:
            raw = json.loads(serialized) if isinstance(serialized, str) else dict(serialized)
            raw_items = raw["items"]
            packet_digest = raw["packet_digest"]
            if not isinstance(raw_items, list) or not isinstance(packet_digest, str):
                raise TypeError
            items = tuple(self._item_from_dict(value) for value in raw_items)
            packet = ContextPacket.create(items)
            if packet.packet_digest != packet_digest:
                raise ProvenanceError("context provenance digest mismatch")
            return packet
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ProvenanceError("invalid serialized provenance context") from exc

    @staticmethod
    def _item_from_dict(value: object) -> PromptItem:
        if not isinstance(value, Mapping) or not isinstance(value.get("provenance"), Mapping):
            raise ProvenanceError("context item lacks provenance")
        metadata = value["provenance"]
        try:
            provenance = Provenance(
                SourceKind(metadata["source_kind"]),
                metadata["source_id"], metadata["origin"], metadata["content_digest"],
                TrustLabel(metadata["trust"]), tuple(metadata.get("parent_ids", ())), metadata.get("observed_at", ""),
            )
            return PromptItem(value["content"], provenance)
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ProvenanceError):
                raise
            raise ProvenanceError("invalid item provenance") from exc


__all__ = [
    "ContextPacket", "MAX_CONTENT_LENGTH", "MAX_ITEMS_PER_CONTEXT", "PromptItem",
    "PromptProvenanceBoundary", "PromotionDecision", "Provenance", "ProvenanceError",
    "SourceKind", "TrustLabel",
]
