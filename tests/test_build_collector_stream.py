"""BuildOutputCollector: the streaming prefilter finds a first error in the
middle of a large log that a head-plus-tail window misses, keeps errors
ahead of a warning flood, and builds a bounded report."""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from sonder_runtime.adapters.build.collector import MAX_SOFT_LINES, scan_log
from sonder_runtime.application.ports.jobs import JobIdentity, JobRecord, JobStatus

pytestmark = pytest.mark.unit

ERROR = "src/core/math.cpp:12:9: error: 'lenght' was not declared in this scope"


def five_megabyte_log(path: Path, *, warnings: bool = False) -> Path:
    with open(path, "w", encoding="utf-8") as handle:
        for index in range(30_000):
            handle.write("[%d/60000] Building CXX object CMakeFiles/x.dir/f%d.cpp.o\n" % (index, index))
            if warnings:
                handle.write("src/w%d.cpp:3:5: warning: unused variable 'x' [-Wunused-variable]\n" % index)
            else:
                handle.write("some ordinary compiler chatter line number %d ..........\n" % index)
        handle.write("FAILED: CMakeFiles/core.dir/src/core/math.cpp.o\n")
        handle.write("/usr/bin/g++ -c /src/core/math.cpp -o CMakeFiles/core.dir/src/core/math.cpp.o\n")
        handle.write(ERROR + "\n")
        for index in range(30_000):
            handle.write("[%d/60000] Building CXX object CMakeFiles/y.dir/g%d.cpp.o\n" % (index, index))
            handle.write("more ordinary chatter that is not a diagnostic %d .........\n" % index)
        handle.write("ninja: build stopped: subcommand failed.\n")
    return path


def head_and_tail(path: Path, *, max_bytes=2_000_000, head=65_536) -> str:
    data = path.read_bytes()
    if len(data) <= max_bytes:
        return data.decode()
    return (data[:head] + data[-(max_bytes - head):]).decode(errors="replace")


def test_the_middle_error_is_found_where_head_plus_tail_misses_it(tmp_path):
    log = five_megabyte_log(tmp_path / "output.log")
    assert log.stat().st_size > 5_000_000
    assert "lenght" not in head_and_tail(log)
    scanned = scan_log(str(log))
    assert ERROR in scanned.text.splitlines()
    assert "FAILED: CMakeFiles/core.dir/src/core/math.cpp.o" in scanned.text
    assert scanned.text.splitlines()[-1] == "ninja: build stopped: subcommand failed."
    assert scanned.kept_lines < 100 and scanned.bytes_scanned == log.stat().st_size
    assert not scanned.truncated


def test_a_warning_flood_cannot_crowd_out_the_error(tmp_path):
    log = five_megabyte_log(tmp_path / "output.log", warnings=True)
    scanned = scan_log(str(log))
    assert ERROR in scanned.text and scanned.truncated
    kept_warnings = sum(1 for line in scanned.text.splitlines() if ": warning:" in line)
    assert kept_warnings == MAX_SOFT_LINES


def test_byte_cap_and_long_lines(tmp_path):
    log = tmp_path / "output.log"
    log.write_text("x" * 100_000 + "\n" + ERROR + "\n" + "y: error: late\n" * 10)
    scanned = scan_log(str(log), max_bytes=100_050)
    assert scanned.truncated and scanned.bytes_scanned == 100_050
    assert all(len(line) <= 4096 for line in scanned.text.splitlines())


