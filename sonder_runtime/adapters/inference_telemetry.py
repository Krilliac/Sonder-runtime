"""Compatibility exports for the inference telemetry adapter.

The canonical implementation lives in
``sonder_runtime.adapters.inference.telemetry``.
"""
from .inference.telemetry import from_ollama, from_openai_compatible

__all__ = ["from_ollama", "from_openai_compatible"]
