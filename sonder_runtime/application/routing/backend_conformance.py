"""Deterministic local backend probes and bounded recent evidence storage."""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from ...application.context import local_owner_context
from ...application.ports.model_gateway import ModelRequest
from ...domain.common.errors import Cancelled
from ...domain.routing.backend_conformance import (
    BackendCapability,
    BackendConformanceRecord,
    BackendIdentity,
    EvidenceReason,
    EvidenceState,
    EvidenceVerdict,
    ProbeResult,
    backend_requirements,
)
from ...domain.routing.capability_profiles import Capability


class SmokeProvider(Protocol):
    def chat(self, prompt: str) -> str: ...
    def structured_tool_continue(self, prompt: str) -> dict: ...
    def cancel(self) -> bool: ...


class DeterministicFakeProvider:
    """Offline provider used by smoke tests and operator preflight checks."""
    def chat(self, prompt: str) -> str:
        return "ok:" + prompt

    def structured_tool_continue(self, prompt: str) -> dict:
        return {"tool": "echo", "arguments": {"prompt": prompt}, "continued": True}

    def cancel(self) -> bool:
        return True


def run_smoke_probes(provider: SmokeProvider, *, backend: str, model: str,
                     now: float | None = None, synthetic: bool = True) -> BackendConformanceRecord:
    """Run bounded, deterministic probes without contacting a live model."""
    if synthetic is not True:
        raise ValueError("offline smoke probes cannot certify live inference")
    checked_at = time.time() if now is None else float(now)
    results = []
    try:
        passed = provider.chat("sonder conformance plain chat") == "ok:sonder conformance plain chat"
        results.append(ProbeResult(BackendCapability.CHAT, passed, "plain_chat_passed" if passed else "plain_chat_mismatch"))
    except Exception:  # noqa: BLE001 - an external probe failure is recorded, never propagated
        results.append(ProbeResult(BackendCapability.CHAT, False, "plain_chat_error"))
    try:
        value = provider.structured_tool_continue("sonder conformance structured")
        passed = isinstance(value, dict) and value.get("continued") is True and value.get("tool")
        results.append(ProbeResult(BackendCapability.STRUCTURED, bool(passed), "structured_tool_continuation_passed" if passed else "structured_tool_continuation_invalid"))
    except Exception:  # noqa: BLE001 - external probe failure
        results.append(ProbeResult(BackendCapability.STRUCTURED, False, "structured_tool_continuation_error"))
    try:
        passed = provider.cancel() is True
        results.append(ProbeResult(BackendCapability.CANCELLATION, passed, "cancellation_passed" if passed else "cancellation_not_supported"))
    except (AttributeError, NotImplementedError):
        results.append(ProbeResult(BackendCapability.CANCELLATION, False, "cancellation_not_supported"))
    except Exception:  # noqa: BLE001 - external probe failure
        results.append(ProbeResult(BackendCapability.CANCELLATION, False, "cancellation_error"))
    return BackendConformanceRecord(backend, model, checked_at, tuple(results), synthetic=True)