def test_cmake_error_blocks_and_trace_lines_are_kept(tmp_path):
    log = tmp_path / "output.log"
    log.write_text("-- The CXX compiler identification is GNU 13\n"
                   "CMake Error at CMakeLists.txt:7 (add_executable):\n"
                   "  Cannot find source file:\n\n    missing.cpp\n\n"
                   "-- Configuring incomplete, errors occurred!\n")
    text = scan_log(str(log)).text
    assert "  Cannot find source file:" in text and "    missing.cpp" in text
    trace = tmp_path / "trace.log"
    trace.write_text(". /src/a.h\n.. /usr/include/vector\nNote: including file: C:\\x\\b.h\nnoise\n")
    kept = scan_log(str(trace), trace=True).text.splitlines()
    assert kept[:3] == [". /src/a.h", ".. /usr/include/vector", "Note: including file: C:\\x\\b.h"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_a_symlinked_log_is_not_followed(tmp_path):
    target = tmp_path / "secret"
    target.write_text("root:x:0:0\n")
    link = tmp_path / "output.log"
    link.symlink_to(target)
    assert scan_log(str(link)).missing


# -- report assembly (needs the build domain) --------------------------------------


@pytest.fixture
def collector(tmp_path):
    pytest.importorskip("sonder_runtime.domain.build.report",
                        reason="needs the build domain (lane A-domain-build)")
    from sonder_runtime.adapters.build.collector import BuildOutputCollector

    run_root = tmp_path / "build-runs"
    run_root.mkdir()
    return BuildOutputCollector(str(run_root), redact=lambda text: text.replace("sonder-fake-value-1", "[REDACTED]"))


def job_dir(collector_root: Path):
    job_id = "build-job-" + uuid.uuid4().hex
    directory = collector_root / job_id
    directory.mkdir()
    return job_id, directory


def record(job_id, status, error=""):
    return JobRecord(JobIdentity(job_id, "tool.build_job", "c", job_id), status=status, error=error)


def meta(job_id, directory, **extra):
    base = {"kind": "tool.build_job", "action": "build", "system": "cmake",
            "project_root": "/work/proj", "build_dir": "/work/proj/build",
            "log_file": str(directory / "output.log"), "display_argv_json": '["cmake","--build","build"]',
            "command_digest": "d" * 64, "world": "host", "network": "enforced_off",
            "isolation_truth": "unverified", "started_at": "0", "notes_json": '["plan note"]'}
    base.update(extra)
    return base


def test_the_report_attributes_and_relabels(collector, tmp_path):
    job_id, directory = job_dir(tmp_path / "build-runs")
    (directory / "output.log").write_text(
        "[1/3] Building CXX object CMakeFiles/core.dir/src/core/entity.cpp.o\n"
        "[2/3] Building CXX object CMakeFiles/core.dir/src/core/math.cpp.o\n"
        "FAILED: CMakeFiles/core.dir/src/core/math.cpp.o\n"
        "/usr/bin/g++ -c /work/proj/src/core/math.cpp\n"
        "/work/proj/src/core/math.cpp:4:40: error: 'lenght' was not declared; token=sonder-fake-value-1\n"
        "ninja: build stopped: subcommand failed.\n")
    report = collector.collect(job_id, meta(job_id, directory), None,
                               record=record(job_id, JobStatus.FAILED), exit_code=1)
    assert report.status == "failed" and report.exit_code == 1
    first = report.first_errors[0]
    assert (first.file, first.line) == ("src/core/math.cpp", 4)
    assert "sonder-fake-value-1" not in first.message
    assert report.attributions[0].kind == "tu"
    assert "plan note" in report.notes and report.network == "enforced_off"
    from sonder_runtime.domain.build.report import build_report_to_wire

    wire = json.dumps(build_report_to_wire(report))
    assert "/work/proj" not in wire and "sonder-fake-value-1" not in wire
    # terminal reports are cached
    assert collector.collect(job_id, meta(job_id, directory), None,
                             record=record(job_id, JobStatus.FAILED), exit_code=1) is report


def test_statuses(collector, tmp_path):
    cases = [
        (JobStatus.SUCCEEDED, "", 0, "", "succeeded"),
        (JobStatus.CANCELLED, "process deadline exceeded", None, "", "timed_out"),
        (JobStatus.CANCELLED, "operator", None, "", "cancelled"),
        (JobStatus.FAILED, "", 127, "sonder: could not start cmake: No such file\n", "did_not_run"),
        (JobStatus.RUNNING, "", None, "", "running"),
    ]
    for status, error, code, text, expected in cases:
        job_id, directory = job_dir(tmp_path / "build-runs")
        (directory / "output.log").write_text(text)
        report = collector.collect(job_id, meta(job_id, directory), None,
                                   record=record(job_id, status, error), exit_code=code)
        assert report.status == expected


def test_a_log_path_outside_the_run_dir_is_never_read(collector, tmp_path):
    job_id, directory = job_dir(tmp_path / "build-runs")
    outside = tmp_path / "elsewhere.log"
    outside.write_text(ERROR + "\n")
    report = collector.collect(job_id, meta(job_id, directory, log_file=str(outside)), None,
                               record=record(job_id, JobStatus.FAILED), exit_code=1)
    assert not report.first_errors and any("missing" in note for note in report.notes)


def test_msbuild_flp_log_supersedes_the_console_and_attributes_per_project(collector, tmp_path):
    job_id, directory = job_dir(tmp_path / "build-runs")
    (directory / "output.log").write_text("console only\n")
    (directory / "msbuild.log").write_text(
        "  math.cpp\n"
        "  entity.cpp\n"
        "C:\\src\\core\\math.cpp(12,9): error C2065: 'lenght': undeclared identifier [C:\\src\\core\\core.vcxproj]\n"
        "C:\\src\\game\\main.cpp(8,5): error C2039: 'pos': is not a member of 'Entity' [C:\\src\\game\\game.vcxproj]\n")
    report = collector.collect(
        job_id, meta(job_id, directory, system="msbuild", project_root="C:/src", build_dir="",
                     extra_logs_json=json.dumps([str(directory / "msbuild.log")]),
                     binlog=str(directory / "build.binlog")),
        None, record=record(job_id, JobStatus.FAILED), exit_code=1)
    projects = {item.project.rsplit("/", 1)[-1] for item in report.attributions}
    assert {"core.vcxproj", "game.vcxproj"} <= projects
    assert all(item.kind in ("project", "tu") for item in report.attributions)
    assert any("binlog" in item for item in report.artifacts)
