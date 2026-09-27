"""Small HTTP contract inconsistencies: model echo, context_size, unknown commands, A2A cancel, log hint."""

from contextlib import contextmanager
import http.client
import json
import threading

import pytest

import sonder_runtime.interfaces.http.serve as ts
from sonder_runtime.application.ports.jobs import JobIdentity, JobRecord, JobStatus
from sonder_runtime.interfaces.http.facades.a2a_jsonrpc import build_application_a2a_handler


@contextmanager
def _http_server(monkeypatch, answers):
    monkeypatch.setattr(ts, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(ts.server, "chat_web_response", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        ts.server, "answer_with_history",
        lambda *args, **kwargs: answers.append(kwargs.get("context_size")) or "answer",
    )
    httpd = ts.ThreadingHTTPServer(("127.0.0.1", 0), ts.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _chat(port, **fields):
    body = json.dumps({"messages": [{"role": "user", "content": "hello"}], **fields})
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("POST", "/v1/chat/completions", body=body,
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()


def test_model_name_is_echoed_without_surrounding_whitespace(monkeypatch):
    answers = []
    with _http_server(monkeypatch, answers) as port:
        status, payload = _chat(port, model="  sonder  ")
    assert status == 200
    assert payload["model"] == "sonder"


@pytest.mark.parametrize("value", ["garbage", "-5", -5, 0, True, [], "12q"])
def test_invalid_context_size_is_rejected_not_silently_defaulted(monkeypatch, value):
    answers = []
    with _http_server(monkeypatch, answers) as port:
        status, payload = _chat(port, model="sonder", context_size=value)
    assert status == 400
    assert "context_size" in payload["error"]["message"]
    assert answers == []


@pytest.mark.parametrize("value", ["32k", 8192, "", None])
def test_valid_context_size_is_accepted(monkeypatch, value):
    answers = []
    with _http_server(monkeypatch, answers) as port:
        status, _payload = _chat(port, model="sonder", context_size=value)
    assert status == 200
    assert len(answers) == 1


def test_lone_unknown_slash_command_is_answered_without_a_model_call(monkeypatch):
    monkeypatch.setattr(ts, "_dispatch_catalogued_tool", lambda *a, **k: None)
    reply = ts._handle_slash("/zzzznotacmd", context={"mode": "local-open", "authorized": True})
    assert reply.startswith("No command with that name is available")
    # An ordinary sentence that merely starts with "/" still reaches the model.
    assert ts._handle_slash("/r/python is a forum", context={"mode": "local-open"}) is None
    assert ts._handle_slash("/zzz what does this mean?", context={"mode": "local-open"}) is None


class _Jobs:
    def __init__(self, status, error):
        self.record = JobRecord(JobIdentity("job-1", "workflow", "op", "idem"), status, error=error)

    def get(self, task_id):
        return self.record

    def list(self, *, limit):
        return (self.record,)

    def cancel(self, task_id, *, reason):
        return (self.record,)


class _Application:
    def __init__(self, jobs):
        self.jobs = jobs

    def job_service(self):
        return self.jobs

    def agent_registry(self):
        return type("R", (), {"registrations": ()})()


def test_a2a_cancelled_task_is_not_described_as_failed():
    handler = build_application_a2a_handler(
        _Application(_Jobs(JobStatus.CANCELLED, "cancelled by operator")),
        base_url="https://sonder.test",
    )
    status = handler("CancelTask", {"id": "job-1"})["task"]["status"]
    assert status["state"] == "TASK_STATE_CANCELED"
    assert status["message"]["parts"][0]["text"] == "task cancelled"
    failed = build_application_a2a_handler(
        _Application(_Jobs(JobStatus.FAILED, "boom")), base_url="https://sonder.test",
    )
    assert failed("GetTask", {"id": "job-1"})["task"]["status"]["message"]["parts"][0]["text"] == "task failed"


def test_missing_launcher_log_explains_redirected_output(monkeypatch, tmp_path):
    monkeypatch.setattr(ts.runtime_paths, "default_home", lambda: tmp_path)
    text = ts._local_server_log_tail()
    assert "not available yet" not in text
    assert "redirected" in text