def run_gateway_probes(
    gateway,
    *,
    backend: str,
    model: str,
    now: float | None = None,
    timeout_seconds: float = 30.0,
    cloud_allowed: bool = False,
    remote_ollama_allowed: bool = False,
    identity: BackendIdentity | None = None,
    synthetic: bool = True,
) -> BackendConformanceRecord:
    """Probe one concrete ``ModelGateway`` route; default to synthetic evidence.

    The probe uses fixed, non-sensitive prompts and records only outcome codes;
    provider text is never persisted. Only a trusted host with an actual live
    backend may set ``synthetic=False``. The structured check requires a small
    JSON response from the requested model, while cancellation is checked
    with a pre-cancelled context so an implementation cannot pass by merely
    returning a plausible cancellation flag.  ``timeout_seconds`` is bounded
    to keep an operator or nightly preflight from becoming an unbounded model
    call.
    """
    if not 0.0 < float(timeout_seconds) <= 300.0:
        raise ValueError("timeout_seconds must be between 0 and 300")
    checked_at = time.time() if now is None else float(now)
    results: list[ProbeResult] = []

    def context(*, cancellation=None):
        return local_owner_context(
            correlation_id="backend-conformance",
            timeout_seconds=float(timeout_seconds),
            cancellation=cancellation,
            cloud_allowed=cloud_allowed,
            remote_ollama_allowed=remote_ollama_allowed,
        )

    try:
        response = gateway.generate(
            ModelRequest(prompt="sonder conformance plain chat", tier=model),
            context(),
        )
        actual_model = getattr(response, "model", None)
        passed = (actual_model == model and isinstance(getattr(response, "text", None), str)
                  and bool(response.text.strip()))
        results.append(ProbeResult(
            BackendCapability.CHAT,
            passed,
            "plain_chat_passed" if passed else (
                "plain_chat_model_mismatch" if actual_model != model else "plain_chat_empty"
            ),
        ))
    except Exception:  # noqa: BLE001 - external gateway failure
        results.append(ProbeResult(BackendCapability.CHAT, False, "plain_chat_error"))

    try:
        response = gateway.generate(
            ModelRequest(
                prompt=(
                    'Return only JSON: {"tool":"echo","continued":true}. '
                    "Do not add prose."
                ),
                tier=model,
            ),
            context(),
        )
        value = json.loads(response.text) if isinstance(response.text, str) else None
        actual_model = getattr(response, "model", None)
        passed = (actual_model == model and isinstance(value, dict)
                  and value.get("continued") is True and bool(value.get("tool")))
        results.append(ProbeResult(
            BackendCapability.STRUCTURED,
            passed,
            "structured_json_passed" if passed else (
                "structured_json_model_mismatch" if actual_model != model
                else "structured_json_invalid"
            ),
        ))
    except Exception:  # noqa: BLE001 - external gateway failure
        results.append(ProbeResult(BackendCapability.STRUCTURED, False, "structured_json_error"))

    class _Cancelled:
        cancelled = True

        def wait(self, timeout=None):
            return True

    try:
        gateway.generate(
            ModelRequest(prompt="sonder conformance cancellation", tier=model),
            context(cancellation=_Cancelled()),
        )
    except Cancelled:
        results.append(ProbeResult(BackendCapability.CANCELLATION, True, "cancellation_passed"))
    except Exception:  # noqa: BLE001 - external gateway failure
        results.append(ProbeResult(BackendCapability.CANCELLATION, False, "cancellation_wrong_error"))
    else:
        results.append(ProbeResult(BackendCapability.CANCELLATION, False, "cancellation_not_supported"))
    return BackendConformanceRecord(
        backend, model, checked_at, tuple(results), synthetic=synthetic, identity=identity,
    )


class LiveProtocolProvider(Protocol):
    """Host-owned active probe; absent operations report unknown evidence."""

    def run_case(self, capability: BackendCapability, *, model: str,
                 timeout_seconds: float) -> Mapping[str, object] | None: ...


PROTOCOL_CASES = (
    BackendCapability.STRUCTURED,
    BackendCapability.CANCELLATION,
    BackendCapability.TOOL_NATIVE,
    BackendCapability.TOOL_FALLBACK,
    BackendCapability.TOOL_SEQUENTIAL,
    BackendCapability.TOOL_PARALLEL,
    BackendCapability.TOOL_CONTINUATION,
    BackendCapability.RESUME,
    BackendCapability.PREFIX_CACHE,
    BackendCapability.LONG_CONTEXT,
    BackendCapability.SUBAGENT,
    BackendCapability.VISION,
)


def _protocol_passed(capability: BackendCapability, value: Mapping[str, object],
                     identity: BackendIdentity) -> bool:
    events = value.get("events")
    if capability is BackendCapability.STRUCTURED:
        return value.get("schema_valid") is True and value.get("response_kind") == "object"
    if capability is BackendCapability.CANCELLATION:
        return value.get("cancelled") is True and value.get("effect_stopped") is True
    if capability is BackendCapability.TOOL_NATIVE:
        return value.get("tool_calls") == ["echo"] and value.get("schema_valid") is True
    if capability is BackendCapability.TOOL_FALLBACK:
        return value.get("fallback_calls") == ["echo"] and value.get("allowlisted") is True
    if capability is BackendCapability.TOOL_SEQUENTIAL:
        return events == ["call:a", "result:a", "call:b", "result:b"]
    if capability is BackendCapability.TOOL_PARALLEL:
        return (events == ["call:a", "call:b", "result:a", "result:b"]
                and type(value.get("parallel_max")) is int and value["parallel_max"] >= 2)
    if capability is BackendCapability.TOOL_CONTINUATION:
        return events == ["call:echo", "result:echo", "assistant:done"]
    if capability is BackendCapability.RESUME:
        return value.get("resume_context_bound") is True and value.get("continued") is True
    if capability is BackendCapability.PREFIX_CACHE:
        return (value.get("prefix_hash_equal") is True
                and type(value.get("cached_tokens")) is int and value["cached_tokens"] > 0
                and value.get("changed_prefix_miss") is True)
    if capability is BackendCapability.LONG_CONTEXT:
        return (type(value.get("input_tokens")) is int
                and 8192 <= value["input_tokens"] <= identity.context_tokens
                and value.get("start_marker") is True
                and value.get("end_marker") is True)
    if capability is BackendCapability.SUBAGENT:
        return (value.get("child_scope_unique") is True
                and value.get("parent_resumed") is True
                and value.get("budget_enforced") is True)
    if capability is BackendCapability.VISION:
        return value.get("image_bound") is True and value.get("answer_verified") is True
    return False


