from datetime import datetime, timezone

import pytest

from sonder_runtime.adapters.persistence.session_checkpoint_privacy import build_session_checkpoint_privacy_adapter
from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.session.checkpoints import checkpoint_projection
from sonder_runtime.domain.common.errors import IntegrityFailure, InvalidInput
from sonder_runtime.domain.common.events import DomainEvent

def _checkpoint():
    events = [DomainEvent("session.started", "session", "s1", 1, {})]
    return checkpoint_projection(events, source_hash="root-hash")

def test_checkpoint_is_durable_idempotent_and_source_pinned(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    adapter = build_session_checkpoint_privacy_adapter(repo)
    checkpoint = _checkpoint()
    first = adapter.save_checkpoint(checkpoint)
    second = adapter.save_checkpoint(checkpoint)
    assert first.event_id == second.event_id
    assert adapter.load_checkpoint("s1") == checkpoint
    assert repo.inspect_integrity("s1").valid
    with pytest.raises(IntegrityFailure, match="stale"):
        adapter.load_checkpoint("s1", source_sequence=2, source_hash="root-hash")

def test_tampered_checkpoint_envelope_fails_closed(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    adapter = build_session_checkpoint_privacy_adapter(repo)
    adapter.save_checkpoint(_checkpoint())
    with repo._connect() as conn:
        conn.execute("DROP TRIGGER session_event_no_update")
        conn.execute("UPDATE session_event SET payload_json = replace(payload_json, 'root-hash', 'changed')")
    with pytest.raises(IntegrityFailure, match="digest"):
        adapter.load_checkpoint("s1")

def test_retention_is_bounded_and_unknown_privacy_is_retained(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db", max_read_limit=4)
    old = "2020-01-01T00:00:00Z"
    repo.append("s1", "message.received", {"privacy_class": "secret"}, occurred_at_utc=old)
    repo.append("s1", "message.received", {"privacy_class": "not-a-class"}, occurred_at_utc=old)
    repo.append("s1", "message.received", {"privacy_class": "public_metadata"}, occurred_at_utc=old)
    adapter = build_session_checkpoint_privacy_adapter(repo, max_scan=4)
    candidates = adapter.retention_candidates("s1", now_utc=datetime(2026, 1, 1, tzinfo=timezone.utc), limit=3)
    assert [(item.sequence, item.privacy_class.value) for item in candidates] == [(1, "secret")]
    assert len(repo.read_range("s1", limit=4)) == 3
    with pytest.raises(InvalidInput):
        adapter.retention_candidates("s1", limit=5)
