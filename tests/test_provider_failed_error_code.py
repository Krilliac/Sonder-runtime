"""provider.failed evidence classifies raw transport failures like telemetry does.

Gateways run the raw transport inside ``dispatch_provider`` and only map its
exception to a domain error afterwards, so the evidence written at dispatch
time sees ``URLError``/``HTTPError``/``TimeoutError``.  Recording those as
INTERNAL_FAILURE made an ordinary provider outage look like a runtime bug.
"""
import io
import socket
import urllib.error

import pytest

from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.application.session.capture import SessionCaptureService
from sonder_runtime.application.session.provider_attempts import (
    dispatch_provider, provider_attempt_scope,
)


def _raise(error):
    def send():
        raise error
    return send


@pytest.mark.parametrize("error,expected", [
    (urllib.error.URLError(ConnectionRefusedError(10061, "refused")), "DEPENDENCY_UNAVAILABLE"),
    (urllib.error.HTTPError("http://x", 503, "unavailable", {}, io.BytesIO()), "DEPENDENCY_UNAVAILABLE"),
    (urllib.error.HTTPError("http://x", 429, "busy", {}, io.BytesIO()), "CAPACITY_EXCEEDED"),
    (socket.timeout("timed out"), "DEADLINE_EXCEEDED"),
    (RuntimeError("bug"), "INTERNAL_FAILURE"),
])
def test_provider_failed_code_matches_transport_failure(tmp_path, error, expected):
    repository = SQLiteSessionRepository(tmp_path / "session.db")
    capture = SessionCaptureService(repository)
    pending = capture.begin_request(
        "session", "turn", ModelRequest(prompt="p", tier="code"), request_id="request",
    )
    with provider_attempt_scope(capture, pending), pytest.raises(type(error)):
        dispatch_provider("ollama", "/api/chat", {}, _raise(error))
    events = repository.read_range("session")
    assert events[-1].event_type == "provider.failed"
    assert events[-1].payload["error_code"] == expected
