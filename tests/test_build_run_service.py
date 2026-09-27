"""BuildJobService over fake planner/launcher/collector: leases, caps,
child leases for a build fix, ownership, timeout and cancellation."""
from __future__ import annotations

import os
import threading
import uuid
from dataclasses import replace

import pytest

from sonder_runtime.application.build.model_service import BuildModelService, LruBuildModelCache
from sonder_runtime.application.build.ports import (
    BUILD_JOB_KIND,
    BuildJobPlan,
    BuildJobRequest,
    BuildJobStatusView,
    BuildTreeLocation,
)
from sonder_runtime.application.build.run_service import (
    BuildJobService,
    InMemoryBuildDirLeases,
    build_job_liveness,
)
from sonder_runtime.application.context import OperationContext, local_owner_context
from sonder_runtime.application.ports.jobs import JobIdentity, JobRecord, JobStatus
from sonder_runtime.domain.common.errors import SonderError

pytestmark = pytest.mark.unit


class Never:
    cancelled = False

    def wait(self, timeout=None):
        return False


def ctx(principal="local-owner"):
    base = local_owner_context(correlation_id=uuid.uuid4().hex)
    if principal == base.principal_id:
        return base
    return OperationContext(correlation_id=base.correlation_id, principal_id=principal,
                            auth_level="user", source="http", deadline_monotonic=None,
                            cancellation=Never())


def plan(build_dir="/p/build", action="build"):
    return BuildJobPlan(
        action=action, system="cmake", project_root="/p", build_dir=build_dir, cwd=build_dir,
        argv=("/usr/bin/cmake", "--build", build_dir), display_argv=("cmake", "--build", "build"),
        cwd_label="p/build", command_digest="c" * 64, environment=(("PATH", "/usr/bin"),),
        env_keys=("PATH",), timeout_seconds=60, max_descendants=512, memory_limit_bytes=None,
        log_dir="/state/build-runs/x", log_file="/state/build-runs/x/output.log", binlog="",
        world="host", network="advisory_off", isolation_truth="unverified", model_digest="m",
        template_id="cmake.build", checked_executables=("/usr/bin/cmake",),
        run_token=uuid.uuid4().hex,
    )


class FakePlanner:
    def __init__(self):
        self.calls = []
        self.build_dir = "/p/build"

    def locate(self, request, context):
        return BuildTreeLocation("/p", self.build_dir, "p", "build")

    def plan_model(self, request, context, *, location=None):
        return {"model": True}

    def plan_run(self, request, model, context, *, lease=None):
        self.calls.append(lease)
        return plan(self.build_dir, request.action)


class FakeReader:
    def fingerprint(self, root, build_dir):
        return "fp"


class FakeLauncher:
    def __init__(self):
        self.jobs = {}
        self.lock = threading.Lock()
        self.cancelled = []

    def start(self, plan, context, job_id, *, parent_job_id="", on_exit=None):
        identity = JobIdentity(job_id, BUILD_JOB_KIND, "c", job_id)
        self.jobs[job_id] = {
            "record": JobRecord(identity, JobStatus.RUNNING),
            "meta": {"kind": BUILD_JOB_KIND, "job_id": job_id, "principal_id": context.principal_id,
                     "action": plan.action, "system": plan.system, "project_root": plan.project_root,
                     "build_dir": plan.build_dir, "parent_job_id": parent_job_id,
                     "command_digest": plan.command_digest, "display_argv_json": '["cmake"]',
                     "started_at": "0"},
            "on_exit": on_exit, "principal": context.principal_id,
        }

    def finish(self, job_id, status=JobStatus.SUCCEEDED, error=""):
        job = self.jobs[job_id]
        job["record"] = replace(job["record"], status=status, error=error)
        if job["on_exit"]:
            job["on_exit"](job_id)

    def poll(self, job_id):
        job = self.jobs.get(job_id)
        return job and job["record"]

    def wait(self, job_id, timeout):
        return self.jobs[job_id]["record"], 0, not self.jobs[job_id]["record"].is_terminal

    def cancel(self, job_id, reason):
        self.cancelled.append(job_id)
        self.finish(job_id, JobStatus.CANCELLED, reason)
        return True

    def metadata(self, job_id):
        job = self.jobs.get(job_id)
        return None if job is None else dict(job["meta"])

    def running_for(self, principal):
        return sum(1 for job in self.jobs.values()
                   if job["principal"] == principal and not job["record"].is_terminal)

    def is_active(self, job_id):
        job = self.jobs.get(job_id)
        return job is not None and not job["record"].is_terminal


