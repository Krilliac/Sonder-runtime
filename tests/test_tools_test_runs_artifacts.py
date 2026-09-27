"""Report collection and launch guards: refusals with permitting controls."""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import pytest

from sonder_runtime.adapters.testing.artifacts import CACHE_NAME, ReportArtifactCollector
from sonder_runtime.adapters.testing.launcher import ProcessTestLauncher
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.testing.ports import TestRunPlan
from sonder_runtime.domain.testing.runners import ReportFormat, TestRunner

pytestmark = pytest.mark.unit

JUNIT = b'<testsuite><testcase name="a"/><testcase name="b"><failure message="m"/></testcase></testsuite>'


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "state" / "test-runs"
    path.mkdir(parents=True)
    return path


def _meta(run_dir: Path, **extra):
    return {"job_id": "test-run-" + "c" * 32, "report_dir": str(run_dir),
            "report_file": str(run_dir / "junit.xml"), "report_format": "junit_xml",
            "started_at": "%.3f" % time.time(), **extra}


def test_a_report_file_in_the_run_dir_is_collected(root):
    run = root / "r1"
    run.mkdir()
    (run / "junit.xml").write_bytes(JUNIT)
    parsed, truncated, note = ReportArtifactCollector(str(root)).collect(_meta(run))
    assert parsed.totals.total == 2 and parsed.totals.failed == 1 and not truncated and note == ""


def test_a_symlinked_report_file_is_not_followed(tmp_path, root):
    run = root / "r2"
    run.mkdir()
    secret = tmp_path / "elsewhere.xml"
    secret.write_bytes(JUNIT)
    os.symlink(secret, run / "junit.xml")
    parsed, _, note = ReportArtifactCollector(str(root)).collect(_meta(run))
    assert parsed is None and "no readable report" in note


def test_a_run_dir_outside_the_report_root_is_refused(tmp_path, root):
    stray = tmp_path / "stray"
    stray.mkdir()
    (stray / "junit.xml").write_bytes(JUNIT)
    parsed, _, note = ReportArtifactCollector(str(root)).collect(_meta(stray))
    assert parsed is None and "refused" in note
    link = root / "linked"
    os.symlink(stray, link, target_is_directory=True)
    parsed, _, note = ReportArtifactCollector(str(root)).collect(_meta(link))
    assert parsed is None and "refused" in note
    # A run dir is a direct child of the root, never a deeper directory.
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    (nested / "junit.xml").write_bytes(JUNIT)
    parsed, _, note = ReportArtifactCollector(str(root)).collect(_meta(nested))
    assert parsed is None and "refused" in note


def test_an_oversized_report_is_refused(root, monkeypatch):
    from sonder_runtime.adapters.testing import artifacts

    run = root / "r3"
    run.mkdir()
    (run / "junit.xml").write_bytes(JUNIT)
    monkeypatch.setattr(artifacts, "MAX_REPORT_FILE_BYTES", 10)
    parsed, _, _ = ReportArtifactCollector(str(root)).collect(_meta(run))
    assert parsed is None


def test_gradle_trees_count_only_files_written_during_the_run(tmp_path, root):
    project = tmp_path / "proj"
    results = project / "build" / "test-results" / "test"
    results.mkdir(parents=True)
    stale = results / "TEST-Old.xml"
    stale.write_bytes(JUNIT)
    old = time.time() - 3600
    os.utime(stale, (old, old))
    (results / "TEST-New.xml").write_bytes(JUNIT)
    (results / "notes.txt").write_text("ignored")
    meta = _meta(root / "unused", report_file="", report_glob="build/test-results/test/*.xml",
                 cwd=str(project))
    parsed, truncated, _ = ReportArtifactCollector(str(root)).collect(meta)
    assert parsed.totals.total == 2 and not truncated  # only the fresh file


def test_a_symlinked_report_tree_is_refused(tmp_path, root):
    project = tmp_path / "proj2"
    project.mkdir()
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "surefire-reports").mkdir(parents=True)
    (elsewhere / "surefire-reports" / "TEST-X.xml").write_bytes(JUNIT)
    os.symlink(elsewhere, project / "target", target_is_directory=True)
    meta = _meta(root / "unused", report_file="", report_glob="target/surefire-reports/TEST-*.xml",
                 cwd=str(project))
    parsed, _, note = ReportArtifactCollector(str(root)).collect(meta)
    assert parsed is None and "symlink" in note


def test_the_report_cache_round_trips_and_ignores_a_foreign_file(root):
    run = root / "r4"
    run.mkdir()
    collector = ReportArtifactCollector(str(root))
    meta = _meta(run)
    collector.store_cached(meta, {"object": "test_report", "job_id": meta["job_id"]})
    assert collector.load_cached(meta)["job_id"] == meta["job_id"]
    if os.name == "posix":
        assert (run / CACHE_NAME).stat().st_mode & 0o777 == 0o600
    (run / CACHE_NAME).write_text(json.dumps({"object": "test_report", "job_id": "test-run-other"}))
    assert collector.load_cached(meta) is None
    (run / CACHE_NAME).write_text("{not json")
    assert collector.load_cached(meta) is None


