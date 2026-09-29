"""Validity intervals for directly asserted project facts.

An append-only fact store keeps recalling facts that stopped being true
("hallucinations of the past").  A fact may therefore carry an optional
half-open validity interval ``[valid_from, valid_to)``.  ``NULL`` on either
bound means "unbounded on that side": ``valid_from`` NULL is "since an unknown
time" and ``valid_to`` NULL is "still true".  Every row written before the
columns existed has both bounds NULL and so stays current forever, which is
exactly how it was recalled before.

Facts have no deterministic subject key -- they are free text, and the only
existing identity is the exact normalized statement used for duplicate
detection.  Supersession is therefore never inferred: a writer names the
fact it replaces by id (``supersedes``) and the store closes that fact's
interval when the new fact is written.

Instants are stored as fixed-width UTC ISO-8601 text
(``YYYY-MM-DDTHH:MM:SS.ffffff+00:00``) so SQL can compare them as strings.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .authoritative_fact_metadata import (
    AuthoritativeFactMetadata,
    fact_metadata_from_inputs,
)

_INPUT_BOUND = 64
_SUPERSEDES_BOUND = 160

# A fact is current at instant ``?`` when the instant lies inside its interval.
# Both placeholders take the same ``now`` value.  Rows with NULL bounds (all
# legacy rows) always satisfy it.
CURRENT_FACT_PREDICATE = (
    "(valid_from IS NULL OR valid_from <= ?) AND (valid_to IS NULL OR valid_to > ?)"
)


def normalize_instant(value, label: str = "instant") -> str:
    """Return ``value`` as fixed-width UTC text; reject naive or malformed input."""
    if isinstance(value, datetime):
        parsed = value
    else:
        if not isinstance(value, str) or not value.strip() or len(value) > _INPUT_BOUND:
            raise ValueError(f"{label} metadata is invalid")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{label} metadata is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def utc_now_text(now=None) -> str:
    """The comparison instant for recall, in the stored text format."""
    return normalize_instant(now if now is not None else datetime.now(timezone.utc), "now")


@dataclass(frozen=True)
class FactValidity:
    """Validated validity bounds and an optional explicit predecessor id."""

    valid_from: str | None = None
    valid_to: str | None = None
    supersedes: str | None = None
    # The caller's original (valid_from, valid_until, supersedes) strings, kept
    # only so an authoritative source can re-validate them exactly as before.
    source_inputs: tuple = field(default=(), compare=False, repr=False)

    def __post_init__(self) -> None:
        if not self.source_inputs:
            object.__setattr__(self, "source_inputs", (
                self.valid_from or "", self.valid_to or "", self.supersedes or "",
            ))
        if self.valid_from is not None:
            object.__setattr__(self, "valid_from", normalize_instant(self.valid_from, "valid_from"))
        if self.valid_to is not None:
            object.__setattr__(self, "valid_to", normalize_instant(self.valid_to, "valid_until"))
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("valid_until must follow valid_from")
        if self.supersedes is not None and (
            not isinstance(self.supersedes, str) or not self.supersedes.strip()
            or len(self.supersedes) > _SUPERSEDES_BOUND
        ):
            raise ValueError("supersedes metadata is invalid")

    @property
    def is_empty(self) -> bool:
        return self.valid_from is None and self.valid_to is None and self.supersedes is None

    def authoritative_metadata(self) -> AuthoritativeFactMetadata:
        """The metadata a configured authoritative source receives today.

        Rebuilt from the caller's original strings so the authoritative path
        keeps its own validation (including its refusal of ``supersedes``
        without an indexed claim) exactly as before this module existed.
        """
        valid_from, valid_until, supersedes = self.source_inputs
        return fact_metadata_from_inputs("", "", valid_from, valid_until, supersedes, "")


def fact_write_inputs(
    entities_json: str = "",
    decision_json: str = "",
    valid_from: str = "",
    valid_until: str = "",
    supersedes: str = "",
    provenance_json: str = "",
):
    """Split remember-fact inputs into ``(authoritative_metadata, validity)``.

    Inputs that name entities, a decision or provenance are authoritative
    index claims and are decoded exactly as before (``validity`` is None).
    Inputs carrying only validity fields return ``(None, FactValidity)``: the
    legacy store honors them directly, and an authoritative source still gets
    the metadata it always did via ``FactValidity.authoritative_metadata``.
    No inputs at all returns ``(None, None)``, the unchanged plain write.
    """
    indexed = any((entities_json, decision_json, provenance_json))
    timed = any((valid_from, valid_until, supersedes))
    if indexed or not timed:
        return fact_metadata_from_inputs(
            entities_json, decision_json, valid_from, valid_until,
            supersedes, provenance_json,
        ), None
    values = {"valid_from": valid_from, "valid_until": valid_until, "supersedes": supersedes}
    for label, value in values.items():
        if not isinstance(value, str):
            raise ValueError("authoritative metadata inputs must be strings")
        if value and len(value) > _INPUT_BOUND:
            raise ValueError(label + " exceeds the input bound")
    return None, FactValidity(
        valid_from=valid_from or None, valid_to=valid_until or None,
        supersedes=supersedes or None,
        source_inputs=(valid_from, valid_until, supersedes),
    )


__all__ = [
    "CURRENT_FACT_PREDICATE",
    "FactValidity",
    "fact_write_inputs",
    "normalize_instant",
    "utc_now_text",
]
