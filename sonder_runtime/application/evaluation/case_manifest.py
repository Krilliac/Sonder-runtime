"""Bounded, immutable manifests for provider-neutral evaluation cases.

This module deliberately stops before execution.  It gives evaluation runners a
shared case identity, grader reference, provenance record, and content digest
without importing a model, verifier implementation, network client, or storage
adapter.  Adapters can preflight verifier availability with
``inspect_manifest`` before deciding whether a manifest is runnable.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .proposal_lifecycle import EvaluationDimension, EvaluationLifecycleError, EvaluationSuite


SCHEMA = "sonder.evaluation-case-manifest.v1"
MAX_CASES = 256
MAX_CASE_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_TAGS = 32
MAX_JSON_DEPTH = 24
MODEL_GRADED_VERIFIERS = frozenset({"llm_judge"})

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class EvaluationCaseManifestError(ValueError):
    """Raised when a case manifest is malformed, unbounded, or tampered with."""


def _text(value: Any, label: str, *, identifier: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvaluationCaseManifestError(f"{label} must be a non-empty string")
    clean = value.strip()
    if identifier and not _IDENTIFIER.fullmatch(clean):
        raise EvaluationCaseManifestError(f"{label} contains unsupported characters")
    return clean


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _freeze(value: Any, *, label: str, depth: int = 0) -> Any:
    if depth > MAX_JSON_DEPTH:
        raise EvaluationCaseManifestError(f"{label} exceeds the JSON nesting bound")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EvaluationCaseManifestError(f"{label} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise EvaluationCaseManifestError(f"{label} object keys must be strings")
            clean[key] = _freeze(item, label=label, depth=depth + 1)
        return MappingProxyType(dict(sorted(clean.items())))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item, label=label, depth=depth + 1) for item in value)
    raise EvaluationCaseManifestError(f"{label} must be JSON-compatible")


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            _thaw(value), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise EvaluationCaseManifestError("manifest values must be bounded JSON") from exc


def _keys(payload: Mapping[str, Any], *, allowed: set[str], required: set[str], label: str) -> None:
    missing = required - set(payload)
    unknown = set(payload) - allowed
    if missing:
        raise EvaluationCaseManifestError(f"{label} is missing: {', '.join(sorted(missing))}")
    if unknown:
        raise EvaluationCaseManifestError(f"{label} has unknown fields: {', '.join(sorted(unknown))}")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            # Do not echo a caller-controlled key into diagnostics. Manifest
            # payloads may contain private task material even when malformed.
            raise EvaluationCaseManifestError("case manifest contains duplicate object fields")
        result[key] = value
    return result


@dataclass(frozen=True)
class EvaluationCaseProvenance:
    """Origin and immutable revision for one evaluation case."""

    source: str
    revision: str
    artifact_digest: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _text(self.source, "provenance source"))
        object.__setattr__(self, "revision", _text(self.revision, "provenance revision"))
        if self.artifact_digest and not _SHA256.fullmatch(self.artifact_digest):
            raise EvaluationCaseManifestError("provenance artifact_digest must be lowercase SHA-256")

    def as_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "revision": self.revision,
            "artifact_digest": self.artifact_digest,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvaluationCaseProvenance":
        if not isinstance(payload, Mapping):
            raise EvaluationCaseManifestError("case provenance must be an object")
        _keys(payload, allowed={"source", "revision", "artifact_digest"},
              required={"source", "revision"}, label="case provenance")
        return cls(payload["source"], payload["revision"], payload.get("artifact_digest", ""))


@dataclass(frozen=True)
class EvaluationCaseGrader:
    """Reference to a verifier plus its JSON-only, non-executed specification."""

    verifier: str
    spec: Mapping[str, Any]
    advisory: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "verifier", _text(self.verifier, "grader verifier", identifier=True))
        if not isinstance(self.advisory, bool):
            raise EvaluationCaseManifestError("grader advisory must be boolean")
        frozen = _freeze(self.spec, label="grader spec")
        if not isinstance(frozen, Mapping):
            raise EvaluationCaseManifestError("grader spec must be an object")
        object.__setattr__(self, "spec", frozen)
        if self.verifier in MODEL_GRADED_VERIFIERS and not self.advisory:
            raise EvaluationCaseManifestError("model-graded verifiers must be advisory")

    @property
    def deterministic(self) -> bool:
        return not self.advisory

    def as_dict(self) -> dict[str, Any]:
        return {"verifier": self.verifier, "spec": _thaw(self.spec), "advisory": self.advisory}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvaluationCaseGrader":
        if not isinstance(payload, Mapping):
            raise EvaluationCaseManifestError("case grader must be an object")
        _keys(payload, allowed={"verifier", "spec", "advisory"},
              required={"verifier", "spec", "advisory"}, label="case grader")
        return cls(payload["verifier"], payload["spec"], payload["advisory"])


@dataclass(frozen=True)
class EvaluationCase:
    """One immutable input/target pair bound to a grader and provenance."""

    case_id: str
    input: Any
    target: Any
    grader: EvaluationCaseGrader
    provenance: EvaluationCaseProvenance
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", _text(self.case_id, "case_id", identifier=True))
        if not isinstance(self.grader, EvaluationCaseGrader):
            raise EvaluationCaseManifestError("case grader is required")
        if not isinstance(self.provenance, EvaluationCaseProvenance):
            raise EvaluationCaseManifestError("case provenance is required")
        frozen_input = _freeze(self.input, label="case input")
        frozen_target = _freeze(self.target, label="case target")
        if frozen_input is None:
            raise EvaluationCaseManifestError("case input must not be null")
        object.__setattr__(self, "input", frozen_input)
        object.__setattr__(self, "target", frozen_target)
        if not isinstance(self.tags, tuple) or len(self.tags) > MAX_TAGS:
            raise EvaluationCaseManifestError(f"case tags must be a tuple with at most {MAX_TAGS} entries")
        tags = tuple(_text(tag, "case tag", identifier=True) for tag in self.tags)
        if tags != tuple(sorted(tags)) or len(set(tags)) != len(tags):
            raise EvaluationCaseManifestError("case tags must be unique and sorted")
        object.__setattr__(self, "tags", tags)
        if len(_canonical(self.as_dict(include_digest=False)).encode("utf-8")) > MAX_CASE_BYTES:
            raise EvaluationCaseManifestError("case exceeds the encoded byte bound")

    @property
    def digest(self) -> str:
        return _digest(self.as_dict(include_digest=False))

    def as_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result = {
            "case_id": self.case_id,
            "input": _thaw(self.input),
            "target": _thaw(self.target),
            "grader": self.grader.as_dict(),
            "provenance": self.provenance.as_dict(),
            "tags": list(self.tags),
        }
        if include_digest:
            result["case_digest"] = self.digest
        return result

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvaluationCase":
        if not isinstance(payload, Mapping):
            raise EvaluationCaseManifestError("evaluation case must be an object")
        _keys(payload, allowed={"case_id", "input", "target", "grader", "provenance", "tags", "case_digest"},
              required={"case_id", "input", "target", "grader", "provenance"}, label="evaluation case")
        raw_tags = payload.get("tags", [])
        if not isinstance(raw_tags, list):
            raise EvaluationCaseManifestError("case tags must be an array")
        case = cls(
            payload["case_id"], payload["input"], payload["target"],
            EvaluationCaseGrader.from_dict(payload["grader"]),
            EvaluationCaseProvenance.from_dict(payload["provenance"]), tuple(raw_tags),
        )
        supplied = payload.get("case_digest")
        if supplied is not None and supplied != case.digest:
            raise EvaluationCaseManifestError("case digest mismatch")
        return case


def _suite_from_dict(payload: Mapping[str, Any]) -> EvaluationSuite:
    if not isinstance(payload, Mapping):
        raise EvaluationCaseManifestError("manifest suite must be an object")
    _keys(payload, allowed={"schema", "suite_id", "version", "dimensions", "metric_names", "suite_digest"},
          required={"schema", "suite_id", "version", "dimensions", "metric_names", "suite_digest"},
          label="manifest suite")
    dimensions = payload["dimensions"]
    metrics = payload["metric_names"]
    if not isinstance(dimensions, list) or not isinstance(metrics, list):
        raise EvaluationCaseManifestError("suite dimensions and metric_names must be arrays")
    try:
        parsed: list[EvaluationDimension] = []
        for item in dimensions:
            if not isinstance(item, Mapping):
                raise EvaluationCaseManifestError("suite dimension must be an object")
            _keys(item, allowed={"name", "value"}, required={"name", "value"},
                  label="suite dimension")
            parsed.append(EvaluationDimension(item["name"], item["value"]))
        parsed_dimensions = tuple(parsed)
        suite = EvaluationSuite(payload["suite_id"], payload["version"], parsed_dimensions,
                                tuple(metrics), schema=payload["schema"])
    except EvaluationCaseManifestError:
        raise
    except (EvaluationLifecycleError, KeyError, TypeError) as exc:
        raise EvaluationCaseManifestError("manifest suite is malformed") from exc
    if payload["suite_digest"] != suite.digest:
        raise EvaluationCaseManifestError("suite digest mismatch")
    return suite


@dataclass(frozen=True)
class EvaluationCaseManifest:
    """Versioned suite plus a deterministic, bounded set of concrete cases."""

    suite: EvaluationSuite
    cases: tuple[EvaluationCase, ...]
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SCHEMA:
            raise EvaluationCaseManifestError(f"unsupported case manifest schema: {self.schema}")
        if not isinstance(self.suite, EvaluationSuite):
            raise EvaluationCaseManifestError("manifest suite is required")
        if not isinstance(self.cases, tuple) or not 1 <= len(self.cases) <= MAX_CASES:
            raise EvaluationCaseManifestError(f"manifest must contain 1..{MAX_CASES} cases")
        if any(not isinstance(case, EvaluationCase) for case in self.cases):
            raise EvaluationCaseManifestError("manifest contains an invalid case")
        ids = tuple(case.case_id for case in self.cases)
        if ids != tuple(sorted(ids)) or len(set(ids)) != len(ids):
            raise EvaluationCaseManifestError("manifest case_ids must be unique and sorted")
        if len(_canonical(self.as_dict(include_digest=False)).encode("utf-8")) > MAX_MANIFEST_BYTES:
            raise EvaluationCaseManifestError("manifest exceeds the encoded byte bound")

    @property
    def digest(self) -> str:
        return _digest(self.as_dict(include_digest=False))

    def as_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result = {
            "schema": self.schema,
            "suite": self.suite.as_dict(),
            "cases": [case.as_dict() for case in self.cases],
        }
        if include_digest:
            result["manifest_digest"] = self.digest
        return result

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvaluationCaseManifest":
        if not isinstance(payload, Mapping):
            raise EvaluationCaseManifestError("case manifest must be an object")
        _keys(payload, allowed={"schema", "suite", "cases", "manifest_digest"},
              required={"schema", "suite", "cases"}, label="case manifest")
        raw_cases = payload["cases"]
        if not isinstance(raw_cases, list):
            raise EvaluationCaseManifestError("manifest cases must be an array")
        manifest = cls(_suite_from_dict(payload["suite"]),
                       tuple(EvaluationCase.from_dict(item) for item in raw_cases),
                       schema=payload["schema"])
        supplied = payload.get("manifest_digest")
        if supplied is not None and supplied != manifest.digest:
            raise EvaluationCaseManifestError("manifest digest mismatch")
        return manifest


@dataclass(frozen=True)
class EvaluationManifestDiagnostics:
    """Content-free preflight result safe to print in operator diagnostics."""

    manifest_digest: str
    case_count: int
    deterministic_cases: int
    advisory_cases: int
    unavailable_deterministic: tuple[str, ...]
    unavailable_advisory: tuple[str, ...]

    @property
    def runnable(self) -> bool:
        return not self.unavailable_deterministic and not self.unavailable_advisory

    @property
    def gate_ready(self) -> bool:
        return self.deterministic_cases > 0 and not self.unavailable_deterministic

    @property
    def warnings(self) -> tuple[str, ...]:
        warnings: list[str] = []
        if self.deterministic_cases == 0:
            warnings.append("manifest has no deterministic promotion-gate cases")
        if self.unavailable_deterministic:
            warnings.append("deterministic verifier implementations are unavailable")
        if self.unavailable_advisory:
            warnings.append("advisory verifier implementations are unavailable")
        return tuple(warnings)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "sonder.evaluation-case-diagnostics.v1",
            "manifest_digest": self.manifest_digest,
            "case_count": self.case_count,
            "deterministic_cases": self.deterministic_cases,
            "advisory_cases": self.advisory_cases,
            "unavailable_deterministic": list(self.unavailable_deterministic),
            "unavailable_advisory": list(self.unavailable_advisory),
            "runnable": self.runnable,
            "gate_ready": self.gate_ready,
            "warnings": list(self.warnings),
        }


def inspect_manifest(
    manifest: EvaluationCaseManifest,
    available_verifiers: Iterable[str],
) -> EvaluationManifestDiagnostics:
    """Compare grader references with an adapter-supplied verifier catalog."""
    if not isinstance(manifest, EvaluationCaseManifest):
        raise EvaluationCaseManifestError("a parsed evaluation case manifest is required")
    try:
        available = frozenset(_text(item, "available verifier", identifier=True) for item in available_verifiers)
    except TypeError as exc:
        raise EvaluationCaseManifestError("available_verifiers must be iterable") from exc
    deterministic = tuple(case for case in manifest.cases if case.grader.deterministic)
    advisory = tuple(case for case in manifest.cases if not case.grader.deterministic)
    unavailable_deterministic = tuple(sorted({case.grader.verifier for case in deterministic
                                               if case.grader.verifier not in available}))
    unavailable_advisory = tuple(sorted({case.grader.verifier for case in advisory
                                         if case.grader.verifier not in available}))
    return EvaluationManifestDiagnostics(
        manifest.digest, len(manifest.cases), len(deterministic), len(advisory),
        unavailable_deterministic, unavailable_advisory,
    )


def load_manifest(path: str | Path, *, max_bytes: int = MAX_MANIFEST_BYTES) -> EvaluationCaseManifest:
    """Load one local JSON manifest without executing or resolving its contents."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= MAX_MANIFEST_BYTES:
        raise EvaluationCaseManifestError("max_bytes is outside the supported bound")
    manifest_path = Path(path)
    try:
        # Bound the read itself rather than trusting a prior stat; a file can
        # grow between those operations and force an unbounded allocation.
        with manifest_path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise EvaluationCaseManifestError("case manifest exceeds the file byte bound")
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_object_without_duplicates)
    except EvaluationCaseManifestError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise EvaluationCaseManifestError("case manifest is unreadable") from exc
    return EvaluationCaseManifest.from_dict(payload)


__all__ = [
    "EvaluationCase", "EvaluationCaseGrader", "EvaluationCaseManifest",
    "EvaluationCaseManifestError", "EvaluationCaseProvenance",
    "EvaluationManifestDiagnostics", "MAX_CASES", "MAX_CASE_BYTES",
    "MAX_MANIFEST_BYTES", "MODEL_GRADED_VERIFIERS", "SCHEMA",
    "inspect_manifest", "load_manifest",
]