def run_protocol_probes(
    provider: LiveProtocolProvider, *, identity: BackendIdentity,
    now: float | None = None, timeout_seconds: float = 30.0,
    cases: tuple[BackendCapability, ...] = PROTOCOL_CASES,
    synthetic: bool = True,
) -> BackendConformanceRecord:
    """Validate bounded host-observed protocol traces; unimplemented cases stay unknown.

    A supplied fake remains synthetic unless the host explicitly declares an
    actual live probe. No model name, metadata or unattempted case certifies a
    capability; unknown categories are not exported as passing results.
    """
    if not isinstance(identity, BackendIdentity):
        raise TypeError("a host-observed backend identity is required")
    if not 0.0 < float(timeout_seconds) <= 300.0:
        raise ValueError("timeout_seconds must be between 0 and 300")
    if len(cases) > len(PROTOCOL_CASES) or len(set(cases)) != len(cases) or any(
        case not in PROTOCOL_CASES for case in cases
    ):
        raise ValueError("unknown or duplicate conformance protocol case")
    results = []
    for capability in cases:
        try:
            observed = provider.run_case(
                capability, model=identity.model, timeout_seconds=float(timeout_seconds),
            )
        except (AttributeError, NotImplementedError):
            observed = None
        except Exception:  # noqa: BLE001 - a host adapter can fail in any way
            results.append(ProbeResult(capability, False, "live_protocol_error"))
            continue
        if observed is None:
            results.append(ProbeResult(capability, None, "live_protocol_unavailable"))
        elif not isinstance(observed, Mapping) or observed.get("model") != identity.model:
            results.append(ProbeResult(capability, False, "live_protocol_model_mismatch"))
        else:
            passed = _protocol_passed(capability, observed, identity)
            results.append(ProbeResult(
                capability, passed,
                "live_protocol_passed" if passed else "live_protocol_invalid",
            ))
    return BackendConformanceRecord(
        identity.backend, identity.model,
        time.time() if now is None else float(now), tuple(results),
        probe_version=2, synthetic=synthetic, identity=identity,
    )


