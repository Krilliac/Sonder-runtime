"""The hermetic CI image's /usr/bin/env shim must accept what build_runtime_argv emits.

scripts/codegen_container_env.c is the only `env` inside the scratch image used
by the native container qualification. If the argv shape built by
isolated_runner drifts from the shim's parser, every launch there fails with
status 125, so this compiles the shim and feeds it the real env segment.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sonder_runtime.adapters.execution import isolated_runner

SHIM_SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "codegen_container_env.c"
IMAGE = "sha256:" + "a" * 64

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="the env shim is a POSIX program built for the Linux CI image"
)


@pytest.fixture(scope="module")
def shim(tmp_path_factory):
    compiler = shutil.which("gcc") or shutil.which("cc") or shutil.which("clang")
    if compiler is None:
        pytest.skip("no C compiler (gcc, cc or clang) on PATH to build the env shim")
    binary = tmp_path_factory.mktemp("envshim") / "env"
    subprocess.run(
        [compiler, "-O2", "-Wall", "-Wextra", "-Werror", "-o", str(binary), str(SHIM_SOURCE)],
        check=True, capture_output=True, timeout=120,
    )
    return str(binary)


def _env_tail(monkeypatch, tmp_path, scratch):
    monkeypatch.setenv(isolated_runner.ROOTS_ENV, str(tmp_path))
    project = tmp_path / "project"
    project.mkdir()
    argv, _name = isolated_runner.build_runtime_argv(
        "docker", str(tmp_path / "docker"), ("--host", "unix:///local"), IMAGE,
        ["/usr/bin/env"], str(project), memory_mb=2048, build_scratch_mb=scratch,
    )
    return argv[argv.index(IMAGE) + 1:]


@pytest.mark.parametrize("scratch", [None, 1024])
def test_shim_runs_command_with_exactly_the_variables_the_runner_emits(
    shim, monkeypatch, tmp_path, scratch
):
    tail = _env_tail(monkeypatch, tmp_path, scratch)
    assert tail[:2] == ["-i", "--"] and tail[-1] == "/usr/bin/env"
    expected = tail[2:-1]
    assert expected and all("=" in item for item in expected)
    result = subprocess.run(
        [shim, *tail], capture_output=True, text=True, timeout=30,
        env={"LEAKED_FROM_PARENT": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == expected


@pytest.mark.parametrize(
    "argv",
    [
        ["-i", "PATH=/usr/bin", "--", "/usr/bin/env"],
        ["-i", "--", "PATH=/usr/bin"],
        ["-i", "--", "/usr/bin/env", "a", "b"],
        ["-i", "--", "=x", "/usr/bin/env"],
    ],
)
def test_shim_fails_closed_on_any_other_shape(shim, argv):
    result = subprocess.run([shim, *argv], capture_output=True, text=True, timeout=30)
    assert result.returncode == 125
    assert result.stdout == ""
    assert "container env:" in result.stderr
