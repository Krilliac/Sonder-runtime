from __future__ import annotations

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


def test_default_application_handler_does_not_synthesize_message_admission():
    handler = build_application_a2a_handler(_Application(), base_url="https://sonder.test")
    try:
        handler("SendMessage", {"message": {"parts": [{"text": "hello"}]}})
    except ValueError as error:
        assert "not configured" in str(error)
    else:
        raise AssertionError("SendMessage must remain explicitly unsupported")
