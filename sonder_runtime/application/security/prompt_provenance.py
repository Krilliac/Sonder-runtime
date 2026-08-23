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
MAX_SOURCE_ID_LENGTH = 512
MAX_ORIGIN_LENGTH = 2_048
MAX_PARENT_ID_LENGTH = 512
MAX_OBSERVED_AT_LENGTH = 64


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


def _text(value: str, name: str, *, max_length: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProvenanceError(f"{name} must not be empty")
    normalized = value.strip()
    if max_length is not None and len(normalized) > max_length:
        raise ProvenanceError(f"{name} exceeds the provenance boundary")
    return normalized


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _request_digest(prompt: str, system: str, history: Sequence[object], packet_digest: str) -> str:
    """Bind every prompt-visible request field to its provenance packet."""
    return _digest(_canonical({
        "prompt": prompt,
        "system": system,
        "history": list(history),
        "packet_digest": packet_digest,
    }))


def _provenance_digest(
    content: str,
    source_kind: SourceKind,
    source_id: str,
    origin: str,
    parent_ids: Sequence[str],
) -> str:
    """Bind visible bytes to the complete external-source identity."""
    return _digest(_canonical({
        "content": content,
        "source_kind": source_kind.value,
        "source_id": source_id,
        "origin": origin,
        "parent_ids": list(parent_ids),
    }))


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
        if not isinstance(self.source_kind, SourceKind):
            raise ProvenanceError("source_kind must be a supported SourceKind")
        if not isinstance(self.trust, TrustLabel):
            raise ProvenanceError("trust must be a supported TrustLabel")
        _text(self.source_id, "source_id", max_length=MAX_SOURCE_ID_LENGTH)
        _text(self.origin, "origin", max_length=MAX_ORIGIN_LENGTH)
        if len(self.content_digest) != 64 or any(c not in "0123456789abcdef" for c in self.content_digest):
            raise ProvenanceError("content_digest must be a SHA-256 hex digest")
        if len(self.parent_ids) > MAX_PROVENANCE_DEPTH:
            raise ProvenanceError("provenance chain is too deep")
        for parent_id in self.parent_ids:
            _text(parent_id, "parent_id", max_length=MAX_PARENT_ID_LENGTH)
        if not self.observed_at:
            object.__setattr__(self, "observed_at", datetime.now(timezone.utc).isoformat())
        elif not isinstance(self.observed_at, str) or len(self.observed_at) > MAX_OBSERVED_AT_LENGTH:
            raise ProvenanceError("observed_at exceeds the provenance boundary")

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
        if _provenance_digest(
            self.content,
            self.provenance.source_kind,
            self.provenance.source_id,
            self.provenance.origin,
            self.provenance.parent_ids,
        ) != self.provenance.content_digest:
            raise ProvenanceError("content or provenance does not match digest")

    @property
    def is_untrusted(self) -> bool:
        return self.provenance.trust is TrustLabel.UNTRUSTED

    def as_dict(self) -> dict[str, object]:
        return {"content": self.content, "provenance": self.provenance.as_dict()}

    def redacted_metadata(self) -> dict[str, object]:
        """Return event-safe provenance without source names or prompt text."""
        return {
            "source_kind": self.provenance.source_kind.value,
            "trust": self.provenance.trust.value,
            "content_digest": self.provenance.content_digest,
            "parent_count": len(self.provenance.parent_ids),
        }


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

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple) or len(self.items) > MAX_ITEMS_PER_CONTEXT:
            raise ProvenanceError("invalid context packet items")
        if any(not isinstance(item, PromptItem) for item in self.items):
            raise ProvenanceError("context packet contains an invalid item")
        expected = _digest(_canonical([item.as_dict() for item in self.items]))
        if self.packet_digest != expected:
            raise ProvenanceError("context packet digest mismatch")

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

    def redacted_metadata(self) -> dict[str, object]:
        """Return the minimum metadata safe for durable event telemetry."""
        return {
            "packet_digest": self.packet_digest,
            "item_count": len(self.items),
            "labels": [item.redacted_metadata() for item in self.items],
        }