class RecentCapabilityEvidence:
    """Atomic JSON record store with a bounded set of backend/model records."""
    schema = 1
    MAX_STORE_BYTES = 1024 * 1024
    MAX_STORED_RECORDS = 64

    def __init__(self, path: str | os.PathLike[str], *, max_age_seconds: float = 86_400,
                 max_records: int = 32):
        if not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")
        self.path = Path(path)
        self.max_age_seconds = float(max_age_seconds)
        if type(max_records) is not int or not 1 <= max_records <= self.MAX_STORED_RECORDS:
            raise ValueError("max_records must fit the bounded evidence store")
        self.max_records = int(max_records)
        self._lock = threading.RLock()

    def save(self, record: BackendConformanceRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            records = self._read_records()
            records[self._key(record.backend, record.model)] = record.to_dict()
            records = dict(sorted(records.items(), key=lambda item: float(item[1].get("checked_at", 0)), reverse=True)[:self.max_records])
            payload = {"schema": self.schema, "records": records}
            encoded = json.dumps(payload, sort_keys=True, allow_nan=False)
            if len(encoded.encode("utf-8")) > self.MAX_STORE_BYTES:
                raise ValueError("backend evidence store exceeds size bounds")
            fd, temp_name = tempfile.mkstemp(prefix=self.path.name + ".", dir=str(self.path.parent), text=True)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, self.path)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)

    def load(self, backend: str, model: str) -> BackendConformanceRecord | None:
        try:
            value = self._read_records().get(self._key(backend, model))
            return BackendConformanceRecord.from_dict(value) if isinstance(value, dict) else None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def check(self, model: str, required: frozenset[Capability], *, backend: str = "local",
              now: float | None = None, identity: BackendIdentity | None = None):
        if identity is not None:
            verdict = self.assess(
                model, backend_requirements(required, measured=True),
                backend=backend, identity=identity, now=now,
            )
            return verdict.state is EvidenceState.PASSED, verdict.reason_code
        record = self.load(backend, model)
        if record is None:
            return False, EvidenceReason.MISSING.value
        current = time.time() if now is None else float(now)
        if not math.isfinite(current):
            return False, EvidenceReason.MISSING.value
        if record.synthetic:
            return False, EvidenceReason.SYNTHETIC.value
        if record.checked_at > current:
            return False, EvidenceReason.FUTURE.value
        if current - record.checked_at > self.max_age_seconds:
            return False, EvidenceReason.STALE.value
        needed = backend_requirements(required)
        if not needed.issubset(record.passed):
            if needed & record.failed:
                return False, EvidenceReason.FAILED.value
            return False, EvidenceReason.MISSING.value
        return True, EvidenceReason.ELIGIBLE.value

    def assess(self, model: str, required: frozenset[BackendCapability], *,
               backend: str, identity: BackendIdentity | None,
               now: float | None = None,
               any_of: tuple[frozenset[BackendCapability], ...] = ()) -> EvidenceVerdict:
        """Admit only exact recent identity-bound measured capabilities."""
        necessary = frozenset(required) | {BackendCapability.CHAT}
        needed = necessary.union(*(group for group in any_of))
        if any(not isinstance(capability, BackendCapability) for capability in needed):
            raise ValueError("exact backend capability requirements are required")
        if any(not group for group in any_of):
            raise ValueError("alternative capability groups cannot be empty")

        def verdict(state: EvidenceState, reason: EvidenceReason):
            return EvidenceVerdict(state, reason.value, {key: state for key in needed})

        if (identity is None or not isinstance(identity, BackendIdentity)
                or identity.model != model or identity.backend != backend):
            return verdict(EvidenceState.UNKNOWN, EvidenceReason.IDENTITY_MISSING)
        record = self.load(backend, model)
        if record is None:
            return verdict(EvidenceState.UNKNOWN, EvidenceReason.MISSING)
        if record.identity is None:
            return verdict(EvidenceState.UNKNOWN, EvidenceReason.IDENTITY_MISSING)
        if record.identity != identity:
            return verdict(EvidenceState.UNKNOWN, EvidenceReason.IDENTITY_CHANGED)
        current = time.time() if now is None else float(now)
        if not math.isfinite(current):
            return verdict(EvidenceState.UNKNOWN, EvidenceReason.MISSING)
        if record.synthetic:
            return verdict(EvidenceState.UNKNOWN, EvidenceReason.SYNTHETIC)
        if record.checked_at > current:
            return verdict(EvidenceState.UNKNOWN, EvidenceReason.FUTURE)
        if current - record.checked_at > self.max_age_seconds:
            return verdict(EvidenceState.STALE, EvidenceReason.STALE)
        states = {key: (EvidenceState.PASSED if key in record.passed
                        else EvidenceState.FAILED if key in record.failed
                        else EvidenceState.UNKNOWN) for key in needed}
        if EvidenceState.FAILED in (states[key] for key in necessary):
            return EvidenceVerdict(EvidenceState.FAILED, EvidenceReason.FAILED.value, states)
        if EvidenceState.UNKNOWN in (states[key] for key in necessary):
            return EvidenceVerdict(EvidenceState.UNKNOWN, EvidenceReason.MISSING.value, states)
        for group in any_of:
            if any(states[key] is EvidenceState.PASSED for key in group):
                continue
            if all(states[key] is EvidenceState.FAILED for key in group):
                return EvidenceVerdict(EvidenceState.FAILED, EvidenceReason.FAILED.value, states)
            return EvidenceVerdict(EvidenceState.UNKNOWN, EvidenceReason.MISSING.value, states)
        return EvidenceVerdict(EvidenceState.PASSED, EvidenceReason.ELIGIBLE.value, states)

    def _read_records(self) -> dict[str, dict]:
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(self.MAX_STORE_BYTES + 1)
            if len(raw) > self.MAX_STORE_BYTES:
                return {}
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get("schema") != self.schema or not isinstance(payload.get("records"), dict):
                return {}
            valid: dict[str, dict] = {}
            for key, value in payload["records"].items():
                if not isinstance(key, str) or not isinstance(value, dict):
                    continue
                try:
                    record = BackendConformanceRecord.from_dict(value)
                except (ValueError, TypeError):
                    continue
                if key == self._key(record.backend, record.model):
                    valid[key] = value
            return valid
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _key(backend: str, model: str) -> str:
        return backend.strip() + "\0" + model.strip()


__all__ = [
    "PROTOCOL_CASES",
    "DeterministicFakeProvider",
    "LiveProtocolProvider",
    "RecentCapabilityEvidence",
    "run_gateway_probes",
    "run_protocol_probes",
    "run_smoke_probes",
]
