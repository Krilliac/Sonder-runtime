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
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path


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
    """Return ``(name, absolute_executable)`` or ``(None, None)``.

    ``SONDER_ISOLATED_RUNTIME`` may be ``auto`` (default), ``docker``,
    ``podman``, or ``off``.  It never accepts a caller-selected executable path.
    On Windows, batch/shim files are rejected because they would reintroduce a
    command interpreter beneath an apparently argv-only launch.
    """
    selected = os.environ.get(RUNTIME_ENV, "auto").strip().lower() or "auto"
    if selected in {"off", "none", "0", "false"}:
        return None, None
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
        return name, resolved
    return None, None


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
    rendered = str(path)
    if os.name == "nt" and (
        rendered.startswith(("\\\\", "//"))
        or not _WINDOWS_DRIVE_RE.match(rendered)
    ):
        raise ValueError("resolved Windows project path is ambiguous")
    if "," in rendered or _CONTROL_RE.search(rendered):
        raise ValueError("resolved project path is unsafe for a bind mount")
    return rendered


def build_runtime_argv(
    runtime_path,
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
    if not os.path.isabs(str(runtime_path or "")):
        raise ValueError("runtime executable must be an absolute detected path")
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
    if not writable_workspace:
        mount += ",readonly"
    argv = [
        str(runtime_path), "run", "--rm", "--pull=never",
        "--name", container_name,
        "--network=none", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", "--pids-limit=%d" % pids,
        "--memory=%dm" % memory_mb, "--cpus=%g" % cpus,
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
    # The CLI gets only OS bootstrap state.  In particular it cannot inherit
    # Docker configuration overrides, registry credentials, proxies, or Sonder
    # secrets from the MCP process.
    env = {}
    if os.name == "nt":
        for key in ("SystemRoot", "WINDIR"):
            if os.environ.get(key):
                env[key] = os.environ[key]
    return env


def _cleanup(runtime_path, container_name):
    try:
        subprocess.run(
            [runtime_path, "rm", "-f", container_name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            env=_child_environment(),
            shell=False,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _run_bounded(argv, runtime_path, container_name, stdin, timeout, output_limit):
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_child_environment(),
        shell=False,
    )
    # Bound unread pipe data as well as returned output.  An unbounded queue
    # would let a fast child consume arbitrary host memory before the main
    # thread noticed that the output cap had been crossed.
    events = queue.Queue(maxsize=8)

    def read_stream(label, stream):
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                events.put((label, chunk))
        finally:
            events.put((label, None))

    def write_stdin():
        try:
            if stdin:
                proc.stdin.write(stdin)
                proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    for label, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
        threading.Thread(target=read_stream, args=(label, stream), daemon=True).start()
    threading.Thread(target=write_stdin, daemon=True).start()

    chunks = {"stdout": [], "stderr": []}
    total = 0
    closed = set()
    reason = ""
    deadline = time.monotonic() + timeout
    while len(closed) < 2:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reason = "timed out after %ss" % timeout
            break
        try:
            label, chunk = events.get(timeout=min(0.1, remaining))
        except queue.Empty:
            if proc.poll() is not None and len(closed) == 2:
                break
            continue
        if chunk is None:
            closed.add(label)
            continue
        kept = chunk[: max(0, output_limit - total)]
        if kept:
            chunks[label].append(kept)
            total += len(kept)
        if len(kept) < len(chunk):
            reason = "combined stdout/stderr exceeded %d bytes" % output_limit
            break
    if reason:
        try:
            proc.kill()
        except OSError:
            pass
        _cleanup(runtime_path, container_name)
    try:
        returncode = proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        _cleanup(runtime_path, container_name)
        returncode = None
        reason = reason or "container client did not exit"
    return {
        "ok": not reason and returncode == 0,
        "returncode": returncode,
        "stdout": b"".join(chunks["stdout"]).decode("utf-8", "replace"),
        "stderr": b"".join(chunks["stderr"]).decode("utf-8", "replace"),
        "error": reason,
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
    runtime_name, runtime_path = detect_runtime()
    if not runtime_path:
        return {
            "ok": False, "returncode": None, "stdout": "", "stderr": "",
            "error": "isolated execution unavailable: Docker or Podman was not detected",
            "runtime": "", "project": "", "writable_workspace": False,
        }
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
        runtime_path, image, command, resolved_project,
        writable_workspace=writable_workspace is True,
        memory_mb=memory_mb, cpus=cpus, pids=pids,
    )
    result = _run_bounded(
        argv, runtime_path, name, stdin_bytes, timeout, output_limit
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
    if result.get("error"):
        lines.append("  error: %s" % result["error"])
    if result.get("stdout"):
        lines.extend(("stdout:", result["stdout"]))
    if result.get("stderr"):
        lines.extend(("stderr:", result["stderr"]))
    return "\n".join(lines)
