import io
import json
import os
import socket
import subprocess
import threading

import pytest

import isolated_runner


DOCKER_PREFIX = ("--host", "npipe:////./pipe/docker_engine")
IMAGE_ID = "sha256:" + "a" * 64


def _build(runtime, image, command, project, **kwargs):
    return isolated_runner.build_runtime_argv(
        "docker", runtime, DOCKER_PREFIX, image, command, project, **kwargs
    )


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
    assert isolated_runner.detect_runtime() is None
    assert called == []


def test_detect_runtime_is_off_or_unavailable_without_detected_binary(monkeypatch):
    monkeypatch.setenv(isolated_runner.RUNTIME_ENV, "off")
    monkeypatch.setattr(isolated_runner.shutil, "which", lambda _name: None)
    assert isolated_runner.detect_runtime() is None
    monkeypatch.setenv(isolated_runner.RUNTIME_ENV, "auto")
    assert isolated_runner.detect_runtime() is None


def test_detect_runtime_accepts_only_named_ready_engine(monkeypatch, tmp_path):
    runtime = _runtime_path(tmp_path, "docker")
    monkeypatch.setenv(isolated_runner.RUNTIME_ENV, "docker")
    monkeypatch.setattr(isolated_runner.shutil, "which", lambda name: runtime)
    monkeypatch.setattr(isolated_runner.os, "access", lambda *_args: True)
    monkeypatch.setattr(isolated_runner, "_runtime_ready", lambda *_args: DOCKER_PREFIX)
    assert isolated_runner.detect_runtime() == (
        "docker", os.path.realpath(runtime), DOCKER_PREFIX,
    )
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
    monkeypatch.setattr(
        isolated_runner, "_runtime_ready",
        lambda name, _path: DOCKER_PREFIX if name == "docker" else None,
    )
    assert isolated_runner.detect_runtime() == (
        "docker", os.path.realpath(docker), DOCKER_PREFIX,
    )


def test_remote_endpoint_is_rejected():
    assert isolated_runner._local_endpoint("npipe:////./pipe/docker_engine")
    assert isolated_runner._local_endpoint("unix:///run/user/1000/podman.sock")
    assert isolated_runner._local_endpoint("ssh://user@127.0.0.1:2222/run/podman.sock")
    assert not isolated_runner._local_endpoint("ssh://host.example/run/podman.sock")
    assert not isolated_runner._local_endpoint("tcp://10.0.0.5:2375")


def test_docker_readiness_pins_verified_endpoint_into_info_probe(monkeypatch):
    calls = []
    responses = iter([
        subprocess.CompletedProcess([], 0, stdout='"npipe:////./pipe/docker_engine"\n', stderr=""),
        subprocess.CompletedProcess([], 0, stdout="linux\n", stderr=""),
    ])
    monkeypatch.setattr(
        isolated_runner, "_probe",
        lambda argv, timeout=5: calls.append(argv) or next(responses),
    )
    assert isolated_runner._runtime_ready("docker", r"C:\docker.exe") == DOCKER_PREFIX
    assert calls[1][:4] == [r"C:\docker.exe", "--host", DOCKER_PREFIX[1], "info"]


