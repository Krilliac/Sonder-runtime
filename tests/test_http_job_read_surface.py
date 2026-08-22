from sonder_runtime.interfaces.http.serve import _job_record_id, _job_record_payload
from sonder_runtime.application.ports.jobs import JobIdentity, JobRecord, JobStatus


def test_job_record_payload_is_bounded_and_json_ready():
    record = JobRecord(
        identity=JobIdentity("job-1", "shell", "op-1", "idem-1"),
        status=JobStatus.SUCCEEDED,
        revision=2,
        created_at="created",
        updated_at="updated",
        result={"ok": True},
    )

    payload = _job_record_payload(record)

    assert payload == {
        "job_id": "job-1", "kind": "shell", "operation_id": "op-1",
        "idempotency_key": "idem-1", "parent_job_id": None,
        "parent_session_id": None, "status": "succeeded", "revision": 2,
        "created_at": "created", "updated_at": "updated",
        "result": {"ok": True}, "error": "",
    }


def test_direct_job_read_decodes_one_safe_path_segment():
    assert _job_record_id("/v1/jobs/job%2Done") == "job-one"
    assert _job_record_id("/v1/jobs/job/child") is None
    assert _job_record_id("/v1/jobs/job%2Fchild") is None
