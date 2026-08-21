import json

import pytest

from sonder_runtime.application.ports.jobs import JobIdentity, JobRecord, JobStatus
from sonder_runtime.application.protocol.mcp_tasks import McpTaskStatus, project_job
from sonder_runtime.domain.common.errors import InvalidInput


def _record(status=JobStatus.RUNNING, *, result=None, error=""):
    return JobRecord(
        JobIdentity("job-1", "workflow", "op-1", "idem-1"), status, 3,
        "2026-08-21T10:00:00Z", "2026-08-21T10:01:00Z", result, error,
    )


def test_job_projection_is_reconnectable_and_content_safe():
    view = project_job(_record(), poll_after_ms=500)
    body = view.to_dict()
    assert body["taskId"] == "job-1"
    assert body["status"] == "working"
    assert body["revision"] == 3
    assert body["contentRedacted"] is True
    assert json.loads(view.to_json()) == body


@pytest.mark.parametrize(("status", "expected"), [
    (JobStatus.SUCCEEDED, McpTaskStatus.COMPLETED),
    (JobStatus.FAILED, McpTaskStatus.FAILED),
    (JobStatus.CANCELLED, McpTaskStatus.CANCELLED),
    (JobStatus.PAUSED, McpTaskStatus.WORKING),
])
def test_terminal_and_paused_statuses_map_without_claiming_input(status, expected):
    assert project_job(_record(status)).status is expected


def test_input_required_is_explicit_and_terminal_jobs_cannot_wait_for_input():
    assert project_job(_record(), input_required=True).status is McpTaskStatus.INPUT_REQUIRED
    view = project_job(_record(JobStatus.SUCCEEDED, result={"secret": "hidden"}), input_required=True)
    assert view.status is McpTaskStatus.COMPLETED
    assert view.result_available is True
    assert "hidden" not in view.to_json()


def test_projection_rejects_invalid_poll_contract():
    with pytest.raises(InvalidInput):
        project_job(_record(), poll_after_ms=-1)
    with pytest.raises(InvalidInput):
        project_job(_record(), input_required=1)
