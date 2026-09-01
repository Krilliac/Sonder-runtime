"""Typed, read-only operator snapshot of runtime control-plane state.

The snapshot is a presentation boundary, not a second source of truth.  Its
sections contain immutable records copied from the owning application ports.
No mutating operation is exposed and the canonical form is stable for UI
polling, audit logs, and health comparisons.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from types import MappingProxyType
from typing import Any, Mapping


class SnapshotValidationError(ValueError):
    """Raised when a control-plane snapshot contains invalid input."""


MAX_SECTION_RECORDS = 1024


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (tuple, list, frozenset)):
        items = [_plain(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, default=str)) if isinstance(value, frozenset) else items
    return value


@dataclass(frozen=True)
class SnapshotSection:
    """An immutable named collection of operator-visible records."""

    name: str
    records: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise SnapshotValidationError("section name must be non-empty")
        frozen = tuple(_freeze(record) for record in self.records)
        if any(not isinstance(record, Mapping) for record in frozen):
            raise SnapshotValidationError(f"{self.name} records must be mappings")
        if len(frozen) > MAX_SECTION_RECORDS:
            raise SnapshotValidationError(
                f"{self.name} exceeds the {MAX_SECTION_RECORDS}-record limit"
            )
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "records", frozen)

    @classmethod
    def from_records(cls, name: str, records: Any = ()) -> "SnapshotSection":
        if records is None:
            records = ()
        return cls(name=name, records=tuple(records))

    @property
    def count(self) -> int:
        return len(self.records)


CONTROL_PLANE_SECTIONS = (
    "sessions", "plans", "approvals", "jobs", "agents", "model_hardware",
    "context", "memory_explanations", "extensions", "training", "selfmod",
    "updates", "health", "startup_authorities", "compute_fabric",
)
_SECTION_NAMES = CONTROL_PLANE_SECTIONS


@dataclass(frozen=True)
class ControlPlaneSnapshot:
    """Complete point-in-time, read-only operator view of the runtime."""

    captured_at: str
    revision: int
    sessions: SnapshotSection
    plans: SnapshotSection
    approvals: SnapshotSection
    jobs: SnapshotSection
    agents: SnapshotSection
    model_hardware: SnapshotSection
    context: SnapshotSection
    memory_explanations: SnapshotSection
    extensions: SnapshotSection
    training: SnapshotSection
    selfmod: SnapshotSection
    updates: SnapshotSection
    health: SnapshotSection
    startup_authorities: SnapshotSection
    compute_fabric: SnapshotSection

    def __post_init__(self) -> None:
        if not isinstance(self.captured_at, str) or not self.captured_at.strip():
            raise SnapshotValidationError("captured_at must be non-empty")
        if not isinstance(self.revision, int) or self.revision < 0:
            raise SnapshotValidationError("revision must be a non-negative integer")
        for name in _SECTION_NAMES:
            section = getattr(self, name)
            if not isinstance(section, SnapshotSection) or section.name != name:
                raise SnapshotValidationError(f"{name} must be a matching SnapshotSection")
        object.__setattr__(self, "captured_at", self.captured_at.strip())

    @classmethod
    def build(
        cls, *, captured_at: str, revision: int = 0,
        **sections: Any,
    ) -> "ControlPlaneSnapshot":
        unknown = set(sections) - set(_SECTION_NAMES)
        if unknown:
            raise SnapshotValidationError(f"unknown sections: {sorted(unknown)}")
        values = {
            name: sections.get(name, ()) if not isinstance(sections.get(name, ()), SnapshotSection) else sections[name]
            for name in _SECTION_NAMES
        }
        for name, value in list(values.items()):
            if not isinstance(value, SnapshotSection):
                values[name] = SnapshotSection.from_records(name, value)
        return cls(captured_at=captured_at, revision=revision, **values)

    def as_dict(self) -> dict[str, Any]:
        return {
            "captured_at": self.captured_at,
            "revision": self.revision,
            "sections": {
                name: {"name": section.name, "records": _plain(section.records), "count": section.count}
                for name in _SECTION_NAMES
                for section in (getattr(self, name),)
            },
        }

    def digest(self) -> str:
        payload = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def total_records(self) -> int:
        return sum(getattr(self, name).count for name in _SECTION_NAMES)


__all__ = [
    "CONTROL_PLANE_SECTIONS",
    "MAX_SECTION_RECORDS",
    "ControlPlaneSnapshot",
    "SnapshotSection",
    "SnapshotValidationError",
]
