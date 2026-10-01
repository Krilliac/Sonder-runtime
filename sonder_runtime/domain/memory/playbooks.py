"""Pure rules and data types for owner-curated playbooks.

Playbooks intentionally use a small, boring data model.  The adapter owns
markdown and locking; this module owns validation, routing, quality and
approval decisions so those rules are usable by the HTTP, CLI and MCP
surfaces without importing either surface.
"""
from __future__ import annotations

import re
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

from sonder_runtime.domain.security.redaction import redact_text
from sonder_runtime.domain.memory.write_quality import classify

DEFAULT_CATEGORIES = frozenset({
    "pitfall", "procedure", "environment", "measurement", "decision",
    "tool-guide", "preference",
})
STATUSES = frozenset({"proposed", "approved", "rejected", "superseded"})
APPROVAL_MODES = frozenset({"required", "owner_corrections_auto", "auto"})
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_INDEX_CHARS = 4096
MAX_TOPIC_CHARS = 256 * 1024
MAX_ENTRY_CHARS = 32 * 1024
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def content_digest(entry: dict) -> str:
    material = {key: entry.get(key) for key in (
        "id", "topic", "category", "title", "body", "evidence", "triggers",
        "date", "provenance", "tainted", "supersedes",
    )}
    return hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PlaybookPolicy:
    """Limits and approval rules; all values are deliberately bounded."""

    approval: str = "required"
    categories: frozenset[str] = field(default_factory=lambda: DEFAULT_CATEGORIES)
    max_index_chars: int = MAX_INDEX_CHARS
    max_topic_chars: int = MAX_TOPIC_CHARS
    max_entry_chars: int = MAX_ENTRY_CHARS
    near_duplicate_threshold: float = 0.84
    max_loaded_entries: int = 12
    max_loaded_chars: int = 24 * 1024
    # Byte-named aliases are accepted by configuration loaders. Markdown is
    # UTF-8; character limits remain the public pure-rule fallback.
    max_entry_bytes: int | None = None
    max_topic_bytes: int | None = None
    max_index_bytes: int | None = None
    max_topics: int = 64
    max_topics_per_turn: int = 3
    max_context_bytes: int | None = None

    def __post_init__(self) -> None:
        if self.approval not in APPROVAL_MODES:
            raise ValueError("approval must be required, owner_corrections_auto, or auto")
        if not self.categories:
            raise ValueError("categories cannot be empty")
        if not 0.0 <= self.near_duplicate_threshold <= 1.0:
            raise ValueError("near_duplicate_threshold must be between 0 and 1")
        for name in ("max_index_chars", "max_topic_chars", "max_entry_chars",
                     "max_loaded_entries", "max_loaded_chars"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")
        for name in ("max_entry_bytes", "max_topic_bytes", "max_index_bytes", "max_context_bytes"):
            value = getattr(self, name)
            if value is not None and value < 1:
                raise ValueError(f"{name} must be positive")
        if self.max_topics < 1 or self.max_topics_per_turn < 1:
            raise ValueError("topic limits must be positive")

    @property
    def entry_limit(self) -> int:
        return self.max_entry_bytes or self.max_entry_chars

    @property
    def topic_limit(self) -> int:
        return self.max_topic_bytes or self.max_topic_chars

    @property
    def index_limit(self) -> int:
        return self.max_index_bytes or self.max_index_chars

    @property
    def context_limit(self) -> int:
        return self.max_context_bytes or self.max_loaded_chars


def slugify(topic: str) -> str:
    if not isinstance(topic, str) or len(topic) > 128 or any(ord(char) < 32 for char in topic):
        raise ValueError("topic must be a single line")
    value = re.sub(r"[^a-z0-9]+", "-", str(topic).strip().lower()).strip("-")
    if not SLUG_RE.fullmatch(value):
        raise ValueError("topic must produce a safe markdown slug")
    return value


def validate_category(category: str, policy: PlaybookPolicy | None = None) -> str:
    policy = policy or PlaybookPolicy()
    value = str(category).strip().lower()
    if value not in policy.categories:
        raise ValueError(f"unsupported playbook category: {value}")
    return value


def _tokens(value: str) -> set[str]:
    return set(_TOKEN_RE.findall(value.lower()))


def similarity(left: str, right: str) -> float:
    a, b = _tokens(left), _tokens(right)
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b) if a and b else 0.0


