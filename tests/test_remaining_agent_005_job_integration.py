from __future__ import annotations

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor

import pytest

from sonder_runtime.adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry
from sonder_runtime.application.agents.durable_lineage import DurableLineageQuery
from sonder_runtime.application.jobs.durable_registry import ProcessTreeCleanupReceipt
from sonder_runtime.application.ports.jobs import JobIdentity, JobStatus
from sonder_runtime.application.execution.world_control import OutputStream, OutputWatermark
from sonder_runtime.application.ports.subagents import SubagentBudget, SubagentRequest, SubagentStatus
from sonder_runtime.application.subagents.durable_continuation import (
    ChildSessionLineage, DurableChildSession,
)
from sonder_runtime.adapters.persistence.durable_continuation import SQLiteDurableContinuationRepository


def identity(job_id: str, *, parent: str | None = None) -> JobIdentity:
    return JobIdentity(job_id, "workflow", f"op-{job_id}", f"idem-{job_id}", parent_job_id=parent)


@dataclass
class Supervisor:
    calls: list[object]
    complete: bool = True

    def cleanup(self, request):
        self.calls.append(request)
        return ProcessTreeCleanupReceipt(request.job_id, True, 2, 2 if self.complete else 1, self.complete)


def test_sqlite_registry_reopens_parent_child_operation_linkage(tmp_path) -> None:
    path = tmp_path / "jobs.db"
    first = SQLiteDurableJobRegistry(path)
    first.start(identity("root"), process_id=91, process_group_id=91)
    first.start(identity("child", parent="root"))
    assert [item.identity.job_id for item in first.list(parent_job_id="root")] == ["child"]

    reopened = SQLiteDurableJobRegistry(path)
    assert reopened.poll("child").identity.parent_job_id == "root"
    assert reopened.view("root").child_job_ids == ("child",)
    assert [item.identity.job_id for item in reopened.list()] == ["root", "child"]

    reopened.append_output("child", OutputStream.STDOUT, "one")
    reopened.append_output("child", OutputStream.STDOUT, "two")
    page = reopened.stream("child", after=OutputWatermark(1), max_events=1)
    assert [event.data for event in page.events] == ["two"]
    assert not page.has_more


def test_lineage_projection_joins_durable_jobs_and_children_without_prompt_leak(tmp_path) -> None:
    jobs = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    jobs.start(identity("root"))
    children = SQLiteDurableContinuationRepository(tmp_path / "children.db")
    request = SubagentRequest("root", "private prompt", SubagentBudget(max_steps=2), "child")
    children.create(DurableChildSession(request, ChildSessionLineage("root"), SubagentStatus.CREATED))

    query = DurableLineageQuery(jobs, children)
    descendants = query.descendants("root")
    assert [(item.node_id, item.kind, item.depth) for item in descendants] == [("child", "subagent", 1)]
    assert query.operator_query(root_id="root")[0].root_id == "root"
    assert all(not hasattr(item, "prompt") for item in query.snapshot())


def test_lineage_projection_exposes_durable_revisions_to_concurrent_readers(tmp_path) -> None:
    jobs = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    jobs.start(identity("root"))
    children = SQLiteDurableContinuationRepository(tmp_path / "children.db")
    request = SubagentRequest("root", "private prompt", SubagentBudget(max_steps=2), "child")
    children.create(DurableChildSession(request, ChildSessionLineage("root"), SubagentStatus.CREATED))
    query = DurableLineageQuery(jobs, children)

    def read_once():
        return tuple((node.node_id, node.root_id, node.depth, node.revision)
                     for node in query.snapshot(limit=8))

    with ThreadPoolExecutor(max_workers=4) as pool:
        reads = tuple(pool.map(lambda _: read_once(), range(12)))

    assert reads
    assert all(read == reads[0] for read in reads)
    assert reads[0] == (("root", "root", 0, 0), ("child", "root", 1, 0))


def test_recovery_executes_only_bounded_cleanup_and_requires_complete_receipt(tmp_path) -> None:
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    registry.start(identity("orphan"), process_id=42, process_group_id=42)
    registry.transition("orphan", JobStatus.RUNNING)
    supervisor = Supervisor([])
    report = registry.reconcile_with_cleanup(supervisor, owner_instance_id="old", owner_alive=False,
                                             max_process_descendants=3)
    assert len(supervisor.calls) == 1
    assert supervisor.calls[0].max_descendants == 3
    assert report.interrupted_job_ids == ("orphan",)
    assert registry.poll("orphan").status is JobStatus.INTERRUPTED

    registry2 = SQLiteDurableJobRegistry(tmp_path / "jobs2.db")
    registry2.start(identity("orphan"), process_id=42)
    registry2.transition("orphan", JobStatus.RUNNING)
    report2 = registry2.reconcile_with_cleanup(Supervisor([], complete=False), owner_instance_id="old", owner_alive=False)
    assert report2.interrupted_job_ids == ()
    assert registry2.poll("orphan").status is JobStatus.RUNNING


def test_process_tree_request_rejects_unbounded_or_invalid_identity(tmp_path) -> None:
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    registry.start(identity("job"))
    with pytest.raises(ValueError):
        registry.start(identity("bad"), process_id=0)
