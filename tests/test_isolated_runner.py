import json
import os
import socket
import subprocess
import threading

import pytest

import isolated_runner


@pytest.fixture(autouse=True)
def _configured_isolated_root(monkeypatch, tmp_path):
    monkeypatch.setenv(isolated_runner.ROOTS_ENV, str(tmp_path.resolve()))


def _runtime_path(tmp_path, name="docker"):
    suffix = ".exe" if os.name == "nt" else ""
    path = tmp_path / (name + suffix)
    path.write_bytes(b"runtime")
    return str(path.resolve())


def test_runtime_is_off_by_default(monkeypatch):
    monkeypatch.delenv(isolated_runner.RUNTIME_ENV, raising=False)
    called = []
    monkeypatch.setattr(isolated_runner.shutil, "which", lambda name: called.append(name))
    assert isolated_runner.detect_runtime() == (None, None)
    assert called == []


def test_detect_runtime_is_off_or_unavailable_without_detected_binary(monkeypatch):
    monkeypatch.setenv(isolated_runner.RUNTIME_ENV, "off")
    monkeypatch.setattr(isolated_runner.shutil, "which", lambda _name: None)
    assert isolated_runner.detect_runtime() == (None, None)
    monkeypatch.setenv(isolated_runner.RUNTIME_ENV, "auto")
    assert isolated_runner.detect_runtime() == (None, None)


def test_detect_runtime_accepts_only_named_ready_engine(monkeypatch, tmp_path):
    runtime = _runtime_path(tmp_path, "docker")
    monkeypatch.setenv(isolated_runner.RUNTIME_ENV, "docker")
    monkeypatch.setattr(isolated_runner.shutil, "which", lambda name: runtime)
    monkeypatch.setattr(isolated_runner.os, "access", lambda *_args: True)
    monkeypatch.setattr(isolated_runner, "_runtime_ready", lambda *_args: True)
    assert isolated_runner.detect_runtime() == ("docker", os.path.realpath(runtime))
    monkeypatch.setenv(isolated_runner.RUNTIME_ENV, str(tmp_path / "evil"))
    with pytest.raises(ValueError, match="must be auto"):
        isolated_runner.detect_runtime()


def test_runtime_falls_back_from_stopped_podman_to_ready_docker(monkeypatch, tmp_path):
    podman = _runtime_path(tmp_path, "podman")
    docker = _runtime_path(tmp_path, "docker")
    monkeypatch.setenv(isolated_runner.RUNTIME_ENV, "auto")
    monkeypatch.setattr(
        isolated_runner.shutil, "which",
        lambda name: podman if name == "podman" else docker,
    )
    monkeypatch.setattr(isolated_runner.os, "access", lambda *_args: True)
    monkeypatch.setattr(isolated_runner, "_runtime_ready", lambda name, _path: name == "docker")
    assert isolated_runner.detect_runtime() == ("docker", os.path.realpath(docker))


def test_remote_endpoint_is_rejected():
    assert isolated_runner._local_endpoint("npipe:////./pipe/docker_engine")
    assert isolated_runner._local_endpoint("unix:///run/user/1000/podman.sock")
    assert isolated_runner._local_endpoint("ssh://user@127.0.0.1:2222/run/podman.sock")
    assert not isolated_runner._local_endpoint("ssh://host.example/run/podman.sock")
    assert not isolated_runner._local_endpoint("tcp://10.0.0.5:2375")


