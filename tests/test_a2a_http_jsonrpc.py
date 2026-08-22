from __future__ import annotations

import hashlib

from sonder_runtime.application.ports.jobs import JobIdentity, JobRecord, JobStatus
from sonder_runtime.interfaces.http.facades.a2a_jsonrpc import (
    build_application_a2a_handler,
    dispatch_a2a_jsonrpc_route,
)


def _request(method="GetTask"):
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}


def test_a2a_http_route_delegates_only_to_explicit_handler():
    calls = []

    def handler(method, params):
        calls.append((method, params))
        return {"task": {"id": "task-1"}}

    result = dispatch_a2a_jsonrpc_route(handler, "POST", "/a2a", _request())
    assert result.status_code == 200
    assert result.body["result"]["task"]["id"] == "task-1"
    assert calls == [("GetTask", {})]


def test_a2a_http_route_is_truthful_when_handler_is_not_configured():
    result = dispatch_a2a_jsonrpc_route(None, "POST", "/a2a", _request())
    assert result.status_code == 503
    assert result.body["error"]["code"] == "A2A_UNAVAILABLE"


def test_a2a_http_route_rejects_wrong_method_and_path():
    assert dispatch_a2a_jsonrpc_route(lambda *_: {}, "GET", "/a2a", _request()).status_code == 405
    assert dispatch_a2a_jsonrpc_route(lambda *_: {}, "POST", "/other", _request()) is None


class _Jobs:
    def __init__(self):
        self.record = JobRecord(JobIdentity("job-1", "workflow", "op", "idem"), JobStatus.RUNNING)

    def get(self, task_id):
        assert task_id == "job-1"
        return self.record

    def list(self, *, limit):
        return (self.record,)

    def cancel(self, task_id, *, reason):
        assert task_id == "job-1"
        return (JobRecord(self.record.identity, JobStatus.CANCELLED),)


class _Registry:
    registrations = ()


class _Application:
    def __init__(self):
        self.jobs = _Jobs()

    def job_service(self):
        return self.jobs

    def agent_registry(self):
        return _Registry()


def test_default_application_handler_binds_durable_job_reads_and_cancel():
    handler = build_application_a2a_handler(_Application(), base_url="https://sonder.test")
    assert handler("GetTask", {"id": "job-1"})["task"]["status"]["state"] == "TASK_STATE_WORKING"
    assert handler("ListTasks", {"pageSize": 10})["totalSize"] == 1
    assert handler("CancelTask", {"id": "job-1"})["task"]["status"]["state"] == "TASK_STATE_CANCELED"
    assert "agentCard" in handler("GetExtendedAgentCard", {})


def test_default_application_handler_requires_chat_admission_service():
    handler = build_application_a2a_handler(_Application(), base_url="https://sonder.test")
    try:
        handler("SendMessage", {"message": {"messageId": "msg-unconfigured", "parts": [{"text": "hello"}]}})
    except ValueError as error:
        assert "not configured" in str(error)
    else:
        raise AssertionError("SendMessage must require a configured chat service")


class _ChatResult:
    response_text = "hello from Sonder"
    model = "test-model"
    tier = "sonder"


class _Chat:
    def complete(self, command, context):
        assert command.content == "hello"
        assert context.source == "http"
        assert context.auth_level == "admin"
        return _ChatResult()


class _AdmittingJobs(_Jobs):
    def __init__(self):
        self.records = {}

    def start(self, identity):
        record = JobRecord(identity, JobStatus.PENDING)
        self.records[identity.job_id] = record
        return record

    def get(self, task_id):
        return self.records.get(task_id)

    def claim(self, job_id, worker_id, *, lease_seconds):
        self.records[job_id] = JobRecord(self.records[job_id].identity, JobStatus.CLAIMED)
        return object()

    def finish(self, job_id, worker_id, status, *, result=None, error=""):
        record = JobRecord(self.records[job_id].identity, status, result=result, error=error)
        self.records[job_id] = record
        return record


class _AdmittingApplication(_Application):
    def __init__(self):
        self.jobs = _AdmittingJobs()
        self.chat = _Chat()


def test_default_application_handler_admits_bounded_text_message_as_durable_task():
    application = _AdmittingApplication()
    handler = build_application_a2a_handler(application, base_url="https://sonder.test")
    params = {
        "message": {
            "messageId": "msg-1",
            "role": "ROLE_USER",
            "parts": [{"text": "hello"}],
        }
    }
    first = handler("SendMessage", params)["task"]
    second = handler("SendMessage", params)["task"]
    assert first["status"]["state"] == "TASK_STATE_COMPLETED"
    assert first["artifacts"][0]["parts"][0]["text"] == "hello from Sonder"
    assert first["artifacts"][0]["lastChunk"] is True
    assert first["artifacts"][0]["metadata"] == {
        "mimeType": "text/plain",
        "sha256": hashlib.sha256(b"hello from Sonder").hexdigest(),
    }
    assert second == first
    assert len(application.jobs.records) == 1


def test_default_application_handler_rejects_non_text_a2a_message():
    handler = build_application_a2a_handler(_AdmittingApplication(), base_url="https://sonder.test")
    try:
        handler("SendMessage", {
            "message": {"messageId": "msg-2", "parts": [{"data": "not-text"}]}
        })
    except ValueError as error:
        assert "text message parts" in str(error)
    else:
        raise AssertionError("non-text A2A message must be rejected")
