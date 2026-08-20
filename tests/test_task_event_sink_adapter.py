from __future__ import annotations

from sonder_runtime.adapters.task_events import ChecklistEventSinkAdapter
from sonder_runtime.adapters.task_store import LegacyChecklistEventSink


def test_task_store_name_is_identity_compatible_with_canonical_event_adapter():
    assert LegacyChecklistEventSink is ChecklistEventSinkAdapter


def test_checklist_event_sink_copies_mapping_before_publishing():
    published = []
    sink = ChecklistEventSinkAdapter(published.append)
    checklist = {"status": "in_progress", "items": ["one"]}

    sink.publish(checklist)
    checklist["status"] = "done"

    assert published == [{"status": "in_progress", "items": ["one"]}]
    assert published[0] is not checklist
