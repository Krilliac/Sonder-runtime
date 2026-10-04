"""Leases on named external resources that parallel workers share.

Worktrees stopped parallel workers colliding on files; ports, dev servers,
browser tabs, temporary databases and the one computer-use desktop still
collide. A resource lease names one such resource as ``(kind, key)`` and
records who holds it, until when, and what would prove it has been cleaned up.

This module is the pure policy; ``adapters.resource_leases`` stores the leases
durably and supplies the evidence (process liveness, a bind test on a port).

The reclaim rule is deliberately narrow. A lease that another worker holds is
never taken merely because it looks old. It is reclaimed only when

* its owner is *proven* dead (a probe that cannot decide counts as alive), or
  its TTL has expired without a heartbeat, **and**
* resource-specific cleanup evidence exists: the port no longer answers a
  bind test, the dev server's process is gone, the temp database's directory
  is gone, the desktop session's indicator process has exited.

Without that evidence the lease stays held and the caller is refused, however
stale it looks. A wedged lease is released by its owner, not stolen.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping

from .common.errors import Conflict, Forbidden, InvalidInput, NotFound

KIND_PORT = "port"
KIND_DEV_SERVER = "dev_server"
KIND_BROWSER_TAB = "browser_tab"
KIND_TEMP_DB = "temp_db"
KIND_DESKTOP_SESSION = "desktop_session"
BUILTIN_KINDS = (KIND_PORT, KIND_DEV_SERVER, KIND_BROWSER_TAB, KIND_TEMP_DB,
                 KIND_DESKTOP_SESSION)

RESOURCE_LEASE_BUSY = "RESOURCE_LEASE_BUSY"
RESOURCE_LEASE_NOT_OWNER = "RESOURCE_LEASE_NOT_OWNER"
RESOURCE_LEASE_EXHAUSTED = "RESOURCE_LEASE_EXHAUSTED"
RESOURCE_LEASE_NOT_FOUND = "RESOURCE_LEASE_NOT_FOUND"

MIN_TTL_SECONDS = 1.0
MAX_TTL_SECONDS = 7 * 24 * 3600.0
MAX_METADATA_ENTRIES = 16
MAX_TEXT = 256

_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def lease_error(code: str, message: str) -> Conflict | Forbidden | InvalidInput | NotFound:
    """A taxonomy error carrying one of the stable resource-lease codes."""
    cls = {RESOURCE_LEASE_BUSY: Conflict, RESOURCE_LEASE_NOT_OWNER: Forbidden,
           RESOURCE_LEASE_EXHAUSTED: Conflict,
           RESOURCE_LEASE_NOT_FOUND: NotFound}.get(code, InvalidInput)
    error = cls(message)
    error.code = code
    return error


def validate_kind(kind: object) -> str:
    if not isinstance(kind, str) or not _KIND_RE.match(kind):
        raise InvalidInput("resource kind must be a short lower_snake_case name")
    return kind


def validate_text(name: str, value: object) -> str:
    if (not isinstance(value, str) or not value.strip() or "\x00" in value
            or len(value) > MAX_TEXT):
        raise InvalidInput("%s must be a non-empty bounded string" % name)
    return value


def validate_ttl(ttl_seconds: object) -> float:
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)):
        raise InvalidInput("ttl_seconds must be a number")
    ttl = float(ttl_seconds)
    if not (MIN_TTL_SECONDS <= ttl <= MAX_TTL_SECONDS):
        raise InvalidInput("ttl_seconds must be between %g and %g"
                           % (MIN_TTL_SECONDS, MAX_TTL_SECONDS))
    return ttl


def validate_metadata(metadata: Mapping | None) -> dict:
    """Metadata is small evidence (a pid, a port, a path), never secrets."""
    if metadata is None:
        return {}
    if not isinstance(metadata, Mapping) or len(metadata) > MAX_METADATA_ENTRIES:
        raise InvalidInput("metadata must be a small mapping")
    clean: dict = {}
    for name, value in metadata.items():
        validate_text("metadata key", name)
        if isinstance(value, bool) or value is None:
            clean[name] = value
        elif isinstance(value, int):
            clean[name] = value
        elif isinstance(value, float):
            clean[name] = value
        elif isinstance(value, str) and len(value) <= 1024 and "\x00" not in value:
            clean[name] = value
        else:
            raise InvalidInput("metadata values must be scalars")
    return clean


@dataclass(frozen=True)
class ResourceLease:
    """One held resource. ``owner_pid``/``owner_identity`` let a dead owner be proven."""

    lease_id: str
    kind: str
    key: str
    owner_id: str
    acquired_at: float
    expires_at: float
    ttl_seconds: float
    owner_pid: int | None = None
    owner_identity: str | None = None
    heartbeat_at: float = 0.0
    metadata: Mapping = field(default_factory=dict)

    @property
    def resource(self) -> str:
        return "%s:%s" % (self.kind, self.key)

    def expired(self, now: float) -> bool:
        return now >= self.expires_at

    def to_record(self) -> dict:
        return {
            "lease_id": self.lease_id, "kind": self.kind, "key": self.key,
            "owner_id": self.owner_id, "acquired_at": self.acquired_at,
            "expires_at": self.expires_at, "ttl_seconds": self.ttl_seconds,
            "owner_pid": self.owner_pid, "owner_identity": self.owner_identity,
            "heartbeat_at": self.heartbeat_at, "metadata": dict(self.metadata),
        }

    @classmethod
    def from_record(cls, record: Mapping) -> "ResourceLease":
        pid = record.get("owner_pid")
        identity = record.get("owner_identity")
        return cls(
            lease_id=str(record["lease_id"]), kind=str(record["kind"]),
            key=str(record["key"]), owner_id=str(record["owner_id"]),
            acquired_at=float(record["acquired_at"]),
            expires_at=float(record["expires_at"]),
            ttl_seconds=float(record["ttl_seconds"]),
            owner_pid=int(pid) if isinstance(pid, int) and not isinstance(pid, bool) else None,
            owner_identity=str(identity) if identity else None,
            heartbeat_at=float(record.get("heartbeat_at") or record["acquired_at"]),
            metadata=dict(record.get("metadata") or {}),
        )


@dataclass(frozen=True)
class ReclaimDecision:
    reclaim: bool
    reason: str


def reclaim_decision(*, owner_dead: bool, ttl_expired: bool,
                     cleanup_evidence: bool) -> ReclaimDecision:
    """Whether a held lease may be taken from its holder, and why (not)."""
    if not (owner_dead or ttl_expired):
        return ReclaimDecision(False, "held by a live owner inside its TTL")
    if not cleanup_evidence:
        return ReclaimDecision(False, "%s, but nothing proves the resource was cleaned up"
                               % ("owner is dead" if owner_dead else "TTL expired"))
    return ReclaimDecision(True, "%s and the resource is proven released"
                           % ("owner is dead" if owner_dead else "TTL expired"))


def parse_port_range(text: str) -> tuple[int, int]:
    """``"47000-47999"`` -> ``(47000, 47999)``; refuses privileged or inverted ranges."""
    match = re.fullmatch(r"\s*(\d{1,5})\s*-\s*(\d{1,5})\s*", str(text or ""))
    if not match:
        raise InvalidInput("port range must look like 47000-47999")
    low, high = int(match.group(1)), int(match.group(2))
    return validate_port_range((low, high))


def validate_port_range(port_range: tuple[int, int]) -> tuple[int, int]:
    try:
        low, high = (int(port_range[0]), int(port_range[1]))
    except (TypeError, ValueError, IndexError):
        raise InvalidInput("port range must be two integers") from None
    if not (1024 <= low <= high <= 65535):
        raise InvalidInput("port range must lie inside 1024-65535 and be ordered")
    return low, high


__all__ = [
    "BUILTIN_KINDS", "KIND_BROWSER_TAB", "KIND_DESKTOP_SESSION", "KIND_DEV_SERVER",
    "KIND_PORT", "KIND_TEMP_DB", "RESOURCE_LEASE_BUSY", "RESOURCE_LEASE_EXHAUSTED",
    "RESOURCE_LEASE_NOT_FOUND", "RESOURCE_LEASE_NOT_OWNER", "ReclaimDecision", "ResourceLease", "lease_error",
    "parse_port_range", "reclaim_decision", "validate_kind", "validate_metadata",
    "validate_port_range", "validate_text", "validate_ttl",
]
