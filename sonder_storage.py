"""Compatibility surface for bounded storage diagnostics.

New package code imports the filesystem adapter directly.  This root module is
retained for legacy doctor/tests and delegates without adding side effects.
"""
from sonder_runtime.adapters.storage import (  # noqa: F401
    PROBE_BYTES,
    PROBE_TIMEOUT_SECONDS,
    classify,
    inspect_config,
    inspect_root,
    model_roots,
    summarize,
    throughput_probe,
)

__all__ = (
    "PROBE_BYTES", "PROBE_TIMEOUT_SECONDS", "classify", "inspect_config",
    "inspect_root", "model_roots", "summarize", "throughput_probe",
)
