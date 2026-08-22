"""Task/checklist event adapter.

This adapter owns the small outbound event boundary used by the checklist
surface.  It deliberately copies the mapping before publishing so callers
cannot mutate the event after it has crossed the adapter boundary.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping


class ChecklistEventSinkAdapter:
    """Publish immutable-at-boundary checklist snapshots to a callback."""

    def __init__(self, publish_fn: Callable[[dict], None]) -> None:
        self._publish_fn = publish_fn

    def publish(self, checklist: Mapping[str, object]) -> None:
        self._publish_fn(dict(checklist))


# Canonical descriptive name for new callers.
ChecklistEventSink = ChecklistEventSinkAdapter
