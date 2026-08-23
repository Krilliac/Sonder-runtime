"""Bounded JSON fixtures and deterministic local providers for evaluation.

Nothing in this adapter opens a network connection or discovers a model.  A
provider fixture is an explicit request-to-result table, useful for harness
validation, CI regression cases, and deterministic trace replay.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from sonder_runtime.application.evaluation.reproducible import (
    EvaluationMatrixReport,
    EvaluationRunReport,
    EvaluationScenario,
    ProviderFailure,
    ProviderIdentity,
    ProviderResponse,
    ReproducibleEvaluationError,
)


PROVIDER_SCHEMA = "sonder.deterministic-provider.v1"
MAX_FIXTURE_BYTES = 2 * 1024 * 1024
MAX_PROVIDER_RESPONSES = 1_024
MAX_REPORT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class ScriptedProviderResult:
    kind: str
    output: Any = None
    latency_ms: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    error_code: str = ""
    error_message: str = ""


class DeterministicLocalProvider:
    """A side-effect-free exact-request provider with scripted outcomes."""

    def __init__(self, identity: ProviderIdentity, responses: Mapping[str, ScriptedProviderResult]) -> None:
        self.identity = identity
        if not responses or len(responses) > MAX_PROVIDER_RESPONSES:
            raise ReproducibleEvaluationError("deterministic provider responses are empty or unbounded")
        self._responses = dict(responses)

    def invoke(self, request: Any, *, timeout_ms: int) -> ProviderResponse:
        key = _canonical(request)
        scripted = self._responses.get(key)
        if scripted is None:
            raise ProviderFailure("unmapped_request", "request has no deterministic fixture response")
        if scripted.kind == "timeout":
            raise TimeoutError(f"scripted deadline exceeded after {timeout_ms} ms")
        if scripted.kind == "error":
            raise ProviderFailure(scripted.error_code or "protocol", scripted.error_message or "scripted provider error")
        if scripted.kind == "invalid_response":
            return scripted.output  # type: ignore[return-value] - intentional protocol fixture
        return ProviderResponse(scripted.output, scripted.latency_ms, scripted.tokens_in, scripted.tokens_out)


def load_scenario_fixture(path: str | Path) -> EvaluationScenario:
    """Load and digest-check one bounded golden scenario fixture."""
    payload = _read_json(path, "scenario fixture")
    if "scenario_digest" not in payload:
        raise ReproducibleEvaluationError("scenario fixture requires a digest")
    return EvaluationScenario.from_dict(payload)


def load_provider_fixture(path: str | Path) -> DeterministicLocalProvider:
    """Load a bounded deterministic provider fixture and bind its identity."""
    payload = _read_json(path, "provider fixture")
    allowed = {"schema", "provider_id", "model_id", "revision", "responses", "provider_digest"}
    _reject_unknown(payload, allowed, "provider fixture")
    if payload.get("schema") != PROVIDER_SCHEMA:
        raise ReproducibleEvaluationError("unsupported provider fixture schema")
    raw_responses = payload.get("responses")
    if not isinstance(raw_responses, list) or not 1 <= len(raw_responses) <= MAX_PROVIDER_RESPONSES:
        raise ReproducibleEvaluationError("provider fixture responses are empty or unbounded")
    unsigned = dict(payload)
    supplied_digest = unsigned.pop("provider_digest", None)
    fixture_digest = _digest(unsigned)
    if supplied_digest != fixture_digest:
        raise ReproducibleEvaluationError("provider fixture digest mismatch")
    try:
        identity = ProviderIdentity(
            payload["provider_id"], payload["model_id"], payload["revision"], fixture_digest,
        )
    except KeyError as exc:
        raise ReproducibleEvaluationError("provider fixture identity is incomplete") from exc
    responses: dict[str, ScriptedProviderResult] = {}
    for index, item in enumerate(raw_responses):
        if not isinstance(item, Mapping):
            raise ReproducibleEvaluationError(f"provider response {index} must be an object")
        _reject_unknown(
            item,
            {"input", "kind", "output", "latency_ms", "tokens_in", "tokens_out", "error_code", "error_message"},
            f"provider response {index}",
        )
        if "input" not in item:
            raise ReproducibleEvaluationError(f"provider response {index} has no input")
        kind = item.get("kind", "response")
        if kind not in {"response", "timeout", "error", "invalid_response"}:
            raise ReproducibleEvaluationError(f"provider response {index} kind is unsupported")
        result = ScriptedProviderResult(
            kind=kind, output=item.get("output"), latency_ms=item.get("latency_ms", 0.0),
            tokens_in=item.get("tokens_in", 0), tokens_out=item.get("tokens_out", 0),
            error_code=item.get("error_code", ""), error_message=item.get("error_message", ""),
        )
        # Reuse ProviderResponse validation for successful response metadata.
        if kind == "response":
            ProviderResponse(result.output, result.latency_ms, result.tokens_in, result.tokens_out)
        key = _canonical(item["input"])
        if key in responses:
            raise ReproducibleEvaluationError("provider fixture contains duplicate inputs")
        responses[key] = result
    return DeterministicLocalProvider(identity, responses)


class JsonEvaluationRunRepository:
    """Explicit, atomic persistence for a raw replay-capable run report."""

    def __init__(self, path: str | Path, *, max_bytes: int = MAX_REPORT_BYTES) -> None:
        self.path = Path(path)
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_REPORT_BYTES:
            raise ReproducibleEvaluationError("report max_bytes is outside the bounded range")
        self._max_bytes = max_bytes

    def save(self, report: EvaluationRunReport) -> None:
        encoded = (json.dumps(report.as_dict(), indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        _atomic_write(self.path, encoded, self._max_bytes)

    def load(self) -> EvaluationRunReport:
        return EvaluationRunReport.from_dict(_read_json(self.path, "evaluation run", max_bytes=self._max_bytes))


class JsonEvaluationMatrixRepository:
    """Atomic, digest-verifying persistence for provider/model matrices."""

    def __init__(self, path: str | Path, *, max_bytes: int = MAX_REPORT_BYTES) -> None:
        self.path = Path(path)
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_REPORT_BYTES:
            raise ReproducibleEvaluationError("matrix max_bytes is outside the bounded range")
        self._max_bytes = max_bytes

    def save(self, report: EvaluationMatrixReport) -> None:
        encoded = (json.dumps(report.as_dict(), indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        _atomic_write(self.path, encoded, self._max_bytes)

    def load(self) -> EvaluationMatrixReport:
        return EvaluationMatrixReport.from_dict(_read_json(self.path, "evaluation matrix", max_bytes=self._max_bytes))


def provider_fixture_digest(payload: Mapping[str, Any]) -> str:
    """Return the digest to place in a provider fixture during authoring."""
    unsigned = dict(payload)
    unsigned.pop("provider_digest", None)
    return _digest(unsigned)


def _read_json(path: str | Path, label: str, *, max_bytes: int = MAX_FIXTURE_BYTES) -> Mapping[str, Any]:
    path = Path(path)
    try:
        size = path.stat().st_size
        if not 1 <= size <= max_bytes:
            raise ReproducibleEvaluationError(f"{label} is empty or exceeds byte bound")
        value = json.loads(path.read_text(encoding="utf-8"))
    except ReproducibleEvaluationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ReproducibleEvaluationError(f"{label} is unreadable") from exc
    if not isinstance(value, Mapping):
        raise ReproducibleEvaluationError(f"{label} must contain a JSON object")
    return value


def _atomic_write(path: Path, encoded: bytes, max_bytes: int) -> None:
    if not 1 <= len(encoded) <= max_bytes:
        raise ReproducibleEvaluationError("evaluation report is empty or exceeds byte bound")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ReproducibleEvaluationError("provider fixture values must be JSON-compatible") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise ReproducibleEvaluationError(f"{label} contains unsupported fields: {', '.join(unknown)}")


__all__ = [
    "DeterministicLocalProvider", "JsonEvaluationMatrixRepository",
    "JsonEvaluationRunRepository", "PROVIDER_SCHEMA",
    "ScriptedProviderResult", "load_provider_fixture", "load_scenario_fixture",
    "provider_fixture_digest",
]
