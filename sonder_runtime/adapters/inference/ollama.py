"""Canonical inference adapter exports for local Ollama (SPEC-5 §12)."""
from __future__ import annotations

from .ollama_gateway import OllamaGateway

__all__ = ["OllamaGateway"]
