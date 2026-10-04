"""Separate durable terminal state from owned reaper/exit-hook completion."""
from __future__ import annotations

import os
import threading
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.build import launcher as module
from sonder_runtime.application.build.ports import BUILD_JOB_KIND, BuildJobStatusView
from sonder_runtime.application.ports.jobs import JobIdentity, JobRecord, JobStatus
from sonder_runtime.domain.build.report import BuildJobReport
from sonder_runtime.domain.common.errors import Conflict
from tests.test_build_launcher_real import full_stack, request as build_request, write_project


def terminal_launcher(tmp_path):
    record = JobRecord(JobIdentity("build-job-synthetic", BUILD_JOB_KIND, "c", "k"),
                       JobStatus.SUCCEEDED, result={"exit_code": 0})
    registry = SimpleNamespace(get=lambda _job: record)
    provider = SimpleNamespace(wait=lambda _job, timeout: SimpleNamespace(
        record=record, exit_code=0, timed_out=False,
    ))
    launcher = module.ProcessBuildLauncher(lambda: provider, lambda: registry,
        executable_guard=lambda value: value, run_root=str(tmp_path / "runs"))
    return launcher, record


def test_owned_terminal_record_stays_pending_until_reaper_marker(tmp_path):
    launcher, record = terminal_launcher(tmp_path)
    run = module._Run("owner", str(tmp_path), None)
    launcher._runs[record.identity.job_id] = run
    waited, code, pending = launcher.wait(record.identity.job_id, 0)
    assert waited.is_terminal and code == 0 and pending
    assert launcher.is_active(record.identity.job_id)
    run.done.set()
    assert launcher.wait(record.identity.job_id, 0)[2] is False
    assert not launcher.is_active(record.identity.job_id)


def test_no_owned_run_preserves_durable_terminal_fallback(tmp_path):
    launcher, record = terminal_launcher(tmp_path)
    waited, code, pending = launcher.wait(record.identity.job_id, 0)
    assert waited is record and code == 0 and not pending


@pytest.mark.parametrize("swept", [True, False, None])
def test_exit_hook_exception_finishes_bookkeeping_without_upgrading_sweep(tmp_path, monkeypatch, caplog, swept):
    launcher, record = terminal_launcher(tmp_path)
    seen = []
    run = module._Run("owner", str(tmp_path), None)
    def callback(job_id):
        seen.append((job_id, run.done.is_set(), run.swept))
        raise RuntimeError("synthetic hook failure")
    run.on_exit = callback
    launcher._runs[record.identity.job_id] = run
    monkeypatch.setattr(module, "sweep_session", lambda _session: swept)
    launcher._reap(record.identity.job_id, run, 5)
    assert seen == [(record.identity.job_id, False, swept)]
    assert run.done.is_set() and run.swept is swept
    assert launcher.wait(record.identity.job_id, 0)[2] is False
    assert "build job exit hook failed" in caplog.text


@pytest.fixture
def gated_configure(tmp_path, monkeypatch, request):
    if os.name == "nt":
        pytest.skip("real CMake helper uses POSIX process sessions")
    stack = full_stack(tmp_path, monkeypatch)
    root = write_project(stack.allowed / "mini")
    entered, release = threading.Event(), threading.Event()
    phase = request.param
    if phase == "sweep":
        original_sweep = module.sweep_session
        def sweep(session_id):
            entered.set()
            assert release.wait(10), "test cleanup gate was not released"
            return original_sweep(session_id)
        monkeypatch.setattr(module, "sweep_session", sweep)
    else:
        original_start = stack.launcher.start
        def start(*args, **kwargs):
            original_exit = kwargs["on_exit"]
            def on_exit(job_id):
                entered.set()
                assert release.wait(10), "test exit-hook gate was not released"
                original_exit(job_id)
            kwargs["on_exit"] = on_exit
            return original_start(*args, **kwargs)
        monkeypatch.setattr(stack.launcher, "start", start)
    job = stack.jobs.start(build_request(root, action="configure", generator="Ninja",
        build_dir="build/ninja", config="Debug"), stack.context())
    try:
        assert entered.wait(10), "real configure never entered its cleanup gate"
        yield SimpleNamespace(stack=stack, root=root, job=job, release=release)
    finally:
        release.set()
        assert stack.launcher._runs[job].done.wait(10)


@pytest.mark.parametrize("gated_configure", ["sweep", "callback"], indirect=True)
def test_real_configure_defers_final_report_and_next_build_until_owned_cleanup(gated_configure):
    g = gated_configure
    record, _code, pending = g.stack.launcher.wait(g.job, 0)
    assert record.is_terminal and pending
    assert g.stack.launcher.is_active(g.job)
    result = g.stack.jobs.result(g.job, g.stack.context(), wait_seconds=0)
    assert isinstance(result, BuildJobStatusView) and result.status == "succeeded"
    assert result.cleanup_proven is None
    with pytest.raises(Conflict):
        g.stack.jobs.start(build_request(g.root, build_dir="build/ninja", target="core"), g.stack.context())
    g.release.set()
    assert g.stack.launcher._runs[g.job].done.wait(10)
    assert g.stack.jobs._leases.holder(str(g.root / "build/ninja")) is None
    result = g.stack.jobs.result(g.job, g.stack.context(), wait_seconds=0)
    assert isinstance(result, BuildJobReport) and result.status == "succeeded"


@pytest.mark.parametrize("gated_configure", ["sweep"], indirect=True)
@pytest.mark.parametrize("control", ["bounded_wait", "cancel", "deadline"])
def test_pending_terminal_cleanup_preserves_bounded_query_controls(gated_configure, monkeypatch, control):
    from dataclasses import replace
    import time
    g = gated_configure
    ctx = g.stack.context()
    if control == "cancel":
        ctx = replace(ctx, cancellation=SimpleNamespace(cancelled=True))
    elif control == "deadline":
        ctx = replace(ctx, deadline_monotonic=time.monotonic() - 1)
    def unexpected_cancel(*_args, **_kwargs):
        pytest.fail("querying completed work must not cancel or relaunch it")
    monkeypatch.setattr(g.stack.launcher, "cancel", unexpected_cancel)
    result = g.stack.jobs.result(g.job, ctx, wait_seconds=0.02)
    assert isinstance(result, BuildJobStatusView) and result.status == "succeeded"
    assert result.cleanup_proven is None
    assert g.stack.jobs._leases.holder(str(g.root / "build/ninja")) is not None
