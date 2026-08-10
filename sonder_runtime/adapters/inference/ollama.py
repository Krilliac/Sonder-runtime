"""Canonical inference adapter for local Ollama (SPEC-5 §12).

Re-exports from the existing implementation. The old path
``adapters.ollama.gateway`` remains until WP11 legacy deletion.
"""
from __future__ import annotations

from ..ollama.gateway import OllamaGateway

__all__ = ["OllamaGateway"]
