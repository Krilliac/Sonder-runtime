"""Explicit retention, redaction, export, and deletion decisions for events."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class EventPrivacyClass(str, Enum):
    PUBLIC_METADATA = "public_metadata"
    CONTENT_REFERENCE = "content_reference"
    SENSITIVE_CONTENT = "sensitive_content"
    SECRET = "secret"


@dataclass(frozen=True)
class RetentionRule:
    privacy_class: EventPrivacyClass
    retention_days: int | None
    allow_export: bool
    allow_delete: bool
    redact_on_export: bool

    def __post_init__(self) -> None:
        if self.retention_days is not None and self.retention_days < 0:
            raise ValueError("retention_days must be non-negative or None")


DEFAULT_RULES = {
    EventPrivacyClass.PUBLIC_METADATA: RetentionRule(EventPrivacyClass.PUBLIC_METADATA, None, True, True, False),
    EventPrivacyClass.CONTENT_REFERENCE: RetentionRule(EventPrivacyClass.CONTENT_REFERENCE, 365, True, True, True),
    EventPrivacyClass.SENSITIVE_CONTENT: RetentionRule(EventPrivacyClass.SENSITIVE_CONTENT, 30, False, True, True),
    EventPrivacyClass.SECRET: RetentionRule(EventPrivacyClass.SECRET, 0, False, True, True),
}


def rule_for(privacy_class: EventPrivacyClass | str) -> RetentionRule:
    try:
        return DEFAULT_RULES[privacy_class if isinstance(privacy_class, EventPrivacyClass) else EventPrivacyClass(privacy_class)]
    except (KeyError, ValueError) as exc:
        raise ValueError(f"unknown privacy class: {privacy_class!r}") from exc


def export_decision(privacy_class: EventPrivacyClass | str) -> dict[str, bool]:
    rule = rule_for(privacy_class)
    return {"allowed": rule.allow_export, "redact": rule.redact_on_export}