def test_fixed_argv_has_guards_total_memory_and_recursive_readonly(tmp_path):
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
        "--memory=512m", "--memory-swap=512m", "--cpus=1",
        "--user=65534:65534", "--entrypoint=/usr/bin/env", "--pull=never",
    ):
        assert guard in argv
    mount = argv[argv.index("--mount") + 1]
    assert mount == (
        "type=bind,src=%s,dst=/workspace,readonly,bind-recursive=readonly"
        % project.resolve()
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
    assert ro_mount == rw_mount + ",readonly,bind-recursive=readonly"
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


def test_command_flags_remain_after_image(tmp_path):
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


def test_paths_with_mount_delimiters_and_relative_paths_fail_closed(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(ValueError, match="absolute"):
        isolated_runner.resolve_project("relative/project")
    with pytest.raises(ValueError, match="commas"):
        isolated_runner.resolve_project(str(project.resolve()) + ",dst=/host")


def test_project_outside_explicit_authorized_roots_is_rejected(monkeypatch, tmp_path):
    allowed, outside = tmp_path / "allowed", tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    monkeypatch.setenv(isolated_runner.ROOTS_ENV, str(allowed))
    with pytest.raises(ValueError, match="outside configured"):
        isolated_runner.resolve_project(str(outside))


def test_filesystem_root_cannot_be_authorized(monkeypatch):
    monkeypatch.setenv(isolated_runner.ROOTS_ENV, os.path.abspath(os.sep))
    with pytest.raises(ValueError, match="authorized root is unsafe"):
        isolated_runner.authorized_roots()


@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX socket fixture is POSIX-only")
def test_project_tree_with_socket_is_rejected(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.bind(str(project / "agent.sock"))
        with pytest.raises(ValueError, match="socket or special"):
            isolated_runner.resolve_project(str(project))
    finally:
        sock.close()


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
    assert "--memory-swap=%dm" % isolated_runner.MAX_MEMORY_MB in argv
    assert "--cpus=%g" % isolated_runner.MAX_CPUS in argv
    assert "--pids-limit=%d" % isolated_runner.MAX_PIDS in argv


def test_process_launch_is_argv_only_with_minimal_bootstrap_environment(monkeypatch):
    seen = {}
    class FakeProc:
        returncode = 0
        def poll(self): return self.returncode
        def wait(self, timeout=None): return self.returncode
        def kill(self): self.returncode = -9
    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen.update(kwargs)
        kwargs["stdout"].write(b"ok\n")
        kwargs["stdout"].flush()
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


def test_ten_output_caps_have_no_reader_threads_and_verify_cleanup(monkeypatch):
    cleanup = []
    class FakeProc:
        def __init__(self): self.returncode = None
        def poll(self): return self.returncode
        def wait(self, timeout=None): return self.returncode
        def kill(self): self.returncode = -9
    def fake_popen(*_args, **kwargs):
        kwargs["stdout"].write(b"x" * 8192)
        kwargs["stdout"].flush()
        return FakeProc()
    monkeypatch.setattr(isolated_runner.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        isolated_runner, "_cleanup",
        lambda runtime, name: cleanup.append((runtime, name)) or "verified-absent",
    )
    before = threading.active_count()
    for _index in range(10):
        result = isolated_runner._run_bounded(
            ["/usr/bin/docker", "run"], "/usr/bin/docker",
            "sonder-isolated-" + "1" * 32, b"", 2, 1024,
        )
        assert result["ok"] is False
        assert len(result["stdout"].encode()) == 1024
        assert result["cleanup"] == "verified-absent"
    assert threading.active_count() == before
    assert len(cleanup) == 10


def test_cleanup_retries_and_surfaces_uncertainty(monkeypatch):
    calls = []
    monkeypatch.setattr(
        isolated_runner, "_probe",
        lambda argv, timeout=5: calls.append(argv) or None,
    )
    status = isolated_runner._cleanup(
        "/usr/bin/docker", "sonder-isolated-" + "2" * 32
    )
    assert status == "uncertain-container-removal"
    assert len(calls) == 6


def test_cleanup_verifies_absence_after_retry(monkeypatch):
    responses = iter([
        subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0),
        subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 1),
    ])
    monkeypatch.setattr(isolated_runner, "_probe", lambda *_a, **_k: next(responses))
    assert isolated_runner._cleanup("/usr/bin/docker", "sonder-isolated-" + "3" * 32) == "verified-absent"


def test_run_isolated_reports_unavailable_without_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(isolated_runner, "detect_runtime", lambda: (None, None))
    result = isolated_runner.run_isolated(
        "busybox:1.36", json.dumps(["true"]), str(tmp_path.resolve())
    )
    assert result["ok"] is False and result["runtime"] == ""
    assert "unavailable" in result["error"]