def test_fixed_argv_has_guards_total_memory_and_recursive_readonly(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    runtime = _runtime_path(tmp_path)
    argv, name = _build(
        runtime, "python:3.12-alpine", ["python", "-c", "print('ok')"],
        str(project.resolve()), name="sonder-isolated-" + "a" * 32,
    )
    assert argv[:4] == [runtime, "--host", DOCKER_PREFIX[1], "run"]
    for guard in (
        "--network=none", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", "--pids-limit=64",
        "--memory=512m", "--memory-swap=512m", "--cpus=1",
        "--user=65534:65534", "--entrypoint=/usr/bin/env", "--pull=never",
        "--log-driver=none", "--no-healthcheck",
    ):
        assert guard in argv
    mount = argv[argv.index("--mount") + 1]
    assert mount == (
        "type=bind,src=%s,dst=/workspace,bind-recursive=disabled,readonly"
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
        runtime=_runtime_path(tmp_path), image="busybox:1.36",
        command=["true"], project=str(project.resolve()),
        name="sonder-isolated-" + "b" * 32,
    )
    readonly, _ = _build(**common)
    writable, _ = _build(**common, writable_workspace=True)
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
        _build(
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
    argv, _ = _build(
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


def test_project_mountpoint_must_exactly_match_authorized_root(monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    mounted = allowed / "mounted"
    mounted.mkdir(parents=True)
    monkeypatch.setenv(isolated_runner.ROOTS_ENV, str(allowed))
    monkeypatch.setattr(
        isolated_runner.os.path, "ismount",
        lambda path: os.path.realpath(path) == os.path.realpath(mounted),
    )
    with pytest.raises(ValueError, match="itself a mount"):
        isolated_runner.resolve_project(str(mounted))
    monkeypatch.setenv(isolated_runner.ROOTS_ENV, str(mounted))
    assert isolated_runner.resolve_project(str(mounted)) == str(mounted.resolve())


def test_post_scan_project_identity_change_is_rejected(monkeypatch):
    monkeypatch.setattr(isolated_runner, "resolve_project", lambda project: project)
    monkeypatch.setattr(isolated_runner, "_project_identity", lambda _project: (2, 3, 4))
    with pytest.raises(ValueError, match="identity changed"):
        isolated_runner._verify_project_unchanged("C:/project", (1, 2, 3))


@pytest.mark.skipif(os.name == "nt", reason="Linux mountinfo fixture is POSIX-only")
def test_nested_mount_table_entry_is_rejected(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    nested = project / "mounted"
    nested.mkdir()
    original_is_file = isolated_runner.Path.is_file
    original_read_text = isolated_runner.Path.read_text
    monkeypatch.setattr(
        isolated_runner.Path, "is_file",
        lambda path: True if str(path) == "/proc/self/mountinfo" else original_is_file(path),
    )
    monkeypatch.setattr(
        isolated_runner.Path, "read_text",
        lambda path, **kwargs: (
            "1 0 0:1 / %s rw - ext4 /dev/x rw\n" % nested
            if str(path) == "/proc/self/mountinfo" else original_read_text(path, **kwargs)
        ),
    )
    with pytest.raises(ValueError, match="nested host mount"):
        isolated_runner.resolve_project(str(project))


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
    argv, _ = _build(
        _runtime_path(tmp_path), "busybox:1.36", ["true"], str(project.resolve()),
        memory_mb=999999, cpus=999999, pids=999999,
        name="sonder-isolated-" + "e" * 32,
    )
    assert "--memory=%dm" % isolated_runner.MAX_MEMORY_MB in argv
    assert "--memory-swap=%dm" % isolated_runner.MAX_MEMORY_MB in argv
    assert "--cpus=%g" % isolated_runner.MAX_CPUS in argv
    assert "--pids-limit=%d" % isolated_runner.MAX_PIDS in argv


def test_podman_argv_uses_pinned_connection_nonrecursive_bind_and_total_cap(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    runtime = _runtime_path(tmp_path, "podman")
    prefix = ("--url", "ssh://core@127.0.0.1:2222/run/podman.sock")
    argv, _ = isolated_runner.build_runtime_argv(
        "podman", runtime, prefix, "busybox:1.36", ["true"], str(project),
        name="sonder-isolated-" + "9" * 32,
    )
    assert argv[:4] == [runtime, "--url", prefix[1], "run"]
    mount = argv[argv.index("--mount") + 1]
    assert mount.endswith("bind-nonrecursive=true,ro=true")
    assert "bind-recursive" not in mount
    assert "--log-driver=none" in argv
    assert "--no-healthcheck" in argv
    assert "--image-volume=ignore" in argv
    memory = argv.index("--memory")
    swap = argv.index("--memory-swap")
    assert argv[memory + 1] == "512m"
    assert argv[swap + 1] == "513m"


def test_runtime_memory_policy_has_bounded_engine_specific_totals():
    assert isolated_runner._memory_policy("docker", 999999) == (
        isolated_runner.MAX_MEMORY_MB,
        isolated_runner.MAX_MEMORY_MB,
        ["--memory=4096m", "--memory-swap=4096m"],
    )
    assert isolated_runner._memory_policy("podman", 999999) == (
        isolated_runner.MAX_MEMORY_MB - 1,
        isolated_runner.MAX_MEMORY_MB,
        ["--memory", "4095m", "--memory-swap", "4096m"],
    )
    assert isolated_runner._memory_policy("podman", 1)[:2] == (64, 65)
    with pytest.raises(ValueError, match="unknown container runtime"):
        isolated_runner._memory_policy("other", 512)


def test_bounded_pinned_image_inspection_returns_immutable_id(monkeypatch):
    calls = []
    monkeypatch.setattr(
        isolated_runner, "_bounded_probe",
        lambda argv, **_kwargs: calls.append(argv) or subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"id": IMAGE_ID, "volumes": None}), stderr=""
        ),
    )
    assert isolated_runner._inspect_image_policy(
        r"C:\docker.exe", DOCKER_PREFIX, "busybox:1.36"
    ) == IMAGE_ID
    assert calls[0][:4] == [r"C:\docker.exe", *DOCKER_PREFIX, "image"]
    assert calls[0][-1] == "busybox:1.36"


@pytest.mark.parametrize("volumes", [{"/data": {}}, {"/cache": None}])
def test_docker_and_podman_image_volumes_are_rejected(monkeypatch, volumes):
    monkeypatch.setattr(
        isolated_runner, "_bounded_probe",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"id": IMAGE_ID, "volumes": volumes}), stderr=""
        ),
    )
    prefixes = [
        DOCKER_PREFIX,
        ("--url", "ssh://core@127.0.0.1:2222/run/podman.sock"),
    ]
    for prefix in prefixes:
        with pytest.raises(ValueError, match="declaring OCI volumes"):
            isolated_runner._inspect_image_policy("runtime.exe", prefix, "image:tag")


