"""Opt-in, source-scoped Linux container builds for active Codegen canaries.

Only the exact host-granted build inputs and model-declared source files are
copied into a read-only container bind. The build launcher in the pinned image
copies them to a memory-bounded tmpfs before invoking the requested command.
No host state directory, home, container socket, or writable host directory is
mounted. Build output is still candidate-controlled diagnostic data, not an
independent task grade or authority to approve a generated change.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import sys
from pathlib import Path

from sonder_runtime.adapters.execution import isolated_runner
from sonder_runtime.platform.paths import default_home, state_path

IMAGE_ENV = "SONDER_CODEGEN_BUILD_IMAGE"
PROJECT_ENV = "SONDER_CODEGEN_BUILD_PROJECT"
STAGING_ENV = "SONDER_CODEGEN_BUILD_STAGING_ROOT"
SOURCES_ENV = "SONDER_CODEGEN_BUILD_ALLOWED_SOURCES"
INPUTS_ENV = "SONDER_CODEGEN_BUILD_INPUTS"
LAUNCHER = "/usr/local/bin/sonder-codegen-launch"
MAX_INPUTS = 256
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
_IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}\Z")
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+@-]*\Z")
_PRIVATE_SEGMENTS = {".git", ".env", "strategy", "strategy-private", "secrets", "credentials"}
_PRIVATE_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".db", ".sqlite", ".sqlite3"}


def _relative_file(value: object) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
        raise ValueError("invalid build input name")
    parts = value.split("/")
    if any(
        not _SEGMENT.fullmatch(part) or part.casefold() in _PRIVATE_SEGMENTS
        or part.startswith(".") for part in parts
    ) or any(value.casefold().endswith(suffix) for suffix in _PRIVATE_SUFFIXES):
        raise ValueError("private or noncanonical build input name")
    if "/".join(parts) != value:
        raise ValueError("noncanonical build input name")
    return value


def _manifest(raw: str, *, required: bool) -> tuple[str, ...]:
    if not raw and not required:
        return ()
    try:
        names = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise ValueError("build input manifest is not JSON") from error
    if not isinstance(names, list) or not names or len(names) > MAX_INPUTS:
        raise ValueError("build input manifest must be a bounded nonempty list")
    rendered = tuple(_relative_file(name) for name in names)
    if len(set(rendered)) != len(rendered):
        raise ValueError("duplicate build input")
    return rendered


def _inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def _identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("build directory changed type")
    return info.st_dev, info.st_ino


def _owned_directory(raw: str, *, private: bool) -> Path:
    path = Path(raw)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("build root must be a canonical absolute directory")
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("build root owner/type is unsafe")
    if private and info.st_mode & 0o077:
        raise ValueError("build staging root must be private")
    return path


def _read_input(root_fd: int, relative: str, root_device: int) -> bytes | None:
    """Walk every component through no-follow directory descriptors."""
    parts = relative.split("/")
    directory_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            try:
                next_fd = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                return None
            os.close(directory_fd)
            directory_fd = next_fd
            if os.fstat(directory_fd).st_dev != root_device:
                raise ValueError("build input crosses a filesystem boundary")
        try:
            descriptor = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            return None
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_dev != root_device or before.st_size > MAX_FILE_BYTES):
                raise ValueError("build input is not a bounded ordinary file")
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                data = source.read(MAX_FILE_BYTES + 1)
            after = os.fstat(descriptor)
            if (len(data) > MAX_FILE_BYTES or before.st_ino != after.st_ino
                    or before.st_mtime_ns != after.st_mtime_ns
                    or before.st_ctime_ns != after.st_ctime_ns
                    or before.st_size != after.st_size or len(data) != after.st_size):
                raise ValueError("build input changed during snapshot")
            return data
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_fd)


def _stage(project: Path, root: Path, names: tuple[str, ...], inputs: frozenset[str],
           project_identity: tuple[int, int], root_identity: tuple[int, int]) -> Path:
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    stage = None
    stage_identity = None
    try:
        info = os.fstat(root_fd)
        if (info.st_dev, info.st_ino) != root_identity:
            raise ValueError("build staging root changed before snapshot")
        for _ in range(8):
            name = "codegen-" + secrets.token_hex(16)
            try:
                os.mkdir(name, mode=0o700, dir_fd=root_fd)
            except FileExistsError:
                continue
            break
        else:
            raise RuntimeError("cannot reserve a private build snapshot")
        stage = root / name
        created = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if not stat.S_ISDIR(created.st_mode):
            raise ValueError("build snapshot changed type")
        stage_identity = (created.st_dev, created.st_ino)
        stage_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                           dir_fd=root_fd)
        try:
            staged = os.fstat(stage_fd)
            if (staged.st_dev, staged.st_ino) != stage_identity:
                raise ValueError("build snapshot changed during creation")
        finally:
            os.close(stage_fd)
        if _identity(root) != root_identity or _identity(stage) != stage_identity:
            raise ValueError("build staging directory changed before snapshot")
        project_fd = os.open(project, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            opened = os.fstat(project_fd)
            if (opened.st_dev, opened.st_ino) != project_identity:
                raise ValueError("build project changed before snapshot")
            device = opened.st_dev
            total = 0
            mounts = isolated_runner._host_mount_points()
            for relative in names:
                target = project / relative
                if any(
                    point != project and _inside(point, project) and _inside(target, point)
                    for point in mounts
                ):
                    raise ValueError("build input crosses a nested mount")
                source = _read_input(project_fd, relative, device)
                if source is None:
                    if relative in inputs:
                        raise ValueError("host-granted build input is missing")
                    continue  # The model may not have written a declared source yet.
                total += len(source)
                if total > MAX_TOTAL_BYTES:
                    raise ValueError("build source snapshot exceeds size bound")
                destination = stage / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source)
                destination.chmod(0o644)
        finally:
            os.close(project_fd)
        for parent in [stage, *stage.rglob("*")]:
            if parent.is_dir():
                parent.chmod(0o755)
        if _identity(root) != root_identity or _identity(stage) != stage_identity:
            raise ValueError("build staging directory changed during snapshot")
        return stage
    except BaseException:
        if (stage is not None and stage_identity is not None
                and _identity(root) == root_identity and _identity(stage) == stage_identity):
            shutil.rmtree(stage)
        raise
    finally:
        os.close(root_fd)


class CodegenContainerBuild:
    """Fixed host policy and exact-source input manifest for one project."""

    def __init__(self, project: Path, staging_root: Path, image: str,
                 names: tuple[str, ...], inputs: frozenset[str]):
        self.project = project
        self.project_identity = _identity(project)
        self.staging_root = staging_root
        self.staging_identity = _identity(staging_root)
        self.image = image
        self.names = names
        self.inputs = inputs

    def run(self, program, args_json, cwd, timeout, token, approval, extra_roots):
        del token, approval, extra_roots  # No child credential or host policy bypass.
        try:
            if (Path(cwd).resolve() != self.project
                    or _identity(self.project) != self.project_identity
                    or _owned_directory(str(self.staging_root), private=True) != self.staging_root
                    or _identity(self.staging_root) != self.staging_identity):
                raise ValueError("build root identity changed")
            timeout = int(timeout)
            if timeout < 1:
                raise ValueError("build deadline expired")
            if not isinstance(program, str) or program.startswith("-"):
                raise ValueError("build program is invalid")
            args = json.loads(args_json)
            if not isinstance(args, list):
                raise TypeError("build arguments must be a JSON list")
            argv = isolated_runner._parse_argv([program, *args])
            stage = _stage(self.project, self.staging_root, self.names, self.inputs,
                           self.project_identity, self.staging_identity)
        except (OSError, ValueError, TypeError, OverflowError) as error:
            return f"error: build could not run: {type(error).__name__}", False
        verified_absent = False
        try:
            result = isolated_runner.run_isolated(
                self.image, [LAUNCHER, *argv], str(stage),
                timeout=min(timeout, isolated_runner.MAX_TIMEOUT),
                memory_mb=2048, build_scratch_mb=1024,
                cpus=2, pids=64, output_bytes=128 * 1024,
                verify_exit_cleanup=True,
            )
            verified_absent = result.get("cleanup") == "verified-absent"
            if not verified_absent or result.get("error") or result.get("returncode") is None:
                return "error: build could not run: isolated result or teardown uncertain", False
            status = result["returncode"]
            # Docker reserves 125/126/127 for daemon or command-launch errors.
            # Higher conventional signal statuses (including 137/OOM or kill)
            # also cannot support a complete compiler verdict. A candidate
            # exiting with one of these values is conservatively refused.
            if type(status) is not int or status < 0 or status >= 125:
                return "error: build could not run: container exit status uncertain", False
            output = "\n".join(
                part for part in (result.get("stdout", ""), result.get("stderr", "")) if part
            )
            return output, status == 0
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            return f"error: build could not run: {type(error).__name__}", False
        finally:
            # If the daemon cannot prove the container stopped, leave its
            # disposable, read-only source bind in place and keep the sealed
            # Codegen project guard pending for operator reconciliation.
            if verified_absent:
                shutil.rmtree(stage)


def compose_build(*, project_dir: str | None, declared_sources: tuple[str, ...]):
    """Return a configured adapter or refuse before the Codegen project guard."""
    if sys.platform != "linux" or not project_dir or not declared_sources:
        return None
    image = os.environ.get(IMAGE_ENV, "").strip()
    if not _IMAGE_ID.fullmatch(image):
        return None
    project_raw = os.environ.get(PROJECT_ENV, "")
    stage_raw = os.environ.get(STAGING_ENV, "")
    if not project_raw or not stage_raw:
        return None
    try:
        project = _owned_directory(project_raw, private=False)
        if Path(project_dir).absolute() != project or Path(project_dir).resolve(strict=True) != project:
            return None
        stage_root = _owned_directory(stage_raw, private=True)
        home = Path(default_home()).resolve()
        checkpoint = Path(state_path("strategy/checkpoints.db", "SONDER_STRATEGY_CHECKPOINT_DB")).resolve()
        key = Path(state_path("strategy-private/checkpoint.key")).resolve()
        if any(
            _inside(project, protected) or _inside(protected, project)
            or _inside(stage_root, protected) or _inside(protected, stage_root)
            for protected in (home, checkpoint, key)
        ) or _inside(stage_root, project) or _inside(project, stage_root):
            return None
        allowed = _manifest(os.environ.get(SOURCES_ENV, ""), required=True)
        inputs = _manifest(os.environ.get(INPUTS_ENV, ""), required=False)
        declared = tuple(_relative_file(name) for name in declared_sources)
        if (not declared or len(declared) > MAX_INPUTS or len(set(declared)) != len(declared)
                or not set(declared).issubset(allowed) or set(declared).intersection(inputs)):
            return None
        names = tuple(dict.fromkeys((*declared, *inputs)))
        if len(names) > MAX_INPUTS:
            return None
        if not any(_inside(stage_root, root) for root in isolated_runner.authorized_roots()):
            return None
        runtime = isolated_runner.detect_runtime()
        if runtime is None:
            return None
        inspected = isolated_runner._inspect_image_policy(runtime[1], runtime[2], image)
        if inspected != image:
            return None
        return CodegenContainerBuild(project, stage_root, image, names, frozenset(inputs))
    except (OSError, ValueError, TypeError):
        return None