def duplicate_match(candidate: str, existing: Iterable[dict[str, Any]], threshold: float = .84) -> dict[str, Any] | None:
    """Return an exact/near duplicate, keeping lexical matching cheap."""
    normalized = " ".join(candidate.lower().split())
    for item in existing:
        hay = " ".join(str(item.get("search_text", item.get("title", ""))).lower().split())
        score = similarity(normalized, hay)
        if normalized and normalized == hay or score >= threshold:
            result = dict(item)
            result["similarity"] = 1.0 if normalized == hay else score
            return result
    return None


def redact(value: Any) -> str:
    return redact_text(str(value or ""))


def quality_findings(title: str, body: str, evidence: str = "", *, category: str = "procedure", policy: PlaybookPolicy | None = None) -> list[str]:
    """Report bounded, self-contained quality issues.

    Procedures may contain several steps and therefore are not subjected to
    the atomic 280-character lesson gate.
    """
    policy = policy or PlaybookPolicy()
    findings: list[str] = []
    if not title.strip():
        findings.append("empty_title")
    if not body.strip():
        findings.append("empty_body")
    if len((title + body + evidence).encode("utf-8")) > policy.entry_limit:
        findings.append("length_out_of_bounds")
    if len(body.split()) < 3:
        findings.append("length_out_of_bounds")
    canonical = classify(body + " " + date.today().isoformat())
    for finding in canonical:
        # A curated procedure is allowed to be several steps and longer than
        # a lesson.  Other canonical checks remain useful here.
        if category in {"procedure", "pitfall", "tool-guide", "measurement", "decision"} and finding in {"multi_claim", "length_out_of_bounds"}:
            continue
        if finding not in {"length_out_of_bounds"}:
            findings.append(finding)
    return findings


def make_entry(topic: str, category: str, title: str, body: str, evidence: Any = "", *,
               triggers: Iterable[str] | None = None, provenance: dict[str, Any] | None = None,
               tainted: bool = True, owner_correction: bool = False,
               policy: PlaybookPolicy | None = None, entry_id: str | None = None,
               created: str | None = None) -> dict[str, Any]:
    policy = policy or PlaybookPolicy()
    slug = slugify(topic)
    category = validate_category(category, policy)
    title, body = redact(title).strip(), redact(body).strip()
    if any(char in title for char in "\r\n"):
        raise ValueError("title must be a single markdown heading line")
    evidence_text = redact(evidence)
    if any(re.search(r"(?m)^##\s+Entry:|<!-- playbook-entry-end -->|^### (?:Body|Evidence|Triggers)\s*$", text) for text in (title, body, evidence_text)):
        raise ValueError("entry content contains a reserved playbook marker")
    if any(re.search(r"(?m)^##\s+Entry:", str(item)) for item in (triggers or [])):
        raise ValueError("entry trigger contains a reserved playbook marker")
    if any(any(char in str(item) for char in "\r\n") for item in (triggers or [])):
        raise ValueError("triggers must be single-line phrases")
    if triggers is not None and (isinstance(triggers, str) or len(triggers) > 32 or any(not isinstance(item, str) or len(item) > 128 for item in triggers)):
        raise ValueError("triggers must be at most 32 bounded phrases")
    if len(title) > 200:
        raise ValueError("entry title exceeds limit")
    findings = quality_findings(title, body, evidence_text, category=category, policy=policy)
    if findings:
        raise ValueError("playbook entry failed quality checks: " + ", ".join(findings))
    now = created or date.today().isoformat()
    date.fromisoformat(now)
    approved = approval_status(policy, tainted=tainted, owner_correction=owner_correction) == "approved"
    safe_provenance = {"session_id": "unknown", "request_id": "unknown", "model": "unknown", "repo": "unknown", "commit": "unknown", **{
        str(key): redact(value) if not isinstance(value, (dict, list, tuple)) else redact(value)
        for key, value in (provenance or {}).items()
    }}
    return {
        "id": entry_id or uuid.uuid4().hex,
        "topic": slug, "category": category, "title": title, "body": body,
        "evidence": evidence_text, "triggers": [redact(t).strip() for t in (triggers or []) if str(t).strip()],
        "date": now, "provenance": safe_provenance, "tainted": bool(tainted),
        "status": "approved" if approved else "proposed", "supersedes": None,
    }


def status_allowed(status: str) -> bool:
    return status in STATUSES


def approval_status(policy: PlaybookPolicy, *, tainted: bool, owner_correction: bool) -> str:
    if tainted:
        return "proposed"
    if policy.approval == "auto" or (policy.approval == "owner_corrections_auto" and owner_correction):
        return "approved"
    return "proposed"