def test_image_inspection_error_and_oversize_fail_closed(monkeypatch):
    monkeypatch.setattr(
        isolated_runner, "_bounded_probe",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 1, stdout="", stderr="daemon unavailable"
        ),
    )
    with pytest.raises(ValueError, match="inspection failed"):
        isolated_runner._inspect_image_policy("runtime", (), "image")
    monkeypatch.setattr(isolated_runner, "_bounded_probe", lambda *_a, **_k: None)
    with pytest.raises(ValueError, match="safety bound"):
        isolated_runner._inspect_image_policy("runtime", (), "image")


def test_metadata_probe_hard_caps_output_without_thread_leak(monkeypatch):
    class FakeProc:
        def __init__(self):
            self.returncode = None
            self.stdout = io.BytesIO(b"x" * (8 * 1024 * 1024))
            self.stderr = io.BytesIO()
        def poll(self): return self.returncode
        def wait(self, timeout=None): return self.returncode
        def kill(self): self.returncode = -9
    monkeypatch.setattr(
        isolated_runner.subprocess, "Popen", lambda *_a, **_k: FakeProc()
    )
    before = threading.active_count()
    assert isolated_runner._bounded_probe(
        ["runtime", "image", "inspect"], output_limit=1024
    ) is None
    assert threading.active_count() == before


