"""Input grants and fail-closed host result checks, independent of an engine.

The native Linux qualification suite separately proves the actual OS boundary.
"""
from __future__ import annotations

import io
import os
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

import isolated_runner
from sonder_runtime.adapters.execution import codegen_container_build as build
from sonder_runtime.bootstrap.strategy import compose_isolated_codegen_build


@pytest.fixture
def configured(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    stage = tmp_path / "private-stage"
    stage.mkdir(mode=0o700)
    monkeypatch.setenv(build.PROJECT_ENV, str(project))
    monkeypatch.setenv(build.STAGING_ENV, str(stage))
    monkeypatch.setenv(build.IMAGE_ENV, "sha256:" + "b" * 64)
    monkeypatch.setenv(build.SOURCES_ENV, '["src/main.c"]')
    monkeypatch.setenv(build.INPUTS_ENV, '["build.mk"]')
    monkeypatch.setenv(isolated_runner.ROOTS_ENV, str(stage))
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "private-state"))
    monkeypatch.setattr(isolated_runner, "detect_runtime", lambda: ("docker", "/usr/bin/docker", ("--host", "unix:///local")))
    monkeypatch.setattr(isolated_runner, "_inspect_image_policy", lambda *_args: "sha256:" + "b" * 64)
    return project, stage


def test_disabled_without_exact_host_grants_and_linux(monkeypatch, configured):
    project, _stage = configured
    monkeypatch.delenv(build.IMAGE_ENV)
    assert compose_isolated_codegen_build(project_dir=str(project), declared_sources=("src/main.c",)) is None
    monkeypatch.setenv(build.IMAGE_ENV, "sha256:" + "b" * 64)
    assert compose_isolated_codegen_build(project_dir=str(project), declared_sources=("other.c",)) is None
    monkeypatch.setenv(build.SOURCES_ENV, '["src/main.c", "build.mk"]')
    assert compose_isolated_codegen_build(project_dir=str(project), declared_sources=("build.mk",)) is None
    monkeypatch.setattr(build.sys, "platform", "win32")
    assert compose_isolated_codegen_build(project_dir=str(project), declared_sources=("src/main.c",)) is None


def test_snapshot_contains_only_declared_files_and_extra_host_input(monkeypatch, configured):
    project, stage_root = configured
    (project / "src").mkdir()
    (project / "src" / "main.c").write_text("int main(void) { return 0; }\n")
    (project / "build.mk").write_text("all:\n\ttrue\n")
    (project / "private.txt").write_text("dummy-private-marker")
    runner = compose_isolated_codegen_build(project_dir=str(project), declared_sources=("src/main.c",))
    assert runner is not None

    def observe(image, argv, stage, **kwargs):
        assert image == "sha256:" + "b" * 64
        assert argv == [build.LAUNCHER, "cc", "-fsyntax-only", "src/main.c"]
        assert not kwargs.get("writable_workspace", False)
        assert kwargs["build_scratch_mb"] == 1024
        assert kwargs["verify_exit_cleanup"] is True
        assert sorted(str(path.relative_to(stage)) for path in Path(stage).rglob("*") if path.is_file()) == ["build.mk", "src/main.c"]
        return {"cleanup": "verified-absent", "error": "", "returncode": 0,
                "stdout": "compiled", "stderr": ""}

    monkeypatch.setattr(isolated_runner, "run_isolated", observe)
    assert runner.run("cc", '["-fsyntax-only", "src/main.c"]', str(project), 20, "", "", "") == ("compiled", True)
    assert list(stage_root.iterdir()) == []


@pytest.mark.parametrize("path", ["../private.key", ".env", "src/.env", "strategy/checkpoints.db", "src\\main.c", "/etc/passwd", "src//main.c"])
def test_private_or_ambiguous_sources_cannot_be_granted(configured, path):
    with pytest.raises(ValueError):
        build._relative_file(path)


def test_link_and_hardlink_cannot_enter_snapshot(configured):
    project, stage = configured
    (project / "src").mkdir()
    source = project / "src" / "main.c"
    private = project.parent / "private.txt"
    private.write_text("dummy-private-marker")
    (project / "build.mk").write_text("all:")
    source.symlink_to(private)
    with pytest.raises(OSError):
        build._stage(project, stage, ("src/main.c", "build.mk"),
                     frozenset({"build.mk"}), build._identity(project),
                     build._identity(stage))
    source.unlink()
    os.link(private, source)
    with pytest.raises(ValueError, match="ordinary file"):
        build._stage(project, stage, ("src/main.c", "build.mk"),
                     frozenset({"build.mk"}), build._identity(project),
                     build._identity(stage))
    assert list(stage.iterdir()) == []


