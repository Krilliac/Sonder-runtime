"""Session capture redacts content before the durable write (finding #33).

``sessions.db`` recorded full model prompts, so a credential that reached a
prompt through a tool argument or user message (``AKIA...``, ``Bearer ...``)
was stored verbatim in ``session_event.payload_json``. Capture now passes every
content field through the runtime redactor before appending, computes the
snapshot digest over the redacted snapshot, and leaves identity fields alone,
so replay and export stay deterministic and verify.

Every credential-shaped fixture is assembled at runtime.
"""
from __future__ import annotations

import sqlite3

from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.application.session.capture import CapturedTool, SessionCaptureService
from sonder_runtime.platform.logging import Redactor

AWS_KEY = "AK" + "IA" + "QWERTYUIOPASDFGH"
BEARER = "Bea" + "rer " + "abcdefgh12345678ijkl"
GH_TOKEN = "gh" + "p_" + "Z9y8X7w6V5u4T3s2R1q0P9o8"
PASSWORD = "correct-" + "horse-battery"


def _raw_payloads(database) -> str:
    with sqlite3.connect(database) as conn:
        return "\n".join(row[0] for row in conn.execute("SELECT payload_json FROM session_event"))


def test_captured_turn_never_stores_recognised_secrets(tmp_path):
    database = tmp_path / "sessions.db"
    capture = SessionCaptureService(SQLiteSessionRepository(database))
    request = ModelRequest(
        prompt="distil this example: key %s and header Authorization: %s" % (AWS_KEY, BEARER),
        tier="code",
        system="system text mentioning %s" % GH_TOKEN,
        history=({"role": "user", "content": "earlier %s" % AWS_KEY},),
    )
    turn = capture.capture_turn(
        "s1", "t1", request, request_id="r1",
        user_message="please use %s" % AWS_KEY,
        tools=(CapturedTool(
            call_id="c1", name="deploy",
            arguments={"password": PASSWORD, "note": "token %s" % GH_TOKEN},
            result={"stdout": "used %s" % AWS_KEY},
        ),),
        model_response="done with %s" % GH_TOKEN,
    )
    raw = _raw_payloads(database)
    for secret in (AWS_KEY, GH_TOKEN, PASSWORD, "abcdefgh12345678ijkl"):
        assert secret not in raw
    assert "[REDACTED]" in raw
    # Identity fields are untouched, and the redacted stream replays/exports.
    assert turn.replay.request is not None and turn.replay.request.turn_id == "t1"
    assert turn.export.integrity.valid
    replayed = capture.replay("s1")
    assert replayed.request.snapshot_digest == turn.replay.request.snapshot_digest
    assert "[REDACTED]" in replayed.request.request.prompt


def test_split_capture_redacts_request_message_and_response(tmp_path):
    database = tmp_path / "sessions.db"
    capture = SessionCaptureService(SQLiteSessionRepository(database))
    pending = capture.begin_request(
        "s1", "t1", ModelRequest(prompt="task with %s" % AWS_KEY, tier="code"),
        request_id="r1", user_message="task with %s" % AWS_KEY,
    )
    capture.complete_request(pending, model_response="echo %s" % AWS_KEY)
    assert AWS_KEY not in _raw_payloads(database)


def test_provider_attempt_payloads_are_redacted(tmp_path):
    database = tmp_path / "sessions.db"
    capture = SessionCaptureService(SQLiteSessionRepository(database))
    pending = capture.begin_request(
        "s1", "t1", ModelRequest(prompt="hello", tier="code"), request_id="r1",
    )
    attempt = capture.begin_provider_attempt(
        pending, provider="ollama", operation="chat",
        payload={"messages": [{"role": "user", "content": AWS_KEY}], "api_key": "plain-value"},
    )
    capture.finish_provider_attempt(pending, attempt, response={"text": GH_TOKEN})
    raw = _raw_payloads(database)
    assert AWS_KEY not in raw and GH_TOKEN not in raw and "plain-value" not in raw


def test_the_injected_runtime_redactor_is_used(tmp_path):
    database = tmp_path / "sessions.db"
    live_secret = "live-" + "secret-value-42"
    redactor = Redactor(secret_values=(live_secret,), env={})
    capture = SessionCaptureService(SQLiteSessionRepository(database), redact=redactor.redact)
    capture.begin_request(
        "s1", "t1", ModelRequest(prompt="leaked %s here" % live_secret, tier="code"),
        request_id="r1",
    )
    assert live_secret not in _raw_payloads(database)


def test_a_failing_redactor_fails_closed(tmp_path):
    database = tmp_path / "sessions.db"

    def broken(_text):
        raise RuntimeError("boom")

    capture = SessionCaptureService(SQLiteSessionRepository(database), redact=broken)
    capture.begin_request(
        "s1", "t1", ModelRequest(prompt="secret %s" % AWS_KEY, tier="code"),
        request_id="r1",
    )
    raw = _raw_payloads(database)
    assert AWS_KEY not in raw
    assert "[REDACTION_FAILED]" in raw
