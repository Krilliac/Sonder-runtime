import io
import json

import pytest

from sonder_runtime.adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry
from sonder_runtime.application.capabilities.jobs import JobRegistryService
from sonder_runtime.application.jobs.durable_registry import DurableJobRegistry

from sonder_runtime.application.ports.jobs import JobIdentity, JobRecord, JobStatus
from sonder_runtime.application.protocol.mcp_tasks import McpTaskHandler
from sonder_runtime.application.protocol.mcp_compatibility import McpCompatibility
from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.interfaces.mcp.transport import StdioMcpTransport


def _record(status=JobStatus.RUNNING, *, result=None, error=""):
    return JobRecord(
        JobIdentity("job-1", "workflow", "op-1", "idem-1"), status, 3,
        "2026-08-21T10:00:00Z", "2026-08-21T10:01:00Z", result, error,
    )


class _Jobs:
    def __init__(self):
        self.record = _record()
        self.cancelled = []

    def get(self, task_id):
        if task_id != "job-1":
            from sonder_runtime.domain.common.errors import NotFound
            raise NotFound("job not found")
        return self.record

    def cancel(self, task_id, *, reason):
        self.cancelled.append((task_id, reason))
        self.record = _record(JobStatus.CANCELLED)
        return (self.record,)


def test_mcp_task_handler_projects_get_update_and_cancel_without_content():
    jobs = _Jobs()
    handler = McpTaskHandler(jobs, poll_after_ms=500)

    view = handler("tasks/get", {"taskId": "job-1"})
    assert view["taskId"] == "job-1"
    assert view["status"] == "working"
    assert view["pollAfterMs"] == 500
    assert view["contentRedacted"] is True

    updated = handler("tasks/update", {"taskId": "job-1", "inputRequired": True})
    assert updated["status"] == "input_required"

    cancelled = handler("tasks/cancel", {"taskId": "job-1", "reason": "operator request"})
    assert cancelled["status"] == "cancelled"
    assert jobs.cancelled == [("job-1", "operator request")]


@pytest.mark.parametrize("params", [{}, {"taskId": ""}, {"taskId": "job-1", "reason": ""}])
def test_mcp_task_handler_rejects_invalid_parameters(params):
    with pytest.raises(InvalidInput):
        McpTaskHandler(_Jobs())("tasks/cancel", params)


def test_mcp_task_handler_rejects_unknown_method_and_invalid_poll_bound():
    with pytest.raises(InvalidInput):
        McpTaskHandler(_Jobs())("tools/call", {"taskId": "job-1"})
    with pytest.raises(InvalidInput):
        McpTaskHandler(_Jobs(), poll_after_ms=-1)


def test_mcp_task_handler_is_the_negotiated_stdio_dispatch_seam():
    request = "\n".join(json.dumps(item) for item in (
        {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersions": ["2.0"], "capabilities": {"tasks": {}}},
        },
        {
            "jsonrpc": "2.0", "id": 2, "method": "tasks/get",
            "params": {"taskId": "job-1"},
        },
    )) + "\n"
    output = io.StringIO()
    transport = StdioMcpTransport(
        io.StringIO(request), output,
        compatibility=McpCompatibility(supported_versions=("2.0",), capabilities=("tasks",)),
        tool_catalog=(), tool_handler=lambda *_: {},
        task_handler=McpTaskHandler(_Jobs()),
    )

    assert transport.serve() == 2
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert rows[1]["result"]["taskId"] == "job-1"
    assert rows[1]["result"]["contentRedacted"] is True


@pytest.mark.parametrize(
    ("params", "code"),
    [({}, -32602), ({"taskId": "missing"}, -32601)],
)
def test_mcp_task_errors_are_mapped_to_bounded_json_rpc_errors(params, code):
    request = "\n".join(json.dumps(item) for item in (
        {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersions": ["2.0"], "capabilities": {"tasks": {}}},
        },
        {
            "jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": params,
        },
    )) + "\n"
    output = io.StringIO()
    transport = StdioMcpTransport(
        io.StringIO(request), output,
        compatibility=McpCompatibility(supported_versions=("2.0",), capabilities=("tasks",)),
        tool_catalog=(), tool_handler=lambda *_: {}, task_handler=McpTaskHandler(_Jobs()),
    )
    transport.serve()
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert rows[1]["error"]["code"] == code