def _plan(tmp_path, root, argv0, **overrides):
    values = dict(
        runner=TestRunner.MAKE, project_root=str(tmp_path), cwd=str(tmp_path), argv=(argv0, "test"),
        display_argv=("make", "test"), cwd_label="p", command_digest="d" * 64,
        report_format=ReportFormat.TEXT_DIGEST, report_dir=str(root / uuid.uuid4().hex),
        timeout_seconds=10, max_descendants=4, memory_limit_bytes=1 << 30, environment=(),
        checked_executables=(argv0,),
    )
    values.update(overrides)
    return TestRunPlan(**values)


class RecordingProvider:
    def __init__(self):
        self.started = []

    def start(self, request):
        self.started.append(request)
        raise RuntimeError("stop after admission")


def test_the_launcher_rechecks_the_host_executable_at_launch(tmp_path, root):
    provider = RecordingProvider()

    def guard(path):
        if "planted" in path:
            raise PermissionError("host executable rejected")
        return path

    launcher = ProcessTestLauncher(lambda: provider, lambda: None, executable_guard=guard,
                                   report_root=str(root))
    context = local_owner_context(correlation_id="c")
    with pytest.raises(PermissionError):
        launcher.start(_plan(tmp_path, root, str(tmp_path / "planted-make")), context, "test-run-" + "1" * 32)
    assert provider.started == []
    # control: an allowed executable reaches the provider with a closed request
    plan = _plan(tmp_path, root, "/usr/bin/make")
    with pytest.raises(RuntimeError, match="stop after admission"):
        launcher.start(plan, context, "test-run-" + "2" * 32)
    request = provider.started[0]
    assert request.inherit_environment is False and request.deadline_seconds == 10
    assert request.identity.kind == "tool.test_run"
    assert dict(request.metadata)["principal_id"] == context.principal_id
    assert not Path(plan.report_dir).exists()  # a failed start leaves no run dir


def test_a_project_executable_must_stay_inside_the_project(tmp_path, root):
    provider = RecordingProvider()
    launcher = ProcessTestLauncher(lambda: provider, lambda: None, executable_guard=lambda p: p,
                                   report_root=str(root))
    outside = tmp_path.parent / ("outside-" + uuid.uuid4().hex)
    outside.write_text("#!/bin/sh\n")
    try:
        plan = _plan(tmp_path / "proj", root, str(outside), checked_executables=(),
                     project_executable=True)
        with pytest.raises(PermissionError):
            launcher.start(plan, local_owner_context(correlation_id="c"), "test-run-" + "3" * 32)
        assert provider.started == []
    finally:
        outside.unlink()


def test_old_run_dirs_are_pruned_to_the_retention_bound(tmp_path, root):
    for index in range(5):
        (root / ("old%d" % index)).mkdir()
        stamp = time.time() - 1000 + index
        os.utime(root / ("old%d" % index), (stamp, stamp))
    launcher = ProcessTestLauncher(lambda: RecordingProvider(), lambda: None,
                                   executable_guard=lambda p: p, report_root=str(root),
                                   max_retained=3)
    with pytest.raises(RuntimeError):
        launcher.start(_plan(tmp_path, root, "/usr/bin/make"), local_owner_context(correlation_id="c"),
                       "test-run-" + "4" * 32)
    assert sorted(item.name for item in root.iterdir()) == ["old3", "old4"]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFOs only")
def test_a_fifo_planted_as_the_report_does_not_block_collection(root):
    import threading

    run = root / "r-fifo"
    run.mkdir()
    os.mkfifo(run / "junit.xml")
    outcome = {}
    worker = threading.Thread(
        target=lambda: outcome.setdefault("r", ReportArtifactCollector(str(root)).collect(_meta(run))),
        daemon=True)
    worker.start()
    worker.join(10)
    assert not worker.is_alive(), "collecting a FIFO report blocked"
    parsed, _, note = outcome["r"]
    assert parsed is None and "no readable report" in note


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_a_hard_link_planted_as_the_report_is_refused(tmp_path, root):
    run = root / "r-link"
    run.mkdir()
    outside = tmp_path / "outside.xml"
    outside.write_bytes(JUNIT)
    os.link(outside, run / "junit.xml")
    parsed, _, note = ReportArtifactCollector(str(root)).collect(_meta(run))
    assert parsed is None and "no readable report" in note
    # control: the same bytes as an ordinary file are collected
    os.unlink(run / "junit.xml")
    (run / "junit.xml").write_bytes(JUNIT)
    parsed, _, _ = ReportArtifactCollector(str(root)).collect(_meta(run))
    assert parsed is not None and parsed.totals.total == 2
