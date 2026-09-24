"""Recent, provider-neutral evidence used to admit model routes.

Profiles describe what a model is expected to support.  This module records
what a local backend actually passed recently, so a stale or failed probe
cannot silently become a routing decision.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from .capability_profiles import Capability


class BackendCapability(str, Enum):
    CHAT = "chat"
    STRUCTURED = "structured"
    CANCELLATION = "cancellation"
    TOOL_NATIVE = "tool_native"
    TOOL_FALLBACK = "tool_fallback"
    TOOL_SEQUENTIAL = "tool_sequential"
    TOOL_PARALLEL = "tool_parallel"
    TOOL_CONTINUATION = "tool_continuation"
    RESUME = "resume"
    PREFIX_CACHE = "prefix_cache"
    LONG_CONTEXT = "long_context"
    SUBAGENT = "subagent"
    REASONING = "reasoning"
    CODING = "coding"
    CODING_CPP = "coding_cpp"
    CODING_CSHARP = "coding_csharp"
    CODING_PYTHON = "coding_python"
    ARCHITECTURE = "architecture"
    DEBUGGING = "debugging"
    REVERSE_ENGINEERING = "reverse_engineering"
    CRITIC = "critic"
    SUMMARIZATION = "summarization"
    VISION = "vision"
    EMBEDDING = "embedding"


class EvidenceState(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    UNKNOWN = "unknown"
    STALE = "stale"


class EvidenceReason(str, Enum):
    ELIGIBLE = "recent_capability_evidence_passed"
    MISSING = "recent_capability_evidence_missing"
    STALE = "recent_capability_evidence_stale"
    FAILED = "recent_capability_probe_failed"
    SYNTHETIC = "synthetic_capability_evidence"
    FUTURE = "future_capability_evidence"
    IDENTITY_MISSING = "backend_identity_missing"
    IDENTITY_CHANGED = "backend_identity_changed"


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class BackendIdentity:
    """Trusted host observation of one concrete deployment at probe/admission.

    These fields identify a tested execution configuration; their presence is
    never by itself evidence that inference or tool calls succeeded.
    """

    backend: str
    model: str
    model_digest: str
    quantization: str
    backend_version: str
    tokenizer_digest: str
    template_digest: str
    context_tokens: int
    hardware: str

    def __post_init__(self) -> None:
        for field in ("backend", "model", "quantization", "backend_version", "hardware"):
            value = getattr(self, field)
            if type(value) is not str or not value.strip() or len(value) > 256:
                raise ValueError("backend identity requires bounded host-observed fields")
        if any(type(getattr(self, field)) is not str or not _DIGEST.fullmatch(getattr(self, field))
               for field in ("model_digest", "tokenizer_digest", "template_digest")):
            raise ValueError("backend identity requires exact SHA-256 digests")
        if type(self.context_tokens) is not int or self.context_tokens <= 0:
            raise ValueError("backend identity requires a positive context window")

    def to_dict(self) -> dict[str, str | int]:
        return {field: getattr(self, field) for field in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> BackendIdentity:
        if (not isinstance(value, Mapping)
                or set(value) != set(cls.__dataclass_fields__)):
            raise ValueError("exact backend identity is required")
        return cls(**{field: value[field] for field in cls.__dataclass_fields__})


@dataclass(frozen=True)
class EvidenceVerdict:
    state: EvidenceState
    reason_code: str
    capabilities: Mapping[BackendCapability, EvidenceState]

    def __post_init__(self) -> None:
        object.__setattr__(self, "capabilities", MappingProxyType(dict(self.capabilities)))


@dataclass(frozen=True)
class ProbeResult:
    capability: BackendCapability
    passed: bool | None
    reason_code: str

    def __post_init__(self) -> None:
        if (not isinstance(self.capability, BackendCapability)
                or self.passed is not None and type(self.passed) is not bool
                or type(self.reason_code) is not str
                or not 1 <= len(self.reason_code) <= 128):
            raise ValueError("invalid conformance probe result")


@dataclass(frozen=True)
class BackendConformanceRecord:
    backend: str
    model: str
    checked_at: float
    results: tuple[ProbeResult, ...]
    probe_version: int = 1
    synthetic: bool = False
    identity: BackendIdentity | None = None

    def __post_init__(self) -> None:
        if not self.backend.strip() or not self.model.strip():
            raise ValueError("backend and model are required")
        if not math.isfinite(self.checked_at) or self.checked_at < 0 or self.probe_version <= 0:
            raise ValueError("invalid conformance record metadata")
        if len(self.results) > len(BackendCapability):
            raise ValueError("conformance record contains too many probe results")
        if len({result.capability for result in self.results}) != len(self.results):
            raise ValueError("conformance record contains duplicate capabilities")
        if self.identity is not None and (
            not isinstance(self.identity, BackendIdentity)
            or self.identity.backend != self.backend or self.identity.model != self.model
        ):
            raise ValueError("conformance identity differs from the probed route")

    @property
    def passed(self) -> frozenset[BackendCapability]:
        return frozenset(result.capability for result in self.results if result.passed is True)

    @property
    def failed(self) -> frozenset[BackendCapability]:
        return frozenset(result.capability for result in self.results if result.passed is False)

    @property
    def unknown(self) -> frozenset[BackendCapability]:
        return frozenset(result.capability for result in self.results if result.passed is None)

    def to_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "model": self.model,
            "checked_at": self.checked_at,
            "probe_version": self.probe_version,
            "synthetic": self.synthetic,
            "identity": self.identity.to_dict() if self.identity else None,
            "results": [
                {"capability": item.capability.value, "passed": item.passed,
                 "reason_code": item.reason_code}
                for item in self.results
            ],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> BackendConformanceRecord:
        raw = value.get("results")
        if not isinstance(raw, list):
            raise ValueError("conformance results must be an array")  # noqa: TRY004 - persisted data contract
        results = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise ValueError("conformance result must be an object")  # noqa: TRY004 - persisted data contract
            passed = item.get("passed")
            if passed is not None and type(passed) is not bool:
                raise ValueError("conformance result requires a boolean or unknown state")
            results.append(ProbeResult(
                BackendCapability(item.get("capability", "")),
                passed,
                str(item.get("reason_code", "")),
            ))
        raw_identity = value.get("identity")
        identity = BackendIdentity.from_dict(raw_identity) if raw_identity is not None else None
        return cls(
            backend=str(value.get("backend", "")), model=str(value.get("model", "")),
            checked_at=float(value.get("checked_at", -1)), results=tuple(results),
            probe_version=int(value.get("probe_version", 1)),
            synthetic=value.get("synthetic") is True,
            identity=identity,
        )


def backend_requirements(required: frozenset[Capability], *, measured: bool = False) -> frozenset[BackendCapability]:
    """Translate semantic route requirements into provider smoke requirements."""
    requirements = {BackendCapability.CHAT}
    if Capability.STRUCTURED in required:
        requirements.add(BackendCapability.STRUCTURED)
    if measured:
        semantic = {
            Capability.PLAN: BackendCapability.REASONING,
            Capability.EDIT: BackendCapability.CODING,
            Capability.TOOLS: BackendCapability.TOOL_CONTINUATION,
            Capability.VERIFY: BackendCapability.CRITIC,
            Capability.SUMMARIZE: BackendCapability.SUMMARIZATION,
            Capability.EMBED: BackendCapability.EMBEDDING,
            Capability.VISION: BackendCapability.VISION,
        }
        requirements.update(semantic[capability] for capability in required if capability in semantic)
        if Capability.TOOLS in required:
            requirements.add(BackendCapability.TOOL_SEQUENTIAL)
    return frozenset(requirements)


__all__ = [
    "BackendCapability", "BackendConformanceRecord", "BackendIdentity", "EvidenceReason",
    "EvidenceState", "EvidenceVerdict",
    "ProbeResult", "backend_requirements",
]
