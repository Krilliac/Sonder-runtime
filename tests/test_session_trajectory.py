from __future__ import annotations

import json

import pytest

from sonder_runtime.application.session import project_trajectory
from sonder_runtime.domain.common.errors import IntegrityFailure
from sonder_runtime.domain.common.events import DomainEvent


def _event(sequence, event_type, payload):
    return DomainEvent(event_type, "session", "s1", sequence, payload)


def test_trajectory_links_actions_and_observations_without_raw_content():
    export = project_trajectory([
        _event(1, "tool.call", {"call_id": "c1", "turn_id": "t1", "name": "read_file", "content": '{"path":"secret.txt"}'}),
        _event(2, "tool.result", {"call_id": "c1", "turn_id": "t1", "name": "read_file", "content": "private contents"}),
    ])

    step = export.steps[0]
    assert step.status == "completed"
    assert step.completed_sequence == 2
    assert step.result_bytes == len("private contents".encode())
    assert "private contents" not in export.to_jsonl()
    assert json.loads(export.to_jsonl())["redacted"] is True


def test_unmatched_observation_fails_closed_and_pending_action_is_visible():
    with pytest.raises(IntegrityFailure):
        project_trajectory([_event(1, "tool.result", {"call_id": "missing", "content": "x"})])

    export = project_trajectory([
        _event(1, "tool.call", {"call_id": "c1", "turn_id": "t1", "name": "run", "content": "{}"}),
    ])
    assert export.steps[0].status == "pending"
    assert export.steps[0].result_sha256 is None
