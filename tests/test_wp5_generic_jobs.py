from __future__ import annotations

import pytest

from sonder_runtime.application.jobs.generic_jobs import (
    GenericJob, GenericJobExecutor, GenericJobStatus, JobExecutionError,
    RetryPolicy,
)


def test_dependency_order_and_typed_values() -> None:
    seen: list[str] = []
    jobs = {
        "finish": GenericJob("finish", lambda ctx: f"{ctx.dependencies['prepare']}!", ("prepare",)),
        "prepare": GenericJob("prepare", lambda ctx: (seen.append(ctx.job_id) or "ready")),
    }
    records = GenericJobExecutor(jobs).run()
    assert [record.job_id for record in records] == ["prepare", "finish"]
    assert records[-1].value == "ready!"
    assert seen == ["prepare"]


def test_retry_hook_controls_attempts_and_records_each_attempt() -> None:
    attempts: list[int] = []
    retries: list[int] = []

    def handler(ctx):
        attempts.append(ctx.attempt)
        if ctx.attempt == 1:
            raise RuntimeError("transient")
        return "ok"

    def retry(record, error):
        retries.append(record.attempt)
        assert str(error) == "transient"
        return True

    records = GenericJobExecutor(
        {"one": GenericJob("one", handler, retry_policy=RetryPolicy(2))},
        retry_hook=retry,
    ).run()
    assert [(r.attempt, r.status) for r in records] == [(1, GenericJobStatus.FAILED), (2, GenericJobStatus.SUCCEEDED)]
    assert attempts == [1, 2]
    assert retries == [1]


def test_failed_dependency_blocks_downstream_job() -> None:
    records = GenericJobExecutor({
        "bad": GenericJob("bad", lambda ctx: (_ for _ in ()).throw(ValueError("no"))),
        "downstream": GenericJob("downstream", lambda ctx: "never", ("bad",)),
    }).run()
    assert records[0].status is GenericJobStatus.FAILED
    assert records[1].status is GenericJobStatus.BLOCKED


def test_missing_and_cyclic_dependencies_are_rejected() -> None:
    with pytest.raises(ValueError, match="unknown job"):
        GenericJobExecutor({"a": GenericJob("a", lambda ctx: None, ("missing",))})
    with pytest.raises(JobExecutionError, match="cycle"):
        GenericJobExecutor({
            "a": GenericJob("a", lambda ctx: None, ("b",)),
            "b": GenericJob("b", lambda ctx: None, ("a",)),
        }).order()