def test_same_size_change_with_restored_mtime_refuses_snapshot(monkeypatch, configured):
    project, _stage = configured
    source = project / "main.c"
    source.write_bytes(b"original")
    original_fdopen = os.fdopen

    @contextmanager
    def mutate_after_read(fd, *args, **kwargs):
        with original_fdopen(fd, *args, **kwargs) as opened:
            class ChangedDuringRead:
                def read(self, amount):
                    data = opened.read(amount)
                    previous = source.stat()
                    time.sleep(0.002)  # Even coarse filesystem timestamp clocks advance.
                    source.write_bytes(b"modified")
                    os.utime(source, ns=(previous.st_atime_ns, previous.st_mtime_ns))
                    assert source.stat().st_mtime_ns == previous.st_mtime_ns
                    return data

            yield ChangedDuringRead()

    monkeypatch.setattr(build.os, "fdopen", mutate_after_read)
    descriptor = os.open(project, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ValueError, match="changed during snapshot"):
            build._read_input(descriptor, "main.c", os.fstat(descriptor).st_dev)
    finally:
        os.close(descriptor)


def test_project_replacement_between_path_check_and_open_refuses(monkeypatch, configured):
    project, stage = configured
    (project / "src").mkdir()
    (project / "src" / "main.c").write_text("safe")
    original_identity = build._identity(project)
    original_open = os.open
    moved = project.with_name("moved-project")

    def swap_project_before_open(path, flags, *args, **kwargs):
        if path == project:
            project.rename(moved)
            project.mkdir()
            (project / "src").mkdir()
            (project / "src" / "main.c").write_text("secret")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(build.os, "open", swap_project_before_open)
    with pytest.raises(ValueError, match="project changed"):
        build._stage(project, stage, ("src/main.c",), frozenset(),
                     original_identity, build._identity(stage))
    assert list(stage.iterdir()) == []


def test_staging_root_replacement_before_descriptor_open_refuses(monkeypatch, configured):
    project, stage = configured
    (project / "src").mkdir()
    (project / "src" / "main.c").write_text("safe")
    original_stage_identity = build._identity(stage)
    original_open = os.open
    moved = stage.with_name("moved-stage")

    def swap_stage_before_open(path, flags, *args, **kwargs):
        if path == stage:
            stage.rename(moved)
            stage.mkdir(mode=0o700)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(build.os, "open", swap_stage_before_open)
    with pytest.raises(ValueError, match="staging root changed"):
        build._stage(project, stage, ("src/main.c",), frozenset(),
                     build._identity(project), original_stage_identity)
    assert list(stage.iterdir()) == []
    assert list(moved.iterdir()) == []


def test_uncertain_teardown_retains_disposable_stage_and_rejects(monkeypatch, configured):
    project, stage_root = configured
    (project / "src").mkdir()
    (project / "src" / "main.c").write_text("int main(void) { return 0; }\n")
    (project / "build.mk").write_text("all:")
    runner = compose_isolated_codegen_build(project_dir=str(project), declared_sources=("src/main.c",))
    monkeypatch.setattr(isolated_runner, "run_isolated", lambda *_a, **_k: {
        "cleanup": "uncertain-container-removal", "error": "cleanup unavailable",
        "returncode": 0, "stdout": "all green", "stderr": "",
    })
    report, okay = runner.run("cc", "[]", str(project), 20, "", "", "")
    assert not okay and "could not run" in report
    assert len(list(stage_root.iterdir())) == 1


