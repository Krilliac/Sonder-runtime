"""Canonical inference adapter for OpenAI-compatible endpoints (SPEC-5 §12).

Re-exports from the existing implementation. The old path
``adapters.openai_compat.gateway`` remains until WP11 legacy deletion.
"""
from __future__ import annotations

from ..openai_compat.gateway import OpenAICompatibleGateway, OpenAICompatibleConfig

__all__ = ["OpenAICompatibleGateway", "OpenAICompatibleConfig"]
