"""Task/checklist adapters (SPEC-5 WP11, relocated from legacy)."""
from __future__ import annotations

from .task_repository import LegacyTaskRepository
from .task_events import ChecklistEventSinkAdapter


# Compatibility name retained for the former task-store import path.
LegacyChecklistEventSink = ChecklistEventSinkAdapter
