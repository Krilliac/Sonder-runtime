"""Typed contracts for model bootstrap policy and generation construction.

The transitional runtime still owns the historical implementation of tier
resolution and request generation.  These protocols make that dependency an
injected boundary so the application graph does not need to know about the
legacy module or its private helper names.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from .model_target import ModelTarget


class LegacyModelRuntime(Protocol):
    """The narrow legacy dependency needed by the compatibility adapter."""

    def _serve_target(self, tier: str, strict: bool = False) -> tuple[Any, ...]: ...

    def _make_generate(
        self,
        model: str,
        system: str,
        temperature: float,
        num_predict: int,
        num_ctx: int,
        **kwargs: Any,
    ) -> Callable[..., str]: ...


class ModelBootstrapProvider(Protocol):
    """Typed owner for target selection and generator construction."""

    def resolve_target(self, tier: str, strict: bool = False) -> ModelTarget: ...

    def make_generate(
        self,
        model: str,
        system: str,
        temperature: float,
        num_predict: int,
        num_ctx: int,
        **kwargs: Any,
    ) -> Callable[..., str]: ...


__all__ = ["LegacyModelRuntime", "ModelBootstrapProvider"]
