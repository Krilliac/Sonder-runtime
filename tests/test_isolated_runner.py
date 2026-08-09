import io
import json
import os

import pytest

import isolated_runner


def _runtime_path(tmp_path, name="docker"):
    suffix = ".exe" if os.name == "nt" else ""
    path = tmp_path / (name + suffix)
    path.write_bytes(b"runtime")
    return str(path.resolve())


def test_detect_runtime_is_off_or_unavailable_without_detected_binary(monkeypatch):
    monkeypatch.setenv(isolated_runner.RUNTIME_ENV, "off")
    monkeypatch.setattr(isolated_runner.shutil, "which", lambda _name: None)
    assert isolated_runner.detect_runtime() == (None, None)
    monkeypatch.setenv(isolated_runner.RUNTIME_ENV, "auto")
    assert isolated_runner.detect_runtime() == (None, None)


def test_detect_runtime_accepts_only_named_docker_or_podman(monkeypatch, tmp_path):
    runtime = _runtime_path(tmp_path, "docker")
    monkeypatch.setenv(isolated_runner.RUNTIME_ENV, "docker")
    monkeypatch.setattr(
        isolated_runner.shutil, "which", lambda name: runtime if name == "docker" else None
    )
    monkeypatch.setattr(isolated_runner.os, "access", lambda *_args: True)
    assert isolated_runner.detect_runtime() == ("docker", os.path.realpath(runtime))
    monkeypatch.setenv(isolated_runner.RUNTIME_ENV, str(tmp_path / "evil"))
    with pytest.raises(ValueError, match="must be auto"):
        isolated_runner.detect_runtime()