class FakeCollector:
    def collect(self, job_id, meta, model, *, record=None, exit_code=None):
        status = record.status.value
        if record.status is JobStatus.CANCELLED and "deadline" in record.error:
            status = "timed_out"
        return {"object": "build_job_report", "job_id": job_id, "status": status}


@pytest.fixture
def svc():
    planner, launcher = FakePlanner(), FakeLauncher()
    models = BuildModelService(FakeReader(), planner, LruBuildModelCache(), clock=lambda: 0.0)
    leases = InMemoryBuildDirLeases(is_active=build_job_liveness(launcher))
    service = BuildJobService(planner, launcher, FakeCollector(), models, leases, clock=lambda: 1.0)
    service.launcher, service.planner = launcher, planner
    return service


def code_of(excinfo):
    return getattr(excinfo.value, "code", "")


def test_a_second_job_on_a_held_build_dir_is_busy(svc):
    svc.start(BuildJobRequest(), ctx())
    with pytest.raises(SonderError) as excinfo:
        svc.start(BuildJobRequest(), ctx())
    assert code_of(excinfo) == "BUILD_DIR_BUSY"


def test_the_lease_is_released_when_the_job_ends(svc):
    job = svc.start(BuildJobRequest(), ctx())
    svc.launcher.finish(job)
    assert svc.start(BuildJobRequest(), ctx()).startswith("build-job-")


def test_the_principal_cap_counts_top_level_jobs(svc):
    for index in range(2):
        svc.planner.build_dir = "/p/b%d" % index
        svc.start(BuildJobRequest(), ctx())
    svc.planner.build_dir = "/p/b9"
    with pytest.raises(SonderError) as excinfo:
        svc.start(BuildJobRequest(), ctx())
    assert code_of(excinfo) == "BUILD_BUSY"
    # another principal is not affected
    assert svc.start(BuildJobRequest(), ctx("alice"))


def test_a_child_lease_bypasses_the_cap_but_not_exclusivity(svc):
    lease = svc.reserve("/p/build", "build-fix-" + "a" * 32, ctx())
    svc.planner.build_dir = "/p/other"
    svc.start(BuildJobRequest(), ctx())  # the fix counts as one, this is two
    svc.planner.build_dir = "/p/build"
    child = svc.start(BuildJobRequest(), ctx(), lease=lease, parent_job_id="build-fix-" + "a" * 32)
    assert svc.launcher.jobs[child]["meta"]["parent_job_id"].startswith("build-fix-")
    assert svc.planner.calls[-1] == lease.lease_id
    # a second concurrent child in the same dir is refused
    with pytest.raises(SonderError) as excinfo:
        svc.start(BuildJobRequest(), ctx(), lease=lease)
    assert code_of(excinfo) == "BUILD_DIR_BUSY"
    # another principal's top-level job cannot take the fix's dir
    with pytest.raises(SonderError) as excinfo:
        svc.start(BuildJobRequest(), ctx("alice"))
    assert code_of(excinfo) == "BUILD_DIR_BUSY"
    svc.launcher.finish(child)
    svc.start(BuildJobRequest(), ctx(), lease=lease)  # next child is fine
    svc.release(lease)


def test_a_foreign_principals_lease_is_refused(svc):
    lease = svc.reserve("/p/build", "build-fix-" + "b" * 32, ctx("alice"))
    with pytest.raises(SonderError) as excinfo:
        svc.start(BuildJobRequest(), ctx(), lease=lease)
    assert code_of(excinfo) == "BUILD_DIR_BUSY"


