"""Typed stop reasons for a selected Codegen strategy canary."""
from __future__ import annotations

from enum import Enum


class CodegenCanaryStop(str, Enum):
    PRIOR_EFFECT = "prior_effect"
    TRACE_UNAVAILABLE = "trace_unavailable"
    RESERVATION = "reservation"
    MODEL_FAILURE = "model_failure"
    RESULT_UNSEALED = "result_unsealed"
    PROJECT_UNSEALED = "project_unsealed"
    BUILD_UNCERTAIN = "build_uncertain"
    ISOLATION_UNAVAILABLE = "isolation_unavailable"


_MESSAGES = {
    CodegenCanaryStop.PRIOR_EFFECT: "prior project effect requires reconciliation",
    CodegenCanaryStop.TRACE_UNAVAILABLE: "sealed strategy state unavailable",
    CodegenCanaryStop.RESERVATION: "attempt could not be reserved before dispatch",
    CodegenCanaryStop.MODEL_FAILURE: "model call failed with a reserved liability",
    CodegenCanaryStop.RESULT_UNSEALED: "attempt result could not be sealed",
    CodegenCanaryStop.PROJECT_UNSEALED: "project completion could not be sealed",
    CodegenCanaryStop.BUILD_UNCERTAIN: "build outcome needs host inspection",
    CodegenCanaryStop.ISOLATION_UNAVAILABLE: "isolated build authority unavailable",
}
_ERROR_PREFIX = "ERROR:"


def render_codegen_canary_stop(stop: CodegenCanaryStop) -> str:
    """Keep the MCP text surface while callers make typed stop decisions."""
    if not isinstance(stop, CodegenCanaryStop):
        raise TypeError("typed Codegen canary stop required")
    return _ERROR_PREFIX + " strategy canary paused: " + _MESSAGES[stop]
