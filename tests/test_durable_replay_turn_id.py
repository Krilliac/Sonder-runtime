from __future__ import annotations

from sonder_runtime.application.session.durable_replay import (
    crash_safe_replay, reconstruct_model_visible_request,
)
from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.domain.common.events import DomainEvent


def _snap(sequence: int, turn_id: str, prompt: str, kind: str = "model.requested") -> DomainEvent:
    return DomainEvent(kind, "session", "s", sequence, {
        "request_id": f"r-{turn_id}", "turn_id": turn_id, "prompt": prompt,
        "tier": "code", "history": [], "options": {}, "tools": [], "ui_facts": {},
    })


def test_selected_turn_id_survives_later_snapshot_of_another_turn() -> None:
    events = (_snap(1, "t1", "first"), _snap(2, "t2", "second"))
    result = reconstruct_model_visible_request(events, turn_id="t1")
    assert result is not None
    assert result.request.prompt == "first"
    assert result.request_id == "r-t1"
    assert result.turn_id == "t1"


def test_selected_turn_id_survives_earlier_and_later_snapshots() -> None:
    events = (_snap(1, "t0", "zero"), _snap(2, "t1", "first", "prompt.snapshot"),
              _snap(3, "t2", "second"))
    result = reconstruct_model_visible_request(events, turn_id="t1")
    assert result is not None
    assert result.request.prompt == "first"
    assert result.turn_id == "t1"


def test_unfiltered_replay_turn_id_matches_the_selected_last_snapshot() -> None:
    events = (_snap(1, "t1", "first"), _snap(2, "t2", "second"))
    result = reconstruct_model_visible_request(events)
    assert result is not None
    assert result.request.prompt == "second"
    assert result.turn_id == "t2"


def test_turn_id_is_empty_when_snapshot_has_none_even_after_other_turns() -> None:
    no_turn = DomainEvent("model.requested", "session", "s", 2, {
        "request_id": "r", "prompt": "p", "tier": "code", "history": [],
        "options": {}, "tools": [], "ui_facts": {},
    })
    result = reconstruct_model_visible_request((_snap(1, "t1", "first"), no_turn))
    assert result is not None
    assert result.request.prompt == "p"
    assert result.turn_id == ""


def test_crash_safe_replay_turn_id_matches_request_returned(tmp_path) -> None:
    repo = SQLiteSessionRepository(tmp_path / "session.db", max_read_limit=100)
    for turn in ("t1", "t2"):
        repo.append("s1", "model.requested", {
            "request_id": f"r-{turn}", "turn_id": turn, "prompt": f"p-{turn}",
            "tier": "code", "history": [], "options": {}, "tools": [], "ui_facts": {},
        })
    result = crash_safe_replay(repo, "s1")
    assert result.request is not None
    assert result.request.request.prompt == "p-t2"
    assert result.request.request_id == "r-t2"
    assert result.request.turn_id == "t2"
