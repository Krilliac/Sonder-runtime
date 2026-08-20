"""Compatibility surface for the packaged SPEC-5 runtime container."""
from __future__ import annotations

from ..adapters.runtime_capabilities import RuntimeCapabilities
from ..adapters.runtime_configuration import RuntimeConfig
from ..adapters.runtime_container import Runtime, build_runtime

__all__ = ["Runtime", "RuntimeCapabilities", "RuntimeConfig", "build_runtime"]
