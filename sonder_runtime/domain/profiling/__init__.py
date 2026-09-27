"""Pure profile readers and the typed ``sonder.profile_digest/1`` result.

Tier 0 of the crash/profile digest feature: callgrind, Chrome trace JSON,
Tracy csvexport CSV, WPA/PIX/Superluminal CSV, heaptrack_print text and perf
report text are read here without launching any process. Host tools (perf,
heaptrack_print, tracy-csvexport, xperf) produce the text these readers take.
"""
from sonder_runtime.domain.profiling.model import (
    SCHEMA,
    AllocationHotspot,
    CaptureMetadata,
    ContextSwitchHotspot,
    FrameStats,
    HotPath,
    ProfileDigest,
    ProfileEngine,
    ProfileFormatUnknown,
    ProfileFunction,
    ProfileLimits,
    ProfileParseError,
    ProfileSourceKind,
    Spike,
    with_source,
)

__all__ = [
    "SCHEMA",
    "AllocationHotspot",
    "CaptureMetadata",
    "ContextSwitchHotspot",
    "FrameStats",
    "HotPath",
    "ProfileDigest",
    "ProfileEngine",
    "ProfileFormatUnknown",
    "ProfileFunction",
    "ProfileLimits",
    "ProfileParseError",
    "ProfileSourceKind",
    "Spike",
    "with_source",
]
