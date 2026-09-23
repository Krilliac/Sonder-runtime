"""Deterministic local backend probes and bounded recent evidence storage."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Protocol

from ...application.context import local_owner_context
from ...application.ports.model_gateway import ModelRequest
from ...domain.common.errors import Cancelled
from ...domain.routing.backend_conformance import (
    BackendCapability, BackendConformanceRecord, EvidenceReason, ProbeResult,
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
    checked_at = time.time() if now is None else float(now)
    results = []
    try:
        passed = provider.chat("sonder conformance plain chat") == "ok:sonder conformance plain chat"
        results.append(ProbeResult(BackendCapability.CHAT, passed, "plain_chat_passed" if passed else "plain_chat_mismatch"))
    except Exception:
        results.append(ProbeResult(BackendCapability.CHAT, False, "plain_chat_error"))
    try:
        value = provider.structured_tool_continue("sonder conformance structured")
        passed = isinstance(value, dict) and value.get("continued") is True and value.get("tool")
        results.append(ProbeResult(BackendCapability.STRUCTURED, bool(passed), "structured_tool_continuation_passed" if passed else "structured_tool_continuation_invalid"))
    except Exception:
        results.append(ProbeResult(BackendCapability.STRUCTURED, False, "structured_tool_continuation_error"))
    try:
        passed = provider.cancel() is True
        results.append(ProbeResult(BackendCapability.CANCELLATION, passed, "cancellation_passed" if passed else "cancellation_not_supported"))
    except (AttributeError, NotImplementedError):
        results.append(ProbeResult(BackendCapability.CANCELLATION, False, "cancellation_not_supported"))
    except Exception:
        results.append(ProbeResult(BackendCapability.CANCELLATION, False, "cancellation_error"))
    return BackendConformanceRecord(backend, model, checked_at, tuple(results), synthetic=synthetic)


def run_gateway_probes(
    gateway,
    *,
    backend: str,
    model: str,
    now: float | None = None,
    timeout_seconds: float = 30.0,
    cloud_allowed: bool = False,
    remote_ollama_allowed: bool = False,
) -> BackendConformanceRecord:
    """Probe one concrete ``ModelGateway`` route and emit non-synthetic evidence.

    The probe uses fixed, non-sensitive prompts and records only outcome codes;
    provider text is never persisted.  The structured check requires a small
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
    except Exception:
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
    except Exception:
        results.append(ProbeResult(BackendCapability.STRUCTURED, False, "structured_tool_continuation_error"))

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
    except Exception:
        results.append(ProbeResult(BackendCapability.CANCELLATION, False, "cancellation_wrong_error"))
    else:
        results.append(ProbeResult(BackendCapability.CANCELLATION, False, "cancellation_not_supported"))
    return BackendConformanceRecord(backend, model, checked_at, tuple(results), synthetic=False)


class RecentCapabilityEvidence:
    """Atomic JSON record store with a bounded set of backend/model records."""
    schema = 1

    def __init__(self, path: str | os.PathLike[str], *, max_age_seconds: float = 86_400,
                 max_records: int = 32):
        if not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")
        self.path = Path(path)
        self.max_age_seconds = float(max_age_seconds)
        if max_records <= 0:
            raise ValueError("max_records must be positive")
        self.max_records = int(max_records)
        self._lock = threading.RLock()

    def save(self, record: BackendConformanceRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            records = self._read_records()
            records[self._key(record.backend, record.model)] = record.to_dict()
            records = dict(sorted(records.items(), key=lambda item: float(item[1].get("checked_at", 0)), reverse=True)[:self.max_records])
            payload = {"schema": self.schema, "records": records}
            fd, temp_name = tempfile.mkstemp(prefix=self.path.name + ".", dir=str(self.path.parent), text=True)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream, sort_keys=True, allow_nan=False)
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
              now: float | None = None):
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

    def _read_records(self) -> dict[str, dict]:
        try:
            with self.path.open(encoding="utf-8") as stream:
                payload = json.load(stream)
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
    "DeterministicFakeProvider", "RecentCapabilityEvidence", "run_gateway_probes",
    "run_smoke_probes",
]