def test_mcp_task_cancel_projects_requested_root_not_last_descendant():
    def rec(job_id, status):
        return JobRecord(
            JobIdentity(job_id, "workflow", "op-" + job_id, "idem-" + job_id),
            status, 4, "2026-08-21T10:00:00Z", "2026-08-21T10:01:00Z", None, "",
        )

    class _Cascade:
        def __init__(self):
            self.root = rec("root", JobStatus.CANCELLED)

        def get(self, task_id):
            return self.root

        def cancel(self, task_id, *, reason):
            return (
                self.root,
                rec("child-a", JobStatus.CANCELLED),
                rec("child-done", JobStatus.SUCCEEDED),
                rec("grandchild", JobStatus.CANCELLED),
            )

    view = McpTaskHandler(_Cascade())("tasks/cancel", {"taskId": "root"})
    assert view["taskId"] == "root"
    assert view["status"] == "cancelled"


def test_mcp_task_cancel_falls_back_to_get_when_records_omit_requested_root():
    def rec(job_id, status):
        return JobRecord(
            JobIdentity(job_id, "workflow", "op-" + job_id, "idem-" + job_id),
            status, 4, "2026-08-21T10:00:00Z", "2026-08-21T10:01:00Z", None, "",
        )

    class _NoRoot:
        def __init__(self):
            self.gets = []

        def get(self, task_id):
            self.gets.append(task_id)
            return rec("root", JobStatus.CANCELLED)

        def cancel(self, task_id, *, reason):
            return (rec("child-a", JobStatus.CANCELLED), rec("grandchild", JobStatus.CANCELLED))

    jobs = _NoRoot()
    view = McpTaskHandler(jobs)("tasks/cancel", {"taskId": "root"})
    assert view["taskId"] == "root"
    assert view["status"] == "cancelled"
    assert jobs.gets == ["root"]


def _memory_registry(tmp_path):
    return DurableJobRegistry()


def _sqlite_registry(tmp_path):
    return SQLiteDurableJobRegistry(tmp_path / "jobs.db")


@pytest.mark.parametrize("factory", [_memory_registry, _sqlite_registry], ids=["memory", "sqlite"])
def test_mcp_task_cancel_cascade_contract_on_real_registries(factory, tmp_path):
    registry = factory(tmp_path)
    service = JobRegistryService(registry)

    def ident(job_id):
        return JobIdentity(job_id, "workflow", "op-" + job_id, "idem-" + job_id)

    registry.start(ident("root"))
    registry.start(ident("child-a"), parent_job_id="root")
    registry.start(ident("child-b"), parent_job_id="root")
    registry.start(ident("grandchild"), parent_job_id="child-b")
    registry.transition("child-a", JobStatus.RUNNING)
    registry.transition("child-a", JobStatus.SUCCEEDED, result={"ok": True})

    handler = McpTaskHandler(service)
    for _ in range(2):  # the second pass proves repeated cancellation is idempotent
        view = handler("tasks/cancel", {"taskId": "root", "reason": "stop"})
        assert view["taskId"] == "root"
        assert view["status"] == "cancelled"

    records = service.cancel("root", "stop")
    ids = [r.identity.job_id for r in records]
    assert sorted(ids) == ["child-a", "child-b", "grandchild", "root"]
    assert len(ids) == len(set(ids))
    status = {r.identity.job_id: r.status for r in records}
    assert status["child-a"] is JobStatus.SUCCEEDED
    assert status["root"] is JobStatus.CANCELLED
    assert status["child-b"] is JobStatus.CANCELLED
    assert status["grandchild"] is JobStatus.CANCELLED
    assert registry.poll("child-a").status is JobStatus.SUCCEEDED
