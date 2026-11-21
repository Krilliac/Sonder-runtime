"""Bounded operator refresh of local model capability evidence."""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, cast

import sonder_runtime.adapters.runtime_policy as runtime_policy
from sonder_runtime.adapters.inference.gpu_admission import GpuAdmissionRefused
from sonder_runtime.domain.routing.backend_conformance import BackendConformanceRecord
from sonder_runtime.platform import context_policy, paths
from .capability_evidence import load_production_evidence
from .ollama_conformance import OllamaConformanceProbe

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from typing import NotRequired, TypedDict

    class CapabilityRefreshOutcome(TypedDict):
        model: str
        status: str
        checked_at: float | None
        model_digest: str | None
        ollama_version: str | None
        capabilities: dict[str, bool | None]
        reason: NotRequired[str]
        retry_after_seconds: NotRequired[float | None]

    class CapabilityRefreshResult(TypedDict):
        evidence_path: str
        max_age_seconds: float
        models: list[CapabilityRefreshOutcome]

MAX_MODELS = 16


def configured_local_models(
    home: str | os.PathLike[str] | None = None,
) -> tuple[str, ...]:
    """Read the canonical policy without creating files or importing server."""
    root = Path(home).expanduser() if home else paths.default_home()
    configured = os.environ.get("SONDER_RUNTIME_POLICY", "").strip()
    path = Path(configured).expanduser() if configured else root / "runtime_policy.json"
    policy = runtime_policy.default_policy()
    if path.exists():
        with path.open("rb") as stream:
            raw = stream.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ValueError("runtime policy exceeds refresh size bound")
        policy = runtime_policy.normalize(json.loads(raw), defaults=policy)
    return tuple(dict.fromkeys(model for model in policy["local_models"].values() if model))


def refresh_capabilities(
    *,
    origin: str,
    home: str | os.PathLike[str] | None = None,
    models: Sequence[str] = (),
    timeout_seconds: float = 120.0,
    context_tokens: int | None = None,
) -> CapabilityRefreshResult:
    """Run at most sixteen serial, fixed-prompt batteries; never use cloud models.

    The timeout is per model, including identity reads. A failed refresh replaces
    old passing evidence with an unknown synthetic record, preventing reuse of
    an earlier success after the most recent measurement could not complete.
    A GPU admission refusal is not a failed measurement (no probe ran): that
    model is reported ``deferred`` and its stored evidence is left untouched.
    """
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 300:
        raise ValueError("timeout must be finite and between 0 and 300 seconds")
    configured = configured_local_models(home)
    selected = tuple(dict.fromkeys(models or configured))
    if not selected or len(selected) > MAX_MODELS:
        raise ValueError("refresh requires between 1 and 16 configured local models")
    if any(model not in configured for model in selected):
        raise ValueError("refresh only accepts models bound to configured local tiers")
    context_tokens = (
        cast("Callable[[], int]", context_policy.default_requested)()
        if context_tokens is None else context_tokens
    )
    # Validate every target before any model call or evidence mutation.
    probes = [OllamaConformanceProbe(origin, model, context_tokens=context_tokens)
              for model in selected]
    evidence = load_production_evidence(home)
    outcomes: list[CapabilityRefreshOutcome] = []
    for model, probe in zip(selected, probes, strict=True):
        try:
            record = probe.run(timeout_seconds=timeout_seconds)
        except GpuAdmissionRefused as refused:
            # Admission (a pinned window, the GPU queue, a memory floor) kept the
            # battery from running: nothing was measured, so the earlier evidence
            # stays exactly as it was and the model is reported deferred.
            outcomes.append({
                "model": model, "status": "deferred", "checked_at": None,
                "reason": refused.refusal.reason.value,
                "retry_after_seconds": refused.retry_after_seconds,
                "model_digest": None, "ollama_version": None, "capabilities": {},
            })
            continue
        except Exception:  # noqa: BLE001 - content-free provider failure invalidates old evidence
            record = BackendConformanceRecord("ollama", model, time.time(), (), synthetic=True)
            status = "unavailable"
        else:
            status = "measured"
        evidence.save(record)
        outcomes.append({
            "model": model, "status": status, "checked_at": record.checked_at,
            "model_digest": record.identity.model_digest if record.identity else None,
            "ollama_version": record.identity.backend_version if record.identity else None,
            "capabilities": {result.capability.value: result.passed for result in record.results},
        })
    return {"evidence_path": str(evidence.path), "max_age_seconds": evidence.max_age_seconds,
            "models": outcomes}
