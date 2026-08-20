"""Immutable, deterministic manifests for reproducible training runs."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping


SCHEMA = "sonder.training-manifest.v1"


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise TypeError("manifest values must be JSON-compatible") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _pairs(values: Mapping[str, Any] | None) -> tuple[tuple[str, Any], ...]:
    if not values:
        return ()
    return tuple(sorted(values.items(), key=lambda item: item[0]))


def _pairs_dict(values: tuple[tuple[str, Any], ...]) -> dict[str, Any]:
    return dict(values)


@dataclass(frozen=True)
class Provenance:
    source: str
    revision: str
    artifact_digest: str = ""
    metadata: tuple[tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not self.source.strip() or not self.revision.strip():
            raise ValueError("provenance requires source and revision")
        _canonical(_pairs_dict(self.metadata))

    @classmethod
    def from_mapping(cls, source: str, revision: str, *, artifact_digest: str = "", metadata: Mapping[str, Any] | None = None) -> "Provenance":
        return cls(source, revision, artifact_digest, _pairs(metadata))

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "revision": self.revision, "artifact_digest": self.artifact_digest, "metadata": _pairs_dict(self.metadata)}


@dataclass(frozen=True)
class DatasetManifest:
    dataset_id: str
    snapshot_digest: str
    row_count: int
    schema_version: str
    provenance: Provenance

    def __post_init__(self) -> None:
        if not self.dataset_id.strip() or not self.snapshot_digest.strip() or self.row_count < 0 or not self.schema_version.strip():
            raise ValueError("dataset manifest fields are invalid")

    def as_dict(self) -> dict[str, Any]:
        return {"dataset_id": self.dataset_id, "snapshot_digest": self.snapshot_digest, "row_count": self.row_count, "schema_version": self.schema_version, "provenance": self.provenance.as_dict()}


@dataclass(frozen=True)
class BaseModelManifest:
    model_id: str
    revision: str
    artifact_digest: str
    tokenizer_digest: str
    provenance: Provenance

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (self.model_id, self.revision, self.artifact_digest, self.tokenizer_digest)):
            raise ValueError("base model manifest fields are required")

    def as_dict(self) -> dict[str, Any]:
        return {"model_id": self.model_id, "revision": self.revision, "artifact_digest": self.artifact_digest, "tokenizer_digest": self.tokenizer_digest, "provenance": self.provenance.as_dict()}


@dataclass(frozen=True)
class DependencyManifest:
    name: str
    version: str
    source: str
    artifact_digest: str

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (self.name, self.version, self.source, self.artifact_digest)):
            raise ValueError("dependency manifest fields are required")

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "version": self.version, "source": self.source, "artifact_digest": self.artifact_digest}


@dataclass(frozen=True)
class EvaluationManifest:
    suite_id: str
    suite_version: str
    dataset_digest: str
    metrics: tuple[tuple[str, float], ...]
    provenance: Provenance

    def __post_init__(self) -> None:
        if not self.suite_id.strip() or not self.suite_version.strip() or not self.dataset_digest.strip():
            raise ValueError("evaluation manifest fields are required")
        if tuple(sorted(self.metrics)) != self.metrics:
            raise ValueError("evaluation metrics must be sorted")

    @classmethod
    def from_mapping(cls, suite_id: str, suite_version: str, dataset_digest: str, metrics: Mapping[str, float], provenance: Provenance) -> "EvaluationManifest":
        return cls(suite_id, suite_version, dataset_digest, tuple(sorted(metrics.items())), provenance)

    def as_dict(self) -> dict[str, Any]:
        return {"suite_id": self.suite_id, "suite_version": self.suite_version, "dataset_digest": self.dataset_digest, "metrics": _pairs_dict(self.metrics), "provenance": self.provenance.as_dict()}


@dataclass(frozen=True)
class ReproducibleTrainingManifest:
    dataset: DatasetManifest
    base_model: BaseModelManifest
    dependencies: tuple[DependencyManifest, ...]
    evaluation: EvaluationManifest
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SCHEMA:
            raise ValueError(f"unsupported training manifest schema: {self.schema}")
        names = tuple(item.name for item in self.dependencies)
        if names != tuple(sorted(names)) or len(set(names)) != len(names):
            raise ValueError("dependencies must be unique and sorted by name")
        if self.evaluation.dataset_digest != self.dataset.snapshot_digest:
            raise ValueError("evaluation dataset must match the locked snapshot")

    def as_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "dataset": self.dataset.as_dict(), "base_model": self.base_model.as_dict(), "dependencies": [item.as_dict() for item in self.dependencies], "evaluation": self.evaluation.as_dict()}

    @property
    def digest(self) -> str:
        return _digest(self.as_dict())