def test_run_uses_inspected_image_id_not_mutable_tag(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    captured = {}
    monkeypatch.setattr(
        isolated_runner, "detect_runtime",
        lambda: ("docker", str((tmp_path / "docker.exe").resolve()), DOCKER_PREFIX),
    )
    monkeypatch.setattr(isolated_runner, "_inspect_image_policy", lambda *_a: IMAGE_ID)
    monkeypatch.setattr(
        isolated_runner, "_run_bounded",
        lambda argv, *_args: captured.update(argv=argv) or {
            "ok": True, "returncode": 0, "stdout": "", "stderr": "",
            "error": "", "cleanup": "not-required",
        },
    )
    result = isolated_runner.run_isolated(
        "busybox:latest", '["true"]', str(project)
    )
    assert result["ok"] is True
    assert IMAGE_ID in captured["argv"]
    assert "busybox:latest" not in captured["argv"]
    assert result["memory_limit_mb"] == 512
    assert result["memory_plus_swap_limit_mb"] == 512
    assert result["swap_allowance_mb"] == 0


def test_podman_result_reports_effective_total_and_swap_allowance(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    prefix = ("--url", "ssh://core@127.0.0.1:2222/run/podman.sock")
    monkeypatch.setattr(
        isolated_runner, "detect_runtime",
        lambda: ("podman", str((tmp_path / "podman.exe").resolve()), prefix),
    )
    monkeypatch.setattr(isolated_runner, "_inspect_image_policy", lambda *_a: IMAGE_ID)
    monkeypatch.setattr(
        isolated_runner, "_run_bounded",
        lambda *_args: {
            "ok": True, "returncode": 0, "stdout": "", "stderr": "",
            "error": "", "cleanup": "not-required",
        },
    )
    result = isolated_runner.run_isolated(
        "busybox:latest", '["true"]', str(project), memory_mb=4096
    )
    assert result["memory_limit_mb"] == 4095
    assert result["memory_plus_swap_limit_mb"] == 4096
    assert result["swap_allowance_mb"] == 1
    formatted = isolated_runner.format_result(result)
    assert "memory limit: 4095 MiB" in formatted
    assert "memory plus swap limit: 4096 MiB" in formatted
    assert "swap allowance: 1 MiB" in formatted


def test_process_launch_is_argv_only_with_minimal_bootstrap_environment(monkeypatch):
    seen = {}
    class FakeProc:
        stdout = io.BytesIO(b"ok\n")
        stderr = io.BytesIO()
        returncode = 0
        def poll(self): return self.returncode
        def wait(self, timeout=None): return self.returncode
        def kill(self): self.returncode = -9
    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen.update(kwargs)
        return FakeProc()
    monkeypatch.setattr(isolated_runner.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(isolated_runner, "_verify_project_unchanged", lambda *_a: None)
    result = isolated_runner._run_bounded(
        ["/usr/bin/docker", "run"], "docker", "/usr/bin/docker",
        DOCKER_PREFIX, "sonder-isolated-" + "f" * 32, b"", 2, 1024,
        "C:/project", (),
    )
    assert result["ok"] is True and result["stdout"] == "ok\n"
    assert seen["shell"] is False
    assert not any(key.startswith("SONDER_") for key in seen["env"])
    assert "DOCKER_HOST" not in seen["env"]


def test_ten_output_caps_have_no_reader_threads_and_verify_cleanup(monkeypatch):
    cleanup = []
    class FakeProc:
        def __init__(self):
            self.returncode = None
            self.stdout = io.BytesIO(b"x" * (8 * 1024 * 1024))
            self.stderr = io.BytesIO()
        def poll(self): return self.returncode
        def wait(self, timeout=None): return self.returncode
        def kill(self): self.returncode = -9
    def fake_popen(*_args, **_kwargs): return FakeProc()
    monkeypatch.setattr(isolated_runner.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(isolated_runner, "_verify_project_unchanged", lambda *_a: None)
    monkeypatch.setattr(
        isolated_runner, "_cleanup",
        lambda runtime_name, runtime, prefix, name: (
            cleanup.append((runtime_name, runtime, prefix, name))
            or "verified-absent"
        ),
    )
    before = threading.active_count()
    for _index in range(10):
        result = isolated_runner._run_bounded(
            ["/usr/bin/docker", "run"], "docker", "/usr/bin/docker",
            DOCKER_PREFIX, "sonder-isolated-" + "1" * 32, b"", 2, 1024,
            "C:/project", (),
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
        lambda argv, timeout=5: calls.append(argv) or subprocess.CompletedProcess(argv, 1, stdout="", stderr="failed"),
    )
    status = isolated_runner._cleanup(
        "docker", "/usr/bin/docker", DOCKER_PREFIX,
        "sonder-isolated-" + "2" * 32,
    )
    assert status == "uncertain-container-removal"
    assert len(calls) == 6


def test_cleanup_verifies_absence_after_retry(monkeypatch):
    responses = iter([
        subprocess.CompletedProcess([], 1, stdout="", stderr="rm failed"),
        subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    ])
    monkeypatch.setattr(isolated_runner, "_probe", lambda *_a, **_k: next(responses))
    assert isolated_runner._cleanup(
        "docker", "/usr/bin/docker", DOCKER_PREFIX,
        "sonder-isolated-" + "3" * 32,
    ) == "verified-absent"


def test_cleanup_does_not_treat_nonzero_query_as_absence(monkeypatch):
    responses = []
    for _ in range(3):
        responses.extend([
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            subprocess.CompletedProcess([], 1, stdout="", stderr="query failed"),
        ])
    iterator = iter(responses)
    monkeypatch.setattr(isolated_runner, "_probe", lambda *_a, **_k: next(iterator))
    assert isolated_runner._cleanup(
        "docker", "/usr/bin/docker", DOCKER_PREFIX,
        "sonder-isolated-" + "4" * 32,
    ) == "uncertain-container-removal"


def test_run_isolated_reports_unavailable_without_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(isolated_runner, "detect_runtime", lambda: None)
    result = isolated_runner.run_isolated(
        "busybox:1.36", json.dumps(["true"]), str(tmp_path.resolve())
    )
    assert result["ok"] is False and result["runtime"] == ""
    assert "unavailable" in result["error"]
