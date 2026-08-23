"""SPEC-2 section 9: redaction happens before formatting, and fails closed."""
from __future__ import annotations

import io
import json
import logging

import pytest

import sonder_logging
from sonder_logging import JsonFormatter, Redactor, REDACTED, REDACTION_FAILED

pytestmark = pytest.mark.unit


def test_known_secret_values_redacted():
    redactor = Redactor(
        env={"SONDER_API_KEY": "topsecret-key-material-1234"}
    )
    out = redactor.redact("auth failed for key topsecret-key-material-1234 sent")
    assert "topsecret-key-material-1234" not in out
    assert REDACTED in out


def test_bearer_tokens_redacted():
    out = Redactor(env={}).redact("Authorization: Bearer abcdef123456789xyz")
    assert "abcdef123456789xyz" not in out


def test_assignment_patterns_redacted():
    redactor = Redactor(env={})
    for text in (
        'api_key="sk-live-abcdef0123456789"',
        "token=ghp_abcdefghijklmnop",
        '{"password": "hunter2hunter2"}',
    ):
        out = redactor.redact(text)
        assert "abcdef" not in out and "hunter2" not in out, text


def test_url_credentials_redacted():
    out = Redactor(env={}).redact("fetching https://owner:s3cr3tpw@host/path")
    assert "s3cr3tpw" not in out


def test_cookie_headers_and_query_credentials_redacted():
    text = (
        "Cookie: sessionid=private-session; csrftoken=private-csrf\n"
        "Set-Cookie: auth=private-auth; HttpOnly\n"
        "https://worker.example/api?access_token=private-token&model=code"
    )
    redacted = Redactor(env={}).redact(text)
    for secret in (
        "private-session", "private-csrf", "private-auth", "private-token",
    ):
        assert secret not in redacted
    assert redacted.count(REDACTED) >= 3


def test_private_key_blocks_redacted():
    text = (
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out = Redactor(env={}).redact(text)
    assert "MIIEow" not in out


def test_workspace_prefixes_redacted():
    redactor = Redactor(env={}, path_prefixes=("/srv/sonder/workspaces",))
    out = redactor.redact("wrote /srv/sonder/workspaces/projA/main.py")
    assert "/srv/sonder/workspaces" not in out
    assert "[WORKSPACE]" in out


def test_redaction_failure_replaces_everything(monkeypatch):
    calls = []
    redactor = Redactor(env={}, failure_hook=lambda: calls.append(1))

    class Exploding:
        def __contains__(self, other):  # pragma: no cover - defensive
            raise RuntimeError("boom")

        def __iter__(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(sonder_logging, "_PATTERNS", Exploding())
    out = redactor.redact("text containing a secret=abcdef012345")
    assert out == REDACTION_FAILED
    assert calls == [1]


def test_json_formatter_emits_structured_redacted_line():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        JsonFormatter(Redactor(env={"SONDER_API_KEY": "secret-value-9876"}))
    )
    logger = logging.getLogger("test.http")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.info(
        "completed with key secret-value-9876",
        extra={
            "component": "http",
            "event_code": "HTTP_REQUEST_COMPLETED",
            "correlation_id": "req_test",
            "duration_ms": 12,
            "result": "ok",
        },
    )
    line = json.loads(stream.getvalue())
    assert line["severity"] == "INFO"
    assert line["component"] == "http"
    assert line["event_code"] == "HTTP_REQUEST_COMPLETED"
    assert line["correlation_id"] == "req_test"
    assert line["timestamp"].endswith("Z")
    assert "secret-value-9876" not in stream.getvalue()
