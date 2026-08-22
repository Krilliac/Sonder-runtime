"""Canonical inference adapter for OpenAI-compatible endpoints (SPEC-5 §12)."""
from __future__ import annotations

from .openai_compat_gateway import OpenAICompatibleGateway, OpenAICompatibleConfig

__all__ = ["OpenAICompatibleGateway", "OpenAICompatibleConfig"]
