"""Deterministic catalog for task, project, and personalization adapters."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from ...domain.training.reproducible import Provenance, _digest


class AdapterKind(str, Enum):
    TASK = "task"
    PROJECT = "project"
    PERSONALIZATION = "personalization"


@dataclass(frozen=True)
class AdapterSpec:
    adapter_id: str
    kind: AdapterKind
    base_model_id: str
    artifact_digest: str
    config_digest: str
    provenance: Provenance

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (self.adapter_id, self.base_model_id, self.artifact_digest, self.config_digest)):
            raise ValueError("adapter identity and digests are required")

    def as_dict(self) -> dict[str, object]:
        return {"adapter_id": self.adapter_id, "kind": self.kind.value, "base_model_id": self.base_model_id, "artifact_digest": self.artifact_digest, "config_digest": self.config_digest, "provenance": self.provenance.as_dict()}


@dataclass(frozen=True)
class AdapterCatalog:
    adapters: tuple[AdapterSpec, ...] = ()

    def __post_init__(self) -> None:
        ids = tuple(adapter.adapter_id for adapter in self.adapters)
        if ids != tuple(sorted(ids)) or len(set(ids)) != len(ids):
            raise ValueError("catalog adapters must be unique and sorted by id")

    @classmethod
    def from_adapters(cls, adapters: Iterable[AdapterSpec]) -> "AdapterCatalog":
        return cls(tuple(sorted(adapters, key=lambda adapter: adapter.adapter_id)))

    def register(self, adapter: AdapterSpec) -> "AdapterCatalog":
        if any(item.adapter_id == adapter.adapter_id for item in self.adapters):
            raise ValueError(f"adapter already registered: {adapter.adapter_id}")
        return self.from_adapters((*self.adapters, adapter))

    def get(self, adapter_id: str) -> AdapterSpec | None:
        return next((item for item in self.adapters if item.adapter_id == adapter_id), None)

    def compatible(self, adapter_id: str, base_model_id: str) -> bool:
        adapter = self.get(adapter_id)
        return adapter is not None and adapter.base_model_id == base_model_id

    @property
    def digest(self) -> str:
        return _digest([adapter.as_dict() for adapter in self.adapters])
