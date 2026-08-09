"""Narrow ports for durable user-preference management."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Mapping, Protocol


class PreferenceRepository(Protocol):
    def upsert_and_list(
        self,
        *,
        scope: str,
        key: str,
        text: str,
        confidence: float,
        limit: int,
    ) -> Sequence[Mapping[str, Any]]: ...

    def list(
        self, *, limit: int, include_disabled: bool
    ) -> Sequence[Mapping[str, Any]]: ...

    def set_enabled(self, target: str, *, enabled: bool, scope: str) -> int: ...


class PreferenceCodec(Protocol):
    def extract(self, text: str) -> Sequence[str]: ...

    def normalize(self, text: str) -> str: ...

    def key(self, text: str) -> str: ...

    def is_stable(self, text: str, source_text: str | None = None) -> bool: ...

    def format(self, rows: Sequence[Mapping[str, Any]]) -> str: ...


class PreferenceEventSink(Protocol):
    """Metadata-only change notification; preference text is never accepted."""

    def changed(self, operation: str, count: int) -> None: ...


ConnectionFactory = Callable[[], Any]
PreferenceModuleProvider = Callable[[], Any]