@dataclass(frozen=True)
class ModelRequestProvenance:
    """Tamper-evident binding carried alongside a model request.

    The binding contains no prompt text.  It proves which context packet and
    prompt-visible fields were sent to the gateway, while keeping event
    records free of untrusted content.
    """

    packet_digest: str
    request_digest: str
    item_digests: tuple[str, ...]

    def __post_init__(self) -> None:
        for name, value in (("packet_digest", self.packet_digest), ("request_digest", self.request_digest)):
            if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ProvenanceError(f"{name} must be a SHA-256 hex digest")
        if not isinstance(self.item_digests, tuple) or len(self.item_digests) > MAX_ITEMS_PER_CONTEXT:
            raise ProvenanceError("invalid provenance item digest list")
        for digest in self.item_digests:
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ProvenanceError("item_digests must contain SHA-256 hex digests")

    def as_dict(self) -> dict[str, object]:
        return {
            "packet_digest": self.packet_digest,
            "request_digest": self.request_digest,
            "item_digests": list(self.item_digests),
        }


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
        content = _text(content, "content", max_length=MAX_CONTENT_LENGTH)
        parents = tuple(dict.fromkeys(
            _text(parent, "parent_id", max_length=MAX_PARENT_ID_LENGTH)
            for parent in parent_ids
        ))
        source_id = _text(source_id, "source_id", max_length=MAX_SOURCE_ID_LENGTH)
        origin = _text(origin, "origin", max_length=MAX_ORIGIN_LENGTH)
        provenance = Provenance(
            kind, source_id, origin,
            _provenance_digest(content, kind, source_id, origin, parents),
            TrustLabel.UNTRUSTED, parents, observed_at or "",
        )
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

    def bind_model_request(
        self,
        prompt: str,
        *,
        system: str = "",
        history: Sequence[object] = (),
        context: ContextPacket,
    ) -> ModelRequestProvenance:
        """Create the required binding before prompt-visible data reaches a gateway."""
        if not isinstance(context, ContextPacket):
            raise ProvenanceError("model request requires a validated context packet")
        if not isinstance(prompt, str) or not prompt.strip() or not isinstance(system, str):
            raise ProvenanceError("model request prompt fields are invalid")
        try:
            history_tuple = tuple(history)
            _canonical(history_tuple)
        except (TypeError, ValueError) as exc:
            raise ProvenanceError("model request history is not JSON-safe") from exc
        return ModelRequestProvenance(
            packet_digest=context.packet_digest,
            request_digest=_request_digest(prompt, system, history_tuple, context.packet_digest),
            item_digests=tuple(item.provenance.content_digest for item in context.items),
        )

    def validate_model_request(
        self,
        prompt: str,
        *,
        system: str = "",
        history: Sequence[object] = (),
        context: ContextPacket,
        binding: ModelRequestProvenance,
    ) -> None:
        """Fail closed if a request, packet, or label was altered after binding."""
        expected = self.bind_model_request(
            prompt, system=system, history=history, context=context,
        )
        if binding != expected:
            raise ProvenanceError("model request provenance binding mismatch")

    @staticmethod
    def event_metadata(context: ContextPacket) -> dict[str, object]:
        """Produce redacted event fields; raw untrusted content never crosses."""
        if not isinstance(context, ContextPacket):
            raise ProvenanceError("event provenance requires a validated context packet")
        return context.redacted_metadata()

    @staticmethod
    def request_event_metadata(
        context: ContextPacket, binding: ModelRequestProvenance,
    ) -> dict[str, object]:
        """Return redacted packet labels plus the request binding digest."""
        metadata = PromptProvenanceBoundary.event_metadata(context)
        if binding.packet_digest != context.packet_digest:
            raise ProvenanceError("request binding does not match context packet")
        expected_items = tuple(
            item.provenance.content_digest for item in context.items
        )
        if binding.item_digests != expected_items:
            raise ProvenanceError("request binding item digests do not match context packet")
        metadata["request_digest"] = binding.request_digest
        return metadata

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
            # Serialized content carries no independent authority capable of
            # proving a trust promotion.  It may replay the untrusted label
            # produced at ingestion, but cannot self-assert user confirmation
            # or independent verification by recomputing an unkeyed digest.
            if provenance.trust is not TrustLabel.UNTRUSTED:
                raise ProvenanceError(
                    "serialized provenance cannot self-assert a trusted label"
                )
            return PromptItem(value["content"], provenance)
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ProvenanceError):
                raise
            raise ProvenanceError("invalid item provenance") from exc


__all__ = [
    "ContextPacket", "MAX_CONTENT_LENGTH", "MAX_ITEMS_PER_CONTEXT", "PromptItem",
    "ModelRequestProvenance", "PromptProvenanceBoundary", "PromotionDecision", "Provenance", "ProvenanceError",
    "SourceKind", "TrustLabel",
]
