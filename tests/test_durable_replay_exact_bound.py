"""A session whose whole chain fits exactly in the replay bound is replayable.

``crash_safe_replay`` rejected any read that returned exactly ``read_limit``
events, although such a read may already contain the tail.  It now proves the
tail was reached by checking that no event follows the bounded read, and
still fails closed when one does.
"""
import pytest

from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.application.session.capture import SessionCaptureService
from sonder_runtime.application.session.durable_replay import crash_safe_replay
from sonder_runtime.domain.common.errors import IntegrityFailure


def _session(tmp_path, *, max_read_limit=10_000):
    repository = SQLiteSessionRepository(tmp_path / "s.db", max_read_limit=max_read_limit)
    capture = SessionCaptureService(SQLiteSessionRepository(tmp_path / "s.db"))
    capture.capture_turn(
        "s", "turn-1", ModelRequest(prompt="hello", tier="code"),
        request_id="r1", user_message="hello", model_response="hi",
    )
    return repository, len(repository.read_range("s", limit=1_000))


def test_history_exactly_at_the_caller_bound_is_replayable(tmp_path):
    repository, count = _session(tmp_path)
    result = crash_safe_replay(repository, "s", max_events=count)
    assert result.recovered_sequence == count


def test_history_exactly_at_the_adapter_ceiling_is_replayable(tmp_path):
    _, count = _session(tmp_path)
    repository = SQLiteSessionRepository(tmp_path / "s.db", max_read_limit=count)
    result = crash_safe_replay(repository, "s", max_events=10_000)
    assert result.recovered_sequence == count


def test_history_past_the_bound_still_fails_closed(tmp_path):
    repository, count = _session(tmp_path)
    with pytest.raises(IntegrityFailure, match="exceeds replay bound"):
        crash_safe_replay(repository, "s", max_events=count - 1)