def test_the_fix_reservation_counts_against_the_cap(svc):
    svc.reserve("/p/r1", "build-fix-" + "c" * 32, ctx())
    svc.reserve("/p/r2", "build-fix-" + "d" * 32, ctx())
    with pytest.raises(SonderError) as excinfo:
        svc.start(BuildJobRequest(), ctx())
    assert code_of(excinfo) == "BUILD_BUSY"


def test_cancel_from_a_foreign_principal_is_not_found(svc):
    job = svc.start(BuildJobRequest(), ctx())
    for call in (lambda: svc.cancel(job, ctx("mallory")), lambda: svc.status(job, ctx("mallory")),
                 lambda: svc.result(job, ctx("mallory"))):
        with pytest.raises(SonderError) as excinfo:
            call()
        assert code_of(excinfo) == "JOB_NOT_FOUND"
    assert not svc.launcher.cancelled
    for bad in ("nope", "build-job-XYZ", "test-run-" + "a" * 32):
        with pytest.raises(SonderError):
            svc.status(bad, ctx())


def test_cancel_by_the_owner_reports_cleanup(svc):
    job = svc.start(BuildJobRequest(), ctx())
    view = svc.cancel(job, ctx(), reason="stop")
    assert isinstance(view, BuildJobStatusView)
    assert view.status == "cancelled" and view.cleanup_proven is True
    assert view.to_wire()["cleanup_proven"] is True


def test_a_deadline_reads_as_timed_out(svc):
    job = svc.start(BuildJobRequest(), ctx())
    svc.launcher.finish(job, JobStatus.CANCELLED, "process deadline exceeded")
    assert svc.status(job, ctx()).status == "timed_out"
    assert svc.result(job, ctx())["status"] == "timed_out"


def test_run_waits_for_the_report(svc):
    job_holder = {}
    original = svc.launcher.start

    def start(plan, context, job_id, **kwargs):
        original(plan, context, job_id, **kwargs)
        job_holder["id"] = job_id
        svc.launcher.finish(job_id)

    svc.launcher.start = start
    report = svc.run(BuildJobRequest(), ctx(), wait_seconds=5)
    assert report["status"] == "succeeded" and report["job_id"] == job_holder["id"]


def test_a_stale_lease_whose_job_vanished_is_reclaimed():
    alive = {"build-job-1": False}
    leases = InMemoryBuildDirLeases(is_active=lambda job: alive.get(job, True))
    leases.acquire("/b", "build-job-1", "p")
    assert leases.acquire("/b", "build-job-2", "p").owner_job_id == "build-job-2"


def test_a_fix_reservation_is_never_treated_as_stale():
    launcher = FakeLauncher()
    leases = InMemoryBuildDirLeases(is_active=build_job_liveness(launcher))
    leases.acquire("/b", "build-fix-" + "e" * 32, "p")
    with pytest.raises(SonderError):
        leases.acquire("/b", "build-job-" + "f" * 32, "p")


def test_request_validation():
    with pytest.raises(SonderError) as excinfo:
        BuildJobRequest(action="rm -rf")
    assert code_of(excinfo) == "ACTION_UNSUPPORTED"
    with pytest.raises(SonderError):
        BuildJobRequest(target="a\x00b")
    assert BuildJobRequest(action="COMPILE_ONE").action == "compile_one"


def test_lease_keys_are_normalized_paths(tmp_path):
    leases = InMemoryBuildDirLeases()
    base = str(tmp_path / "b")
    leases.acquire(base, "build-job-" + "1" * 16, "p")
    for alias in (base + os.sep, str(tmp_path / "b" / ".." / "b")):
        with pytest.raises(SonderError) as excinfo:
            leases.acquire(alias, "build-job-" + "2" * 16, "p")
        assert excinfo.value.code == "BUILD_DIR_BUSY"
