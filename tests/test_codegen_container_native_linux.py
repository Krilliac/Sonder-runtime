"""Real Docker boundary probes; the dedicated workflow requires zero skips."""
from __future__ import annotations

import json
import os
import sys

import pytest

import isolated_runner
from sonder_runtime.adapters.execution import codegen_container_build as build
from sonder_runtime.bootstrap.strategy import compose_isolated_codegen_build

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not os.environ.get("CODEGEN_NATIVE_IMAGE_ID"),
    reason="requires the locally built, pinned Docker qualification image",
)


@pytest.fixture
def configured(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    private = tmp_path / "host-private"
    private.mkdir(mode=0o700)
    for name in ("checkpoint.key", "checkpoints.db", "checkpoints.db-wal", "checkpoints.db-shm", "home-secret"):
        (private / name).write_text("dummy-never-print-" + name)
    (project / "main.c").write_text("int main(void) { return 0; }\n")
    (project / "private.txt").write_text("project-secret-not-granted")
    monkeypatch.setenv(build.PROJECT_ENV, str(project))
    monkeypatch.setenv(build.STAGING_ENV, str(stage))
    monkeypatch.setenv(build.IMAGE_ENV, os.environ["CODEGEN_NATIVE_IMAGE_ID"])
    monkeypatch.setenv(build.SOURCES_ENV, '["main.c"]')
    monkeypatch.setenv(isolated_runner.ROOTS_ENV, str(stage))
    monkeypatch.setenv(isolated_runner.RUNTIME_ENV, "docker")
    monkeypatch.setenv("SONDER_HOME", str(private))
    monkeypatch.setenv("SONDER_TEST_DUMMY_SECRET", "dummy-credential-never-print")
    runner = compose_isolated_codegen_build(project_dir=str(project), declared_sources=("main.c",))
    assert runner is not None, "Docker image/engine or private staging policy unavailable"
    return project, stage, private, runner


def _run(runner, project, mode, *arguments, timeout=20):
    return runner.run(
        "/usr/bin/probe", json.dumps([mode, *map(str, arguments)]), str(project),
        timeout, "", "", "",
    )


def test_real_staging_fixture_keeps_outputs_in_bounded_tmpfs(configured):
    project, stage, _private, runner = configured
    report, okay = _run(runner, project, "staging", "main.c")
    assert okay and "STAGING_TMPFS_OK" in report, report
    assert not (project / "probe.out").exists()
    assert list(stage.iterdir()) == []
    report, okay = runner.run(
        "/usr/bin/does-not-exist", "[]", str(project), 20, "", "", "",
    )
    assert not okay and "could not run" in report, report
    assert list(stage.iterdir()) == []
    (project / "main.c").write_text("BROKEN\n")
    report, okay = _run(runner, project, "staging", "main.c")
    assert not okay and "STAGING_SOURCE_REJECTED" in report, report
    assert list(stage.iterdir()) == []


def test_real_process_cannot_read_state_home_other_source_env_or_network(configured):
    project, stage, private, runner = configured
    paths = tuple(private / name for name in (
        "checkpoint.key", "checkpoints.db", "checkpoints.db-wal", "checkpoints.db-shm", "home-secret",
    ))
    report, okay = _run(runner, project, "attack", *paths, os.getpid())
    assert okay, report
    for marker in (
        *(f"HOST_FILE_{number}_DENIED" for number in range(2, 7)),
        "UNDECLARED_DENIED", "SOURCE_WRITE_DENIED", "ENV_DENIED",
        "HOST_PROCESS_DENIED",
        "ENGINE_SOCKET_DENIED", "USER_UNPRIVILEGED", "NO_NEW_PRIVILEGES_YES",
        "CAPABILITIES_NONE", "NETWORK_DENIED", "NETWORK_NAMESPACE_LOOPBACK_ONLY",
    ):
        assert marker in report, report
    assert "dummy-never-print" not in report and "dummy-credential" not in report
    assert (project / "main.c").read_text() == "int main(void) { return 0; }\n"
    assert list(stage.iterdir()) == []


def test_real_timeout_is_uncertain_and_container_removed(configured):
    project, stage, _private, runner = configured
    report, okay = _run(runner, project, "hang", timeout=1)
    assert not okay and "could not run" in report, report
    assert list(stage.iterdir()) == []


def test_real_non_granted_source_and_symlink_refuse_before_launch(configured):
    project, stage, private, runner = configured
    (project / "main.c").unlink()
    (project / "main.c").symlink_to(private / "checkpoint.key")
    report, okay = _run(runner, project, "staging", "main.c")
    assert not okay and "could not run" in report, report
    assert list(stage.iterdir()) == []
