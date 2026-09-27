"""Digest text sources: the guarded file window and the job output reader."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

import sonder_runtime.adapters.filesystem.file_ops as file_ops
from sonder_runtime.adapters.diagnostics.sources import RegistryJobOutputReader
from sonder_runtime.adapters.inspection import log_inspect
from sonder_runtime.application.execution.world_control import (
    OutputEvent,
    OutputPage,
    OutputStream,
    OutputWatermark,
)
from sonder_runtime.application.ports.jobs import JobIdentity, JobRecord, JobStatus
from sonder_runtime.domain.common.errors import NotFound


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(root))
    return root


def test_guarded_window_head_and_tail_modes(project):
    path = project / "run.log"
    path.write_text("".join("line %d\n" % i for i in range(1000)), encoding="utf-8")
    lines, window, target = log_inspect.read_guarded_text_window(str(path), max_lines=10)
    assert lines == ["line %d" % i for i in range(10)]
    assert window["line_cap_truncated"] is True and window["tail"] is False
    assert target == path.resolve()
    tail, window, _ = log_inspect.read_guarded_text_window(
        str(path), tail_lines=3, max_scan_bytes=100,
    )
    assert tail == ["line 997", "line 998", "line 999"]
    assert window["tail"] is True and window["byte_truncated"] is True


def test_guarded_window_clamps_every_limit(project):
    path = project / "a.log"
    path.write_text("x\n", encoding="utf-8")
    lines, window, _ = log_inspect.read_guarded_text_window(
        str(path), max_file_bytes=10**15, max_scan_bytes=10**15, tail_lines=10**9,
        max_lines=10**9, timeout=10**9,
    )
    assert lines == ["x"]
    assert window["tail_lines"] == log_inspect.HARD_MAX_TAIL_LINES


def test_guarded_window_refuses_outside_roots(project, tmp_path):
    outside = tmp_path / "elsewhere.log"
    outside.write_text("x\n", encoding="utf-8")
    with pytest.raises(log_inspect.LogInspectError):
        log_inspect.read_guarded_text_window(str(outside))


# --- RegistryJobOutputReader ----------------------------------------------------


class _Registry:
    """Pages a fixed event list like the SQLite registry's stream()."""

    def __init__(self, chunks, *, kind="tool.test_run", dropped_before=0, metadata=None):
        self.events = [
            OutputEvent(OutputWatermark(i + 1), OutputStream.STDOUT, chunk)
            for i, chunk in enumerate(chunks)
        ]
        self.dropped_before = dropped_before
        self.calls = []
        self.record = JobRecord(JobIdentity("job-1", kind, "op", "idem"), JobStatus.SUCCEEDED)
        self.metadata = metadata or {"principal_id": "owner", "runner": "pytest"}

    def view(self, job_id):
        if job_id != "job-1":
            raise KeyError(job_id)
        return SimpleNamespace(record=self.record, metadata=self.metadata)

    def stream(self, job_id, *, after=None, max_events=64, max_bytes=16 * 1024):
        if job_id != "job-1":
            raise KeyError(job_id)
        self.calls.append((after, max_events, max_bytes))
        cursor = (after or OutputWatermark(0)).sequence
        pending = [e for e in self.events if e.watermark.sequence > cursor]
        selected, used = [], 0
        for event in pending:
            size = len(event.data.encode("utf-8"))
            if selected and (len(selected) >= max_events or used + size > max_bytes):
                break
            selected.append(event)
            used += size
        last = selected[-1].watermark if selected else OutputWatermark(cursor)
        has_more = any(e.watermark.sequence > last.sequence for e in pending)
        return OutputPage(tuple(selected), last, has_more, cursor < self.dropped_before)


def test_reader_keeps_head_and_tail_and_marks_the_dropped_middle():
    chunks = ["HEAD-%04d\n" % i for i in range(100)] + ["mid-%05d\n" % i for i in range(5000)] + [
        "tail-%04d\n" % i for i in range(100)
    ]
    registry = _Registry(chunks)
    window = RegistryJobOutputReader(lambda: registry).read_output(
        "job-1", max_bytes=3_000, head_bytes=1_000,
    )
    assert window.truncated is True
    assert window.bytes_read <= 3_000
    assert window.text.startswith("HEAD-0000\n")
    assert window.text.rstrip().endswith("tail-0099")
    assert "mid-00000" not in window.text
    assert window.source_bytes == sum(len(c) for c in chunks)
    assert all(call[1] == 256 and call[2] == 65_536 for call in registry.calls)


