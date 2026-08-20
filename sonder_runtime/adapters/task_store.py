"""Task/checklist adapters (SPEC-5 WP11, relocated from legacy)."""
from __future__ import annotations

from collections.abc import Callable
from typing import Mapping

from .task_repository import LegacyTaskRepository


class LegacyChecklistEventSink:
    def __init__(self, publish_fn: Callable[[dict], None]) -> None:
        self._publish_fn = publish_fn

    def publish(self, checklist: Mapping[str, object]) -> None:
        self._publish_fn(dict(checklist))
