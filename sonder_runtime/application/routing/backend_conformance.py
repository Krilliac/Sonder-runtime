"""Deterministic local backend probes and bounded recent evidence storage."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Protocol

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


class RecentCapabilityEvidence:
    """Atomic JSON record store with a bounded set of backend/model records."""
    schema = 1

    def __init__(self, path: str | os.PathLike[str], *, max_age_seconds: float = 86_400,
                 max_records: int = 32):
        if max_age_seconds <= 0:
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
                    json.dump(payload, stream, sort_keys=True)
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
            if payload.get("schema") != self.schema or not isinstance(payload.get("records"), dict):
                return {}
            return {str(key): value for key, value in payload["records"].items() if isinstance(value, dict)}
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _key(backend: str, model: str) -> str:
        return backend.strip() + "\0" + model.strip()


__all__ = ["DeterministicFakeProvider", "RecentCapabilityEvidence", "run_smoke_probes"]