def test_fixed_argv_has_all_guards_and_one_exact_read_only_mount(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    runtime = _runtime_path(tmp_path)
    argv, name = isolated_runner.build_runtime_argv(
        runtime, "python:3.12-alpine", ["python", "-c", "print('ok')"],
        str(project.resolve()), name="sonder-isolated-" + "a" * 32,
    )
    for guard in (
        "--network=none", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", "--pids-limit=64",
        "--memory=512m", "--cpus=1", "--user=65534:65534",
        "--entrypoint=/usr/bin/env", "--pull=never",
    ):
        assert guard in argv
    mount_index = argv.index("--mount")
    assert argv[mount_index + 1] == (
        "type=bind,src=%s,dst=/workspace,readonly" % project.resolve()
    )
    assert sum(item.startswith("type=bind,") for item in argv) == 1
    assert not any(item == "--privileged" or item.startswith("--device") for item in argv)
    assert not any("docker.sock" in item or "podman.sock" in item for item in argv)
    assert argv[-3:] == ["python", "-c", "print('ok')"]
    assert name == "sonder-isolated-" + "a" * 32


def test_writable_workspace_changes_only_mount_mode(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    common = dict(
        runtime_path=_runtime_path(tmp_path), image="busybox:1.36",
        command=["true"], project=str(project.resolve()),
        name="sonder-isolated-" + "b" * 32,
    )
    readonly, _ = isolated_runner.build_runtime_argv(**common)
    writable, _ = isolated_runner.build_runtime_argv(**common, writable_workspace=True)
    ro_mount = readonly[readonly.index("--mount") + 1]
    rw_mount = writable[writable.index("--mount") + 1]
    assert ro_mount == rw_mount + ",readonly"
    assert [x for x in readonly if x != ro_mount] == [x for x in writable if x != rw_mount]


@pytest.mark.parametrize(
    "image",
    ["--privileged", "-v", "/host/image", "ok image", "name,readonly", "name=evil", "../image"],
)
def test_image_cannot_smuggle_runtime_flags(image, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(ValueError):
        isolated_runner.build_runtime_argv(
            _runtime_path(tmp_path), image, ["true"], str(project.resolve()),
            name="sonder-isolated-" + "c" * 32,
        )


@pytest.mark.parametrize(
    "command",
    [[], ["ok", "bad\x00arg"], ["ok", "line\nbreak"], ["ok", 7], "not-json-array"],
)
def test_command_argv_rejects_ambiguous_or_non_argv_input(command):
    with pytest.raises(ValueError):
        isolated_runner._parse_argv(command)


def test_command_arguments_remain_after_image_and_cannot_be_runtime_flags(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    argv, _ = isolated_runner.build_runtime_argv(
        _runtime_path(tmp_path), "busybox:1.36",
        ["printf", "--privileged", "--mount=/host"], str(project.resolve()),
        name="sonder-isolated-" + "d" * 32,
    )
    image_index = argv.index("busybox:1.36")
    assert argv.index("--privileged") > image_index
    assert argv.index("--mount=/host") > image_index
    assert argv[image_index + 1 : image_index + 7] == [
        "-i", "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME=/tmp", "TMPDIR=/tmp", "LANG=C.UTF-8", "--",
    ]


def test_paths_with_mount_delimiters_and_relative_paths_fail_closed(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(ValueError, match="absolute"):
        isolated_runner.resolve_project("relative/project")
    with pytest.raises(ValueError, match="commas"):
        isolated_runner.resolve_project(str(project.resolve()) + ",dst=/host")


def test_windows_unc_and_non_drive_paths_fail_before_translation(monkeypatch):
    monkeypatch.setattr(isolated_runner.os, "name", "nt", raising=False)
    with pytest.raises(ValueError, match="UNC"):
        isolated_runner.resolve_project(r"\\server\share\repo")
    with pytest.raises(ValueError, match="drive-qualified"):
        isolated_runner.resolve_project(r"\repo")


def test_resource_requests_are_hard_clamped(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    argv, _ = isolated_runner.build_runtime_argv(
        _runtime_path(tmp_path), "busybox:1.36", ["true"], str(project.resolve()),
        memory_mb=999999, cpus=999999, pids=999999,
        name="sonder-isolated-" + "e" * 32,
    )
    assert "--memory=%dm" % isolated_runner.MAX_MEMORY_MB in argv
    assert "--cpus=%g" % isolated_runner.MAX_CPUS in argv
    assert "--pids-limit=%d" % isolated_runner.MAX_PIDS in argv


def test_process_launch_is_argv_only_with_scrubbed_host_environment(monkeypatch):
    seen = {}
    class FakeProc:
        def __init__(self):
            self.stdin, self.stdout, self.stderr = io.BytesIO(), io.BytesIO(b"ok\n"), io.BytesIO()
            self.returncode = 0
        def poll(self): return self.returncode
        def wait(self, timeout=None): return self.returncode
        def kill(self): self.returncode = -9
    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen.update(kwargs)
        return FakeProc()
    monkeypatch.setattr(isolated_runner.subprocess, "Popen", fake_popen)
    result = isolated_runner._run_bounded(
        ["/usr/bin/docker", "run"], "/usr/bin/docker",
        "sonder-isolated-" + "f" * 32, b"", 2, 1024,
    )
    assert result["ok"] is True and result["stdout"] == "ok\n"
    assert seen["shell"] is False
    assert not any(key.startswith("SONDER_") for key in seen["env"])
    assert "DOCKER_HOST" not in seen["env"]


def test_output_cap_kills_client_and_removes_named_container(monkeypatch):
    cleanup = []
    class FakeProc:
        def __init__(self):
            self.stdin, self.stdout, self.stderr = io.BytesIO(), io.BytesIO(b"x" * 8192), io.BytesIO()
            self.returncode = None
        def poll(self): return self.returncode
        def wait(self, timeout=None): return self.returncode
        def kill(self): self.returncode = -9
    monkeypatch.setattr(isolated_runner.subprocess, "Popen", lambda *_a, **_k: FakeProc())
    monkeypatch.setattr(isolated_runner, "_cleanup", lambda runtime, name: cleanup.append((runtime, name)))
    result = isolated_runner._run_bounded(
        ["/usr/bin/docker", "run"], "/usr/bin/docker",
        "sonder-isolated-" + "1" * 32, b"", 2, 1024,
    )
    assert result["ok"] is False
    assert len(result["stdout"].encode()) == 1024
    assert "exceeded 1024 bytes" in result["error"]
    assert cleanup == [("/usr/bin/docker", "sonder-isolated-" + "1" * 32)]


def test_run_isolated_reports_unavailable_without_falling_back(monkeypatch, tmp_path):
    monkeypatch.setattr(isolated_runner, "detect_runtime", lambda: (None, None))
    result = isolated_runner.run_isolated(
        "busybox:1.36", json.dumps(["true"]), str(tmp_path.resolve())
    )
    assert result["ok"] is False and result["runtime"] == ""
    assert "unavailable" in result["error"]