def test_reader_small_output_is_complete_and_untruncated():
    registry = _Registry(["a\n", "b\n", "1 passed in 0.1s\n"])
    window = RegistryJobOutputReader(lambda: registry).read_output("job-1")
    assert window.text == "a\nb\n1 passed in 0.1s\n"
    assert window.truncated is False and window.label == "job:job-1"


def test_reader_reports_registry_retention_loss():
    registry = _Registry(["x\n"], dropped_before=5)
    assert RegistryJobOutputReader(lambda: registry).read_output("job-1").truncated is True


def test_reader_respects_page_and_byte_caps():
    registry = _Registry(["%06d\n" % i for i in range(10_000)])
    reader = RegistryJobOutputReader(lambda: registry, max_pages=3)
    window = reader.read_output("job-1")
    assert len(registry.calls) == 3 and window.truncated is True
    registry = _Registry(["y" * 1000 + "\n" for _ in range(100)])
    window = RegistryJobOutputReader(lambda: registry, max_scan_bytes=5_000).read_output("job-1")
    assert window.truncated is True and window.source_bytes < 5_000 + 65_536 + 2_000


def test_reader_wall_clock_cap():
    ticks = iter(range(0, 1000, 3))
    registry = _Registry(["%06d\n" % i for i in range(10_000)])
    reader = RegistryJobOutputReader(lambda: registry, monotonic=lambda: next(ticks))
    window = reader.read_output("job-1")
    assert window.truncated is True
    assert len(registry.calls) <= 2


def test_reader_metadata_and_missing_jobs():
    registry = _Registry(["x\n"], kind="agent_lane.test")
    reader = RegistryJobOutputReader(lambda: registry)
    meta = reader.job_metadata("job-1")
    assert meta["kind"] == "agent_lane.test" and meta["principal_id"] == "owner"
    assert meta["status"] == "succeeded"
    assert reader.job_metadata("nope") is None
    with pytest.raises(NotFound):
        reader.read_output("nope")


def test_reader_against_a_real_subprocess_job_printing_three_megabytes(tmp_path):
    from sonder_runtime.adapters.execution.process_jobs import SubprocessJobProvider
    from sonder_runtime.adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry
    from sonder_runtime.adapters.process_termination import ProcessTreeSupervisor
    from sonder_runtime.application.execution.process_jobs import ProcessJobRequest

    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    provider = SubprocessJobProvider(
        registry,
        process_cleanup=ProcessTreeSupervisor(platform_name=os.name, timeout_seconds=5),
        platform_name=os.name,
    )
    # 3 MB in 16,000-byte lines: each line stays under the provider's inline
    # output bound, so the registry holds real text rather than spill previews.
    script = (
        "import sys\n"
        "row = 'x' * 15999 + '\\n'\n"
        "for i in range(188):\n"
        "    sys.stdout.write(row)\n"
        "sys.stdout.write('3 passed in 0.50s\\n')\n"
    )
    provider.start(ProcessJobRequest(
        JobIdentity("big-output", "tool.test_run", "op-big", "idem-big"),
        (sys.executable, "-c", script), cwd=tmp_path, max_descendants=4,
        deadline_seconds=60,
    ))
    try:
        waited = provider.wait("big-output", timeout=60)
        assert not waited.timed_out
    finally:
        provider.cancel("big-output", reason="test cleanup")
    import time

    reader = RegistryJobOutputReader(lambda: registry)
    # Output readers may still be publishing the last lines just after exit.
    deadline = time.monotonic() + 30
    while True:
        window = reader.read_output("big-output")
        last = window.text.rstrip().splitlines()[-1] if window.text.strip() else ""
        if last == "3 passed in 0.50s" or time.monotonic() > deadline:
            break
        time.sleep(0.2)
    assert window.bytes_read <= 2_000_000
    assert len(window.text.encode("utf-8")) <= 2_000_000 + 1
    assert window.text.rstrip().splitlines()[-1] == "3 passed in 0.50s"
    assert window.truncated is True  # the registry retains a bounded window
    assert reader.job_metadata("big-output")["kind"] == "tool.test_run"
