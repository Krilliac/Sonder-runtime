"""Optional container-backed execution with a deliberately narrow contract.

This module is stronger isolation than :mod:`code_runner`, but it is not a
security boundary by itself.  It depends on the selected Docker/Podman daemon,
container runtime, host kernel, and image being correctly patched and
configured.  Only a fixed argv template is emitted; callers cannot add mounts,
devices, runtime flags, privileges, environment variables, or a container user.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 120
DEFAULT_MEMORY_MB = 512
MAX_MEMORY_MB = 4096
DEFAULT_CPUS = 1.0
MAX_CPUS = 4.0
DEFAULT_PIDS = 64
MAX_PIDS = 256
DEFAULT_OUTPUT_BYTES = 128 * 1024
MAX_OUTPUT_BYTES = 256 * 1024
MAX_STDIN_BYTES = 64 * 1024
MAX_ARGV_ITEMS = 64
MAX_ARG_BYTES = 4096
RUNTIME_ENV = "SONDER_ISOLATED_RUNTIME"
ROOTS_ENV = "SONDER_ISOLATED_ROOTS"
MAX_PROJECT_ENTRIES = 100_000

_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,254}$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _clamp_int(value, default, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _clamp_float(value, default, minimum, maximum):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        parsed = default
    return max(minimum, min(parsed, maximum))


def detect_runtime():
    """Return ``(name, absolute_executable, pinned_global_argv)`` or ``None``.

    ``SONDER_ISOLATED_RUNTIME`` defaults to ``off`` and may be explicitly set
    to ``auto``, ``docker``, or ``podman``. It never accepts a caller-selected
    executable path.
    On Windows, batch/shim files are rejected because they would reintroduce a
    command interpreter beneath an apparently argv-only launch.
    """
    selected = os.environ.get(RUNTIME_ENV, "off").strip().lower() or "off"
    if selected in {"off", "none", "0", "false"}:
        return None
    if selected not in {"auto", "docker", "podman"}:
        raise ValueError(
            "%s must be auto, docker, podman, or off" % RUNTIME_ENV
        )
    candidates = ("podman", "docker") if selected == "auto" else (selected,)
    for name in candidates:
        found = shutil.which(name)
        if not found:
            continue
        resolved = os.path.realpath(found)
        if not os.path.isfile(resolved):
            continue
        if os.name == "nt" and Path(resolved).suffix.casefold() != ".exe":
            continue
        if os.name != "nt" and not os.access(resolved, os.X_OK):
            continue
        pinned = _runtime_ready(name, resolved)
        if pinned is not None:
            return name, resolved, tuple(pinned)
    return None


def _runtime_environment():
    """Minimal host bootstrap state for local Desktop/machine runtimes."""
    env = {}
    keys = (
        ("SystemRoot", "WINDIR", "USERPROFILE", "APPDATA", "LOCALAPPDATA")
        if os.name == "nt"
        else ("HOME", "XDG_RUNTIME_DIR")
    )
    for key in keys:
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def _probe(argv, timeout=5):
    try:
        return subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=timeout, env=_runtime_environment(), shell=False, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _local_endpoint(uri):
    value = str(uri or "").strip().strip('"')
    if value.startswith(("unix://", "npipe://")):
        return True
    parsed = urlparse(value)
    return parsed.scheme in {"tcp", "http", "https", "ssh"} and (
        parsed.hostname or ""
    ).casefold() in {"localhost", "127.0.0.1", "::1"}


def _runtime_ready(name, path):
    """Return pinned global argv for a responsive local Linux engine."""
    if name == "docker":
        context = _probe([
            path, "context", "inspect", "--format",
            "{{json .Endpoints.docker.Host}}",
        ])
        if not context or context.returncode != 0:
            return None
        endpoint = context.stdout.strip().strip('"')
        if not _local_endpoint(endpoint):
            return None
        pinned = ("--host", endpoint)
        info = _probe([path, *pinned, "info", "--format", "{{.OSType}}"])
        return pinned if (
            info and info.returncode == 0 and info.stdout.strip() == "linux"
        ) else None
    connections = _probe([path, "system", "connection", "list", "--format", "json"])
    if not connections or connections.returncode != 0:
        return None
    try:
        rows = json.loads(connections.stdout or "[]")
    except (TypeError, ValueError):
        return None
    def field(row, wanted):
        return next(
            (value for key, value in row.items() if str(key).casefold() == wanted),
            None,
        )

    defaults = [
        row for row in rows
        if isinstance(row, dict) and field(row, "default") is True
    ]
    if rows and not defaults:
        return None
    if defaults and not all(_local_endpoint(field(row, "uri")) for row in defaults):
        return None
    if len(defaults) > 1:
        return None
    pinned = ()
    if defaults:
        row = defaults[0]
        uri = str(field(row, "uri") or "").strip()
        pinned = ("--url", uri)
        identity = str(field(row, "identity") or "").strip()
        if identity:
            if _CONTROL_RE.search(identity):
                return None
            pinned += ("--identity", identity)
    info = _probe([path, *pinned, "info", "--format", "json"])
    if not info or info.returncode != 0:
        return None
    try:
        payload = json.loads(info.stdout or "{}")
    except (TypeError, ValueError):
        return None
    return pinned if (
        str(payload.get("host", {}).get("os", "")).casefold() == "linux"
    ) else None


def _parse_argv(argv_json):
    try:
        value = json.loads(argv_json) if isinstance(argv_json, str) else argv_json
    except (TypeError, ValueError) as exc:
        raise ValueError("argv_json must be valid JSON: %s" % exc) from exc
    if not isinstance(value, list) or not value:
        raise ValueError("argv_json must be a non-empty JSON array")
    if len(value) > MAX_ARGV_ITEMS:
        raise ValueError("argv_json exceeds %d items" % MAX_ARGV_ITEMS)
    argv = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValueError("every argv item must be a non-empty string")
        if len(item.encode("utf-8")) > MAX_ARG_BYTES:
            raise ValueError("argv item exceeds %d bytes" % MAX_ARG_BYTES)
        if _CONTROL_RE.search(item):
            raise ValueError("argv items cannot contain control characters")
        argv.append(item)
    return argv


def _validate_image(image):
    image = str(image or "").strip()
    if not _IMAGE_RE.fullmatch(image) or image.startswith(("-", "/")):
        raise ValueError("image must be a fixed OCI image reference")
    if ".." in image or "," in image or "=" in image:
        raise ValueError("image contains an ambiguous or unsupported character")
    return image


def _is_inside(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _prohibited_root(path):
    if path == Path(path.anchor):
        return True
    if os.name != "nt":
        prohibited = tuple(Path(item) for item in ("/proc", "/sys", "/dev", "/run"))
        if any(_is_inside(path, item) for item in prohibited):
            return True
    return False


def authorized_roots():
    raw = os.environ.get(ROOTS_ENV, "").strip()
    if not raw:
        return []
    roots = []
    for item in raw.split(os.pathsep):
        item = item.strip()
        if not item:
            continue
        path = Path(item).expanduser().resolve(strict=True)
        if not path.is_dir() or _prohibited_root(path):
            raise ValueError("isolated authorized root is unsafe: %s" % path)
        if path not in roots:
            roots.append(path)
    return roots


def _host_mount_points():
    if os.name == "nt" or not Path("/proc/self/mountinfo").is_file():
        return ()
    try:
        mount_lines = Path("/proc/self/mountinfo").read_text(
            encoding="utf-8", errors="strict"
        ).splitlines()
    except OSError as exc:
        raise ValueError("cannot inspect host mount table") from exc
    escapes = {"\\040": " ", "\\011": "\t", "\\012": "\n", "\\134": "\\"}
    points = []
    for line in mount_lines:
        fields = line.split(" - ", 1)[0].split()
        if len(fields) < 5:
            raise ValueError("host mount table contains an ambiguous entry")
        rendered = fields[4]
        for encoded, decoded in escapes.items():
            rendered = rendered.replace(encoded, decoded)
        points.append(Path(rendered))
    return tuple(points)


def _reject_special_project_entries(project):
    """Reject submounts, reparse points, links, sockets, and device-like files."""
    project = Path(project)
    for mount_point in _host_mount_points():
        if mount_point != project and _is_inside(mount_point, project):
            raise ValueError("project contains a nested host mount: %s" % mount_point)
    stack = [project]
    seen = 0
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    isjunction = getattr(os.path, "isjunction", lambda _path: False)
    while stack:
        directory = stack.pop()
        try:
            entries = os.scandir(directory)
        except OSError as exc:
            raise ValueError("cannot safely inspect project tree: %s" % exc) from exc
        with entries:
            for entry in entries:
                seen += 1
                if seen > MAX_PROJECT_ENTRIES:
                    raise ValueError(
                        "project safety scan exceeds %d entries" % MAX_PROJECT_ENTRIES
                    )
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise ValueError(
                        "cannot safely inspect project entry: %s" % entry.path
                    ) from exc
                mode = info.st_mode
                if (
                    entry.is_symlink()
                    or isjunction(entry.path)
                    or (
                        reparse_flag
                        and getattr(info, "st_file_attributes", 0) & reparse_flag
                    )
                    or os.path.ismount(entry.path)
                ):
                    raise ValueError(
                        "project contains a link, reparse point, or submount: %s"
                        % entry.path
                    )
                if stat.S_ISDIR(mode):
                    stack.append(Path(entry.path))
                elif not stat.S_ISREG(mode):
                    raise ValueError(
                        "project contains a socket or special entry: %s" % entry.path
                    )


def resolve_project(project):
    raw = str(project or "").strip()
    if not raw:
        raise ValueError("project is required")
    if _CONTROL_RE.search(raw) or "," in raw:
        raise ValueError("project path cannot contain commas or control characters")
    if os.name == "nt":
        if raw.startswith(("\\\\", "//", "\\\\?\\", "\\\\.\\")):
            raise ValueError("UNC and Windows device paths are not supported")
        if not _WINDOWS_DRIVE_RE.match(raw):
            raise ValueError("project must be an absolute drive-qualified Windows path")
    elif not os.path.isabs(raw):
        raise ValueError("project must be an absolute path")
    path = Path(raw).resolve(strict=True)
    if not path.is_dir():
        raise ValueError("project is not a directory: %s" % path)
    if _prohibited_root(path):
        raise ValueError("project root is a filesystem/runtime/device root")
    roots = authorized_roots()
    if not roots:
        raise ValueError("no isolated project roots configured in %s" % ROOTS_ENV)
    if not any(_is_inside(path, root) for root in roots):
        raise ValueError("project is outside configured isolated roots")
    project_is_mount = os.path.ismount(path) or path in _host_mount_points()
    if project_is_mount and path not in roots:
        raise ValueError(
            "project is itself a mount and must exactly match an authorized root"
        )
    rendered = str(path)
    if os.name == "nt" and (
        rendered.startswith(("\\\\", "//"))
        or not _WINDOWS_DRIVE_RE.match(rendered)
    ):
        raise ValueError("resolved Windows project path is ambiguous")
    if "," in rendered or _CONTROL_RE.search(rendered):
        raise ValueError("resolved project path is unsafe for a bind mount")
    _reject_special_project_entries(path)
    return rendered


def _project_identity(project):
    info = os.stat(project, follow_symlinks=False)
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))


def _verify_project_unchanged(project, expected_identity):
    resolved = resolve_project(project)
    if resolved != project or _project_identity(resolved) != expected_identity:
        raise ValueError("project identity changed between safety scan and launch")


def build_runtime_argv(
    runtime_name,
    runtime_path,
    runtime_prefix,
    image,
    command,
    project,
    *,
    writable_workspace=False,
    memory_mb=DEFAULT_MEMORY_MB,
    cpus=DEFAULT_CPUS,
    pids=DEFAULT_PIDS,
    name=None,
):
    """Build the complete fixed Docker/Podman argv without executing it."""
    if runtime_name not in {"docker", "podman"}:
        raise ValueError("runtime must be detected Docker or Podman")
    if not os.path.isabs(str(runtime_path or "")):
        raise ValueError("runtime executable must be an absolute detected path")
    runtime_prefix = tuple(runtime_prefix or ())
    if runtime_name == "docker":
        if (
            len(runtime_prefix) != 2
            or runtime_prefix[0] != "--host"
            or not _local_endpoint(runtime_prefix[1])
        ):
            raise ValueError("Docker invocation is not pinned to a verified local endpoint")
    elif runtime_prefix:
        if (
            len(runtime_prefix) not in {2, 4}
            or runtime_prefix[0] != "--url"
            or not _local_endpoint(runtime_prefix[1])
            or (len(runtime_prefix) == 4 and runtime_prefix[2] != "--identity")
        ):
            raise ValueError("Podman invocation is not pinned to a verified local connection")
    image = _validate_image(image)
    command = _parse_argv(command)
    project = resolve_project(project)
    memory_mb = _clamp_int(memory_mb, DEFAULT_MEMORY_MB, 64, MAX_MEMORY_MB)
    cpus = _clamp_float(cpus, DEFAULT_CPUS, 0.1, MAX_CPUS)
    pids = _clamp_int(pids, DEFAULT_PIDS, 16, MAX_PIDS)
    container_name = name or ("sonder-isolated-" + uuid.uuid4().hex)
    if not re.fullmatch(r"sonder-isolated-[a-f0-9]{32}", container_name):
        raise ValueError("invalid internal container name")
    mount = "type=bind,src=%s,dst=/workspace" % project
    mount += (
        ",bind-recursive=disabled"
        if runtime_name == "docker"
        else ",bind-nonrecursive=true"
    )
    if not writable_workspace:
        mount += ",readonly" if runtime_name == "docker" else ",ro=true"
    memory_args = (
        ["--memory=%dm" % memory_mb, "--memory-swap=%dm" % memory_mb]
        if runtime_name == "docker"
        else ["--memory", "%dm" % memory_mb, "--memory-swap", "%dm" % memory_mb]
    )
    argv = [
        str(runtime_path), *runtime_prefix, "run", "--rm", "--pull=never",
        "--name", container_name,
        "--network=none", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", "--pids-limit=%d" % pids,
        *memory_args, "--cpus=%g" % cpus,
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m",
        "--user=65534:65534", "--workdir=/workspace",
        "--mount", mount,
        "--entrypoint=/usr/bin/env", image,
        "-i", "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME=/tmp", "TMPDIR=/tmp", "LANG=C.UTF-8", "--",
    ]
    argv.extend(command)
    return argv, container_name


def _child_environment():
    return _runtime_environment()


def _cleanup(runtime_name, runtime_path, runtime_prefix, container_name):
    """Retry removal and verify absence through a successful exact-name list."""
    pattern = (
        "name=^/%s$" % container_name
        if runtime_name == "docker"
        else "name=^%s$" % container_name
    )
    for _attempt in range(3):
        _probe([runtime_path, *runtime_prefix, "rm", "-f", container_name])
        queried = _probe([
            runtime_path, *runtime_prefix, "container", "ls", "-a",
            "--filter", pattern, "--format", "{{.Names}}",
        ])
        if queried is not None and queried.returncode == 0:
            names = {
                line.strip().lstrip("/")
                for line in (queried.stdout or "").splitlines()
                if line.strip()
            }
            if container_name not in names:
                return "verified-absent"
    return "uncertain-container-removal"


def _run_bounded(
    argv, runtime_name, runtime_path, runtime_prefix, container_name, stdin,
    timeout, output_limit, project, expected_identity,
):
    # Reader threads write only to fixed-size bytearrays. Pipes add bounded OS
    # buffering, and every exit path reaps the CLI, closes the streams, and
    # joins both readers before returning.
    _verify_project_unchanged(project, expected_identity)
    with tempfile.TemporaryFile() as input_file:
        input_file.write(stdin)
        input_file.seek(0)
        proc = subprocess.Popen(
            argv, stdin=input_file, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=_child_environment(), shell=False,
        )
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        total = [0]
        output_lock = threading.Lock()
        output_exceeded = threading.Event()

        def drain(label, stream):
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    return
                with output_lock:
                    remaining = max(0, output_limit - total[0])
                    kept = chunk[:remaining]
                    if kept:
                        buffers[label].extend(kept)
                        total[0] += len(kept)
                    if len(kept) < len(chunk):
                        output_exceeded.set()
                        return

        readers = [
            threading.Thread(target=drain, args=(label, stream), daemon=False)
            for label, stream in (("stdout", proc.stdout), ("stderr", proc.stderr))
        ]
        for reader in readers:
            reader.start()
        reason = ""
        try:
            _verify_project_unchanged(project, expected_identity)
        except (OSError, ValueError) as exc:
            reason = str(exc)
        deadline = time.monotonic() + timeout
        while not reason and proc.poll() is None:
            if output_exceeded.is_set():
                reason = "combined stdout/stderr exceeded %d bytes" % output_limit
                break
            if time.monotonic() >= deadline:
                reason = "timed out after %ss" % timeout
                break
            time.sleep(0.01)
        if not reason and output_exceeded.is_set():
            reason = "combined stdout/stderr exceeded %d bytes" % output_limit

        cleanup_status = "not-required"
        if reason:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                returncode = proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                returncode = None
                reason += "; container CLI did not exit after kill"
            # The CLI is reaped before daemon cleanup.  Removal is retried and
            # then independently verified by exact generated container name.
            cleanup_status = _cleanup(
                runtime_name, runtime_path, runtime_prefix, container_name
            )
            if cleanup_status != "verified-absent":
                reason += "; container removal could not be verified"
        else:
            returncode = proc.wait(timeout=3)
        for reader in readers:
            reader.join(timeout=3)
        if any(reader.is_alive() for reader in readers):
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
            for reader in readers:
                reader.join(timeout=1)
        if any(reader.is_alive() for reader in readers):
            reason = (reason + "; " if reason else "") + "output reader teardown uncertain"
        return {
            "ok": not reason and returncode == 0,
            "returncode": returncode,
            "stdout": bytes(buffers["stdout"]).decode("utf-8", "replace"),
            "stderr": bytes(buffers["stderr"]).decode("utf-8", "replace"),
            "error": reason,
            "cleanup": cleanup_status,
        }


def run_isolated(
    image,
    argv_json,
    project,
    *,
    stdin="",
    writable_workspace=False,
    timeout=DEFAULT_TIMEOUT,
    memory_mb=DEFAULT_MEMORY_MB,
    cpus=DEFAULT_CPUS,
    pids=DEFAULT_PIDS,
    output_bytes=DEFAULT_OUTPUT_BYTES,
):
    selected_runtime = detect_runtime()
    if not selected_runtime:
        return {
            "ok": False, "returncode": None, "stdout": "", "stderr": "",
            "error": (
                "isolated execution unavailable: explicitly enable a ready local "
                "Docker or Podman engine with %s" % RUNTIME_ENV
            ),
            "runtime": "", "project": "", "writable_workspace": False,
        }
    runtime_name, runtime_path, runtime_prefix = selected_runtime
    timeout = _clamp_int(timeout, DEFAULT_TIMEOUT, 1, MAX_TIMEOUT)
    output_limit = _clamp_int(
        output_bytes, DEFAULT_OUTPUT_BYTES, 1024, MAX_OUTPUT_BYTES
    )
    stdin_bytes = str(stdin or "").encode("utf-8")
    if len(stdin_bytes) > MAX_STDIN_BYTES:
        raise ValueError("stdin exceeds %d bytes" % MAX_STDIN_BYTES)
    command = _parse_argv(argv_json)
    resolved_project = resolve_project(project)
    argv, name = build_runtime_argv(
        runtime_name, runtime_path, runtime_prefix,
        image, command, resolved_project,
        writable_workspace=writable_workspace is True,
        memory_mb=memory_mb, cpus=cpus, pids=pids,
    )
    expected_identity = _project_identity(resolved_project)
    result = _run_bounded(
        argv, runtime_name, runtime_path, runtime_prefix, name, stdin_bytes,
        timeout, output_limit, resolved_project, expected_identity,
    )
    result.update({
        "runtime": runtime_name,
        "project": resolved_project,
        "writable_workspace": writable_workspace is True,
        "timeout": timeout,
        "output_limit": output_limit,
    })
    return result


def format_result(result):
    lines = [
        "isolated status: %s" % ("ok" if result.get("ok") else "failed"),
        "  runtime: %s" % (result.get("runtime") or "unavailable"),
        "  returncode: %s" % result.get("returncode"),
        "  project: %s" % (result.get("project") or "(none)"),
        "  workspace: %s" % (
            "writable (explicit host request)"
            if result.get("writable_workspace") else "read-only"
        ),
    ]
    if result.get("cleanup"):
        lines.append("  cleanup: %s" % result["cleanup"])
    if result.get("error"):
        lines.append("  error: %s" % result["error"])
    if result.get("stdout"):
        lines.extend(("stdout:", result["stdout"]))
    if result.get("stderr"):
        lines.extend(("stderr:", result["stderr"]))
    return "\n".join(lines)
