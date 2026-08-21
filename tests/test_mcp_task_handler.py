import io
import json

import pytest

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
        assert task_id == "job-1"
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