@pytest.mark.parametrize("status,expected_report,okay", [
    (0, "candidate diagnostic", True), (1, "candidate diagnostic", False),
    (124, "candidate diagnostic", False),
    (125, "could not run", False), (126, "could not run", False),
    (127, "could not run", False), (137, "could not run", False),
    (143, "could not run", False), (-9, "could not run", False),
    (True, "could not run", False),
])
def test_reserved_or_signal_like_status_never_becomes_compiler_feedback(
    monkeypatch, configured, status, expected_report, okay,
):
    project, stage_root = configured
    (project / "src").mkdir()
    (project / "src" / "main.c").write_text("int main(void) { return 0; }\n")
    (project / "build.mk").write_text("all:")
    runner = compose_isolated_codegen_build(project_dir=str(project), declared_sources=("src/main.c",))
    monkeypatch.setattr(isolated_runner, "run_isolated", lambda *_a, **_k: {
        "cleanup": "verified-absent", "error": "", "returncode": status,
        "stdout": "candidate diagnostic", "stderr": "",
    })
    report, actual_okay = runner.run("cc", "[]", str(project), 20, "", "", "")
    assert actual_okay is okay and expected_report in report
    assert list(stage_root.iterdir()) == []


def test_state_inside_project_blocks_composition_even_with_exact_source_grant(monkeypatch, configured):
    project, _stage = configured
    monkeypatch.setenv("SONDER_HOME", str(project))
    assert compose_isolated_codegen_build(project_dir=str(project), declared_sources=("src/main.c",)) is None


def test_project_or_private_staging_root_replacement_refuses_before_engine(monkeypatch, configured):
    project, stage_root = configured
    runner = compose_isolated_codegen_build(project_dir=str(project), declared_sources=("src/main.c",))
    monkeypatch.setattr(isolated_runner, "run_isolated", lambda *_a, **_k: pytest.fail("container launched"))
    project.rename(project.with_name("old-project"))
    project.mkdir()
    report, okay = runner.run("cc", "[]", str(project), 20, "", "", "")
    assert not okay and "could not run" in report
    project.rmdir()
    project.with_name("old-project").rename(project)
    stage_root.rename(stage_root.with_name("old-stage"))
    stage_root.mkdir(mode=0o700)
    report, okay = runner.run("cc", "[]", str(project), 20, "", "", "")
    assert not okay and "could not run" in report


@pytest.mark.parametrize("cleanup_status,expected_ok", [
    ("verified-absent", True), ("uncertain-container-removal", False),
])
def test_host_requires_verified_cleanup_even_after_zero_exit(monkeypatch, cleanup_status, expected_ok):
    class ExitedProcess:
        returncode = 0
        stdout = io.BytesIO(b"candidate says passed\n")
        stderr = io.BytesIO()

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(isolated_runner.subprocess, "Popen", lambda *_a, **_k: ExitedProcess())
    monkeypatch.setattr(isolated_runner, "_verify_project_unchanged", lambda *_a: None)
    monkeypatch.setattr(isolated_runner, "_cleanup", lambda *_a: cleanup_status)
    result = isolated_runner._run_bounded(
        ["/usr/bin/docker", "run"], "docker", "/usr/bin/docker", ("--host", "unix:///local"),
        "sonder-isolated-" + "1" * 32, b"", 2, 4096, "/tmp/unused", (),
        verify_exit_cleanup=True,
    )
    assert result["ok"] is expected_ok
    assert result["cleanup"] == cleanup_status
    assert bool(result["error"]) is (not expected_ok)


def test_build_scratch_policy_is_bounded_and_source_mount_remains_read_only(monkeypatch, tmp_path):
    project = tmp_path / "stage"
    project.mkdir()
    monkeypatch.setenv(isolated_runner.ROOTS_ENV, str(tmp_path))
    runtime = str(tmp_path / "docker")
    argv, _name = isolated_runner.build_runtime_argv(
        "docker", runtime, ("--host", "unix:///local"), "sha256:" + "a" * 64,
        ["/usr/local/bin/sonder-codegen-launch", "cc"], str(project),
        memory_mb=2048, build_scratch_mb=1024,
    )
    assert "--tmpfs=/build:rw,nosuid,nodev,mode=1777,size=1024m" in argv
    assert "--network=none" in argv and "--read-only" in argv
    assert "--security-opt=no-new-privileges" in argv
    assert argv[argv.index("--mount") + 1].endswith(",readonly")
    for invalid in (0, 2049, 1.5, True):
        with pytest.raises(ValueError):
            isolated_runner.build_runtime_argv(
                "docker", runtime, ("--host", "unix:///local"), "sha256:" + "a" * 64,
                ["cc"], str(project), memory_mb=2048, build_scratch_mb=invalid,
            )
