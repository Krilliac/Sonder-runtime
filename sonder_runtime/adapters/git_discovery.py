"""Bounded, fail-closed Git visibility discovery for repository inspection.

The filesystem walkers consume only paths reported by ``git ls-files``.  Git
therefore owns nested ignore, negation, and ``.git/info/exclude`` semantics,
while this adapter owns process and repository-boundary safety.
"""
from __future__ import annotations

from sonder_runtime.platform.runtime_threads import Thread as owned_runtime_thread

import os
import shutil
import stat
import subprocess
import threading
from pathlib import Path, PurePosixPath


DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_OUTPUT_BYTES = 8_000_000
MAX_ERROR_BYTES = 16_384


class GitDiscoveryError(RuntimeError):
    """Git visibility could not be determined completely and safely."""


def _is_reparse(metadata) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & flag
    )


def _git_marker(start: Path) -> Path | None:
    """Return the nearest containing .git marker without invoking Git."""
    current = start
    while True:
        marker = current / ".git"
        try:
            marker_stat = marker.lstat()
        except FileNotFoundError:
            marker_stat = None
        except OSError as exc:
            raise GitDiscoveryError("Git metadata boundary could not be inspected") from exc
        if marker_stat is not None:
            if _is_reparse(marker_stat):
                raise GitDiscoveryError("Git metadata marker is a symlink")
            return marker
        if current.parent == current:
            return None
        current = current.parent


def _scrubbed_git_env() -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    })
    return env


def _run_bounded(argv: list[str], *, timeout_seconds: float, output_limit: int) -> bytes:
    """Run an argv-only Git command while continuously draining capped pipes."""
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_scrubbed_git_env(),
        shell=False,
    )
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = threading.Event()

    def drain(name: str, stream, limit: int) -> None:
        try:
            while True:
                chunk = stream.read(65_536)
                if not chunk:
                    return
                remaining = limit - len(buffers[name])
                if remaining > 0:
                    buffers[name].extend(chunk[:remaining])
                if len(chunk) > remaining:
                    overflow.set()
                    try:
                        process.kill()
                    except OSError:
                        pass
                    return
        finally:
            stream.close()

    threads = [
        owned_runtime_thread(target=drain, args=("stdout", process.stdout, output_limit), daemon=True),
        owned_runtime_thread(target=drain, args=("stderr", process.stderr, MAX_ERROR_BYTES), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        returncode = process.wait(timeout=max(0.05, float(timeout_seconds)))
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        for thread in threads:
            thread.join(timeout=1)
        raise GitDiscoveryError("Git ignore discovery timed out") from exc
    for thread in threads:
        thread.join(timeout=1)
    if any(thread.is_alive() for thread in threads):
        process.kill()
        raise GitDiscoveryError("Git ignore discovery did not close its output pipes")
    if overflow.is_set():
        raise GitDiscoveryError("Git ignore discovery output exceeded its bounded limit")
    if returncode:
        raise GitDiscoveryError("Git ignore discovery command failed")
    return bytes(buffers["stdout"])


def _git_argv(git: str, *, safe_directory: Path | None = None) -> list[str]:
    """Build a hermetic Git argv with one validated repository trust scope."""
    args = [
        git, "--no-optional-locks",
        "-c", "core.excludesFile=%s" % os.devnull,
        "-c", "core.fsmonitor=false",
        "-c", "core.untrackedCache=false",
    ]
    if safe_directory is not None:
        args.extend(["-c", "safe.directory=%s" % safe_directory])
    return args


def visible_paths(
    root: Path, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    output_limit: int = MAX_OUTPUT_BYTES,
) -> frozenset[str] | None:
    """Return non-ignored paths relative to *root*, or ``None`` outside Git.

    A detected Git marker changes failure semantics: any Git/process/parse
    failure raises instead of falling back to a raw filesystem scan.
    """
    root = root.resolve()
    marker = _git_marker(root)
    if marker is None:
        return None
    git = shutil.which("git")
    if not git:
        raise GitDiscoveryError("Git metadata exists but the Git executable is unavailable")
    git = str(Path(git).resolve())
    # The marker was inspected without Git; trust only its containing path,
    # never a wildcard or user/global config entry.  The later repository and
    # marker-boundary checks still reject indirect or unrelated metadata.
    prefix = _git_argv(git, safe_directory=marker.parent.resolve())
    raw_metadata = _run_bounded(
        prefix + ["-C", str(root), "rev-parse", "--show-toplevel", "--absolute-git-dir"],
        timeout_seconds=timeout_seconds,
        output_limit=16_384,
    )
    try:
        metadata = raw_metadata.decode("utf-8", errors="strict").splitlines()
    except UnicodeDecodeError as exc:
        raise GitDiscoveryError("Git repository identity was not valid UTF-8") from exc
    if len(metadata) != 2:
        raise GitDiscoveryError("Git repository identity was incomplete")
    repo_root = Path(metadata[0]).resolve()
    git_dir_raw = Path(metadata[1])
    if not git_dir_raw.is_absolute():
        raise GitDiscoveryError("Git metadata directory was not absolute")
    try:
        git_dir_stat = git_dir_raw.lstat()
    except OSError as exc:
        raise GitDiscoveryError("Git metadata directory is unavailable") from exc
    if _is_reparse(git_dir_stat):
        raise GitDiscoveryError("Git metadata directory is indirect")
    git_dir = git_dir_raw.resolve()
    try:
        subroot = root.relative_to(repo_root)
        marker.relative_to(repo_root)
    except ValueError as exc:
        raise GitDiscoveryError("Git resolved outside the requested repository boundary") from exc
    if marker.parent.resolve() != repo_root:
        raise GitDiscoveryError("Git resolved an unrelated containing repository")
    if not git_dir.is_dir():
        raise GitDiscoveryError("Git metadata directory is unavailable")

    argv = prefix + [
        "--git-dir=%s" % git_dir,
        "--work-tree=%s" % repo_root,
        "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--full-name",
    ]
    at_repo_root = subroot == Path(".")
    if not at_repo_root:
        argv.extend(["--", subroot.as_posix()])
    raw = _run_bounded(argv, timeout_seconds=timeout_seconds, output_limit=output_limit)
    visible: set[str] = set()
    root_prefix = "" if at_repo_root else subroot.as_posix().rstrip("/")
    for encoded in raw.split(b"\0"):
        if not encoded:
            continue
        value = encoded.decode("utf-8", errors="surrogateescape")
        candidate = PurePosixPath(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise GitDiscoveryError("Git returned an unsafe repository path")
        if root_prefix:
            prefix_value = root_prefix + "/"
            if value == root_prefix:
                relative = ""
            elif value.startswith(prefix_value):
                relative = value[len(prefix_value):]
            else:
                raise GitDiscoveryError("Git returned a path outside the requested subroot")
        else:
            relative = value
        if not relative:
            continue
        relative_path = PurePosixPath(relative)
        visible.add(os.path.normcase(str(Path(*relative_path.parts))))
        for parent in relative_path.parents:
            if str(parent) != ".":
                visible.add(os.path.normcase(str(Path(*parent.parts))))
    return frozenset(visible)


def require_unchanged(
    root: Path, initial: frozenset[str] | None, *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """Reject a scan if its Git/non-Git visibility boundary changed."""
    current = visible_paths(root, timeout_seconds=timeout_seconds)
    if current != initial:
        raise GitDiscoveryError("Git visibility changed during filesystem scan")
