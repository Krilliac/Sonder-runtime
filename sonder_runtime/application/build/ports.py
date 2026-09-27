"""Ports, requests and plans for the C++ build model and build jobs.

The planner turns a model's small request (project, build dir, action,
target, config, platform, preset, file) into a host-owned plan: argv comes
from the closed template set in ``domain.build.templates`` and every value in
it names a validated member of the parsed build model. The launcher owns the
durable process job and the private log; the collector reads that log back.
Nothing here accepts argv, environment entries or executable paths from a
caller.

Domain build types (``BuildModel``, ``BuildJobReport``, ``TargetSafety``) are
referenced for annotations only, so this module imports without the domain
package's parsers loaded.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Mapping, Protocol

from ...domain.common.errors import (
    CapacityExceeded,
    Conflict,
    DependencyUnavailable,
    Forbidden,
    InvalidInput,
    NotFound,
    SonderError,
)
from ..context import OperationContext
from ..ports.jobs import JobRecord

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from ...domain.build.model import BuildModel
    from ...domain.build.report import BuildJobReport

BUILD_JOB_KIND = "tool.build_job"
BUILD_JOB_PREFIX = "build-job-"
BUILD_FIX_JOB_KIND = "tool.build_fix"
BUILD_FIX_PREFIX = "build-fix-"
BUILD_JOB_ID_RE = re.compile(r"^build-job-[0-9a-f]{16,32}$")
BUILD_FIX_ID_RE = re.compile(r"^build-fix-[0-9a-f]{16,32}$")

# Actions a build job runs (``domain.build.model.BuildAction`` values).
ACTION_CONFIGURE = "configure"
ACTION_BUILD = "build"
ACTION_COMPILE_ONE = "compile_one"
ACTION_INCLUDE_TRACE = "include_trace"
BUILD_ACTIONS = (ACTION_CONFIGURE, ACTION_BUILD, ACTION_COMPILE_ONE, ACTION_INCLUDE_TRACE)

# Error codes shared by every build surface.
BUILD_MODEL_UNAVAILABLE = "BUILD_MODEL_UNAVAILABLE"
BUILD_TREE_MISSING = "BUILD_TREE_MISSING"
BUILD_TREE_REJECTED = "BUILD_TREE_REJECTED"
UNKNOWN_TARGET = "UNKNOWN_TARGET"
UNKNOWN_CONFIG = "UNKNOWN_CONFIG"
UNKNOWN_PLATFORM = "UNKNOWN_PLATFORM"
UNKNOWN_PRESET = "UNKNOWN_PRESET"
UNKNOWN_FILE = "UNKNOWN_FILE"
UTILITY_TARGET_REFUSED = "UTILITY_TARGET_REFUSED"
ACTION_UNSUPPORTED = "ACTION_UNSUPPORTED"
RUNNER_UNAVAILABLE = "RUNNER_UNAVAILABLE"
BUILD_DIR_BUSY = "BUILD_DIR_BUSY"
BUILD_BUSY = "BUILD_BUSY"
PROJECT_OUTSIDE_ROOTS = "PROJECT_OUTSIDE_ROOTS"
ENV_CAPTURE_FAILED = "ENV_CAPTURE_FAILED"
NETWORK_ISOLATION_UNAVAILABLE = "NETWORK_ISOLATION_UNAVAILABLE"
JOB_NOT_FOUND = "JOB_NOT_FOUND"
BUILD_TOOLS_UNAVAILABLE = "BUILD_TOOLS_UNAVAILABLE"

_ERROR_CLASSES: dict[str, type[SonderError]] = {
    BUILD_MODEL_UNAVAILABLE: DependencyUnavailable,
    BUILD_TREE_MISSING: InvalidInput,
    BUILD_TREE_REJECTED: Forbidden,
    UNKNOWN_TARGET: InvalidInput,
    UNKNOWN_CONFIG: InvalidInput,
    UNKNOWN_PLATFORM: InvalidInput,
    UNKNOWN_PRESET: InvalidInput,
    UNKNOWN_FILE: InvalidInput,
    UTILITY_TARGET_REFUSED: Forbidden,
    ACTION_UNSUPPORTED: InvalidInput,
    RUNNER_UNAVAILABLE: DependencyUnavailable,
    BUILD_DIR_BUSY: Conflict,
    BUILD_BUSY: CapacityExceeded,
    PROJECT_OUTSIDE_ROOTS: Forbidden,
    ENV_CAPTURE_FAILED: DependencyUnavailable,
    NETWORK_ISOLATION_UNAVAILABLE: DependencyUnavailable,
    JOB_NOT_FOUND: NotFound,
    BUILD_TOOLS_UNAVAILABLE: DependencyUnavailable,
}


def build_error(code: str, message: str) -> SonderError:
    """A taxonomy error carrying one of the stable build error ``code`` values."""
    error = _ERROR_CLASSES.get(code, InvalidInput)(message)
    error.code = code
    return error


def action_value(action: object) -> str:
    """The plain action string for a ``BuildAction`` member or a string."""
    value = str(getattr(action, "value", action) or "").strip().lower()
    return value


# ---------------------------------------------------------------------------
# Requests


@dataclass(frozen=True, slots=True)
class BuildModelRequest:
    project: str = "."
    build_dir: str = ""
    preset: str = ""
    refresh: bool = False


@dataclass(frozen=True, slots=True)
class BuildJobRequest:
    project: str = "."
    build_dir: str = ""
    action: str = ACTION_BUILD
    target: str = ""
    config: str = ""
    platform: str = ""
    preset: str = ""
    build_preset: str = ""
    file: str = ""
    generator: str = ""
    profile: str = ""
    jobs: int | None = None
    timeout_seconds: int | None = None
    allow_network: bool = False

    def __post_init__(self) -> None:
        action = action_value(self.action)
        if action not in BUILD_ACTIONS:
            raise build_error(ACTION_UNSUPPORTED, "action must be one of %s" % ", ".join(BUILD_ACTIONS))
        object.__setattr__(self, "action", action)
        for name in ("project", "build_dir", "target", "config", "platform", "preset",
                     "build_preset", "file", "generator", "profile"):
            value = getattr(self, name)
            if not isinstance(value, str) or "\x00" in value or len(value) > 1024:
                raise InvalidInput("%s must be a bounded string" % name)
        for name in ("jobs", "timeout_seconds"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise InvalidInput("%s must be an integer" % name)
        if not isinstance(self.allow_network, bool):
            raise InvalidInput("allow_network must be a boolean")

    def model_request(self) -> BuildModelRequest:
        return BuildModelRequest(project=self.project, build_dir=self.build_dir,
                                 preset=self.preset)


# ---------------------------------------------------------------------------
# Tree location, raw tree, environment


@dataclass(frozen=True, slots=True)
class BuildTreeLocation:
    """Resolved roots of one build: absolute paths stay internal."""

    project_root: str
    build_dir: str
    project_label: str
    build_label: str
    # "cmake", "msbuild", "ninja", "make" or "" when nothing was detected.
    detected_system: str = ""
    build_dir_exists: bool = False
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RawBuildTree:
    """Bounded bytes read from one project and build tree; nothing parsed.

    Every mapping is ``(label, bytes)`` pairs with labels relative to the
    project root (``reply/<name>`` for File API reply objects).
    """

    project_root: str
    build_dir: str
    reply_index: tuple[tuple[str, bytes], ...] = ()
    reply_objects: tuple[tuple[str, bytes], ...] = ()
    compile_db: bytes | None = None
    solution: tuple[str, bytes] | None = None
    vcxproj: tuple[tuple[str, bytes], ...] = ()
    props_imports: tuple[tuple[str, bytes], ...] = ()
    presets: tuple[tuple[str, bytes], ...] = ()
    preset_includes: tuple[tuple[str, bytes], ...] = ()
    cache_values: tuple[tuple[str, str], ...] = ()
    ninja_present: bool = False
    makefile_present: bool = False
    ninja_files: tuple[str, ...] = ()
    # Unity blobs a codemodel lists: (path relative to build_dir, bytes).
    unity_blobs: tuple[tuple[str, bytes], ...] = ()
    cmake_lists_present: bool = False
    truncated: bool = False
    notes: tuple[str, ...] = ()
    fingerprint: str = ""

    def cache_value(self, key: str) -> str:
        for name, value in self.cache_values:
            if name == key:
                return value
        return ""


@dataclass(frozen=True, slots=True)
class BuildEnvironment:
    """The replacement environment a job launches with (never on the wire)."""

    pairs: tuple[tuple[str, str], ...]
    keys: tuple[str, ...]
    source: str  # "scrubbed" | "vcvars"
    cache_hit: bool = False
    notes: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Plans


@dataclass(frozen=True)
class BuildJobPlan:
    action: str
    system: str
    project_root: str
    build_dir: str
    cwd: str
    argv: tuple[str, ...]
    display_argv: tuple[str, ...]
    cwd_label: str
    command_digest: str
    environment: tuple[tuple[str, str], ...]
    env_keys: tuple[str, ...]
    timeout_seconds: int
    max_descendants: int
    memory_limit_bytes: int | None
    log_dir: str
    log_file: str
    binlog: str
    world: str
    network: str
    isolation_truth: str
    model_digest: str
    template_id: str
    checked_executables: tuple[str, ...]
    notes: tuple[str, ...] = ()
    project_label: str = ""
    target: str = ""
    config: str = ""
    platform: str = ""
    file_label: str = ""
    run_token: str = ""
    # Extra files the launcher writes before the job starts: the File API
    # query for a configure. ``(absolute path, fixed bytes)``.
    pre_writes: tuple[tuple[str, bytes], ...] = ()
    # ``-flp`` log of an MSBuild run, scanned in addition to ``log_file``.
    extra_logs: tuple[str, ...] = ()
    trace_family: str = ""
    # Headers an include trace forces with -include or /FI (the real PCH
    # header): -H and /showIncludes do not list them, the report does.
    trace_forced: tuple[str, ...] = ()
    lease_id: str = ""

    def resolved_command(self) -> dict:
        """The approval-binding view of the plan: labels only, no host paths."""
        return {
            "action": self.action,
            "system": self.system,
            "template_id": self.template_id,
            "display_argv": list(self.display_argv),
            "project": self.project_label,
            "target": self.target,
            "config": self.config,
            "platform": self.platform,
            "command_digest": self.command_digest,
            "world": self.world,
            "network": self.network,
        }


@dataclass(frozen=True, slots=True)
class BuildDirLease:
    lease_id: str
    build_dir: str
    owner_job_id: str
    principal_id: str
    parent_lease_id: str = ""

    @property
    def is_child(self) -> bool:
        return bool(self.parent_lease_id)


@dataclass(frozen=True)
class BuildJobStatusView:
    job_id: str
    status: str
    action: str
    system: str
    elapsed_seconds: float
    command_digest: str
    display_command: tuple[str, ...]
    parent_job_id: str = ""
    cleanup_proven: bool | None = None
    notes: tuple[str, ...] = field(default=())

    def to_wire(self) -> dict:
        wire = {
            "object": "build_job_status",
            "job_id": self.job_id,
            "status": self.status,
            "action": self.action,
            "system": self.system,
            "elapsed_seconds": round(float(self.elapsed_seconds), 1),
            "command_digest": self.command_digest,
            "display_command": list(self.display_command),
            "next": "call build_job_result with this job_id to wait for the report",
        }
        if self.parent_job_id:
            wire["parent_job_id"] = self.parent_job_id
        if self.cleanup_proven is not None:
            wire["cleanup_proven"] = bool(self.cleanup_proven)
        if self.notes:
            wire["notes"] = list(self.notes)
        return wire


# ---------------------------------------------------------------------------
# Ports


class BuildTreeReader(Protocol):
    def read(self, project_root: str, build_dir: str) -> RawBuildTree:
        """Bounded, no-follow reads of the project and build tree."""

    def fingerprint(self, project_root: str, build_dir: str) -> str:
        """A cheap digest of the tree's reply index, cache and compile db stats."""

    def read_presets(self, project_root: str) -> RawBuildTree:
        """Only the presets (and their in-root includes) of a project."""


class BuildEnvironmentProvider(Protocol):
    def environment(self, *, system: str, family: str, toolchain_hint: str = "",
                    arch: str = "x64") -> BuildEnvironment: ...


class BuildPlanner(Protocol):
    def locate(self, request: BuildModelRequest, context: OperationContext) -> BuildTreeLocation: ...

    def plan_model(self, request: BuildModelRequest, context: OperationContext, *,
                   location: BuildTreeLocation | None = None) -> "BuildModel": ...

    def plan_run(self, request: BuildJobRequest, model: "BuildModel | None",
                 context: OperationContext, *, lease: str | None = None) -> BuildJobPlan: ...


class BuildLauncher(Protocol):
    def start(self, plan: BuildJobPlan, context: OperationContext, job_id: str, *,
              parent_job_id: str = "",
              on_exit: Callable[[str], None] | None = None) -> None: ...

    def poll(self, job_id: str) -> JobRecord | None: ...

    def wait(self, job_id: str, timeout: float) -> tuple[JobRecord, int | None, bool]:
        """(record, exit_code, timed_out); ``timed_out`` never claims terminal."""

    def cancel(self, job_id: str, reason: str) -> bool:
        """Cancel the job's process tree; True once cleanup is proven."""

    def metadata(self, job_id: str) -> Mapping[str, str] | None:
        """The job's metadata plus ``kind``; None when the job is unknown."""

    def running_for(self, principal_id: str) -> int: ...


class BuildOutputCollector(Protocol):
    def collect(self, job_id: str, plan_meta: Mapping[str, str], model: "BuildModel | None", *,
                record: JobRecord | None = None,
                exit_code: int | None = None) -> "BuildJobReport": ...


class BuildModelCache(Protocol):
    def get(self, key: tuple) -> tuple[Any, float] | None:
        """(entry, stored_at) or None."""

    def put(self, key: tuple, model: Any, *, stored_at: float) -> None: ...

    def entries_for(self, principal_id: str) -> tuple[tuple[tuple, Any, float], ...]: ...

    def invalidate(self, key: tuple) -> None: ...


class BuildDirLeases(Protocol):
    def acquire(self, build_dir: str, owner_job_id: str, principal_id: str) -> BuildDirLease:
        """A top-level lease; raises ``BUILD_DIR_BUSY`` when the dir is held."""

    def child(self, lease: BuildDirLease, owner_job_id: str) -> BuildDirLease:
        """A child lease under a held parent; one child at a time."""

    def release(self, lease: BuildDirLease) -> None: ...

    def holder(self, build_dir: str) -> BuildDirLease | None: ...


# (text, source_label) -> an ``OutputDigest.to_wire()`` mapping.
OutputSummarizer = Callable[[str, str], Mapping[str, object]]


__all__ = [
    "ACTION_BUILD", "ACTION_COMPILE_ONE", "ACTION_CONFIGURE", "ACTION_INCLUDE_TRACE",
    "ACTION_UNSUPPORTED", "BUILD_ACTIONS", "BUILD_BUSY", "BUILD_DIR_BUSY", "BUILD_FIX_ID_RE",
    "BUILD_FIX_JOB_KIND", "BUILD_FIX_PREFIX", "BUILD_JOB_ID_RE", "BUILD_JOB_KIND",
    "BUILD_JOB_PREFIX", "BUILD_MODEL_UNAVAILABLE", "BUILD_TOOLS_UNAVAILABLE",
    "BUILD_TREE_MISSING", "BUILD_TREE_REJECTED", "BuildDirLease", "BuildDirLeases",
    "BuildEnvironment", "BuildEnvironmentProvider", "BuildJobPlan", "BuildJobRequest",
    "BuildJobStatusView", "BuildLauncher", "BuildModelCache", "BuildModelRequest",
    "BuildOutputCollector", "BuildPlanner", "BuildTreeLocation", "BuildTreeReader",
    "ENV_CAPTURE_FAILED", "JOB_NOT_FOUND", "NETWORK_ISOLATION_UNAVAILABLE", "OutputSummarizer",
    "PROJECT_OUTSIDE_ROOTS", "RUNNER_UNAVAILABLE", "RawBuildTree", "UNKNOWN_CONFIG",
    "UNKNOWN_FILE", "UNKNOWN_PLATFORM", "UNKNOWN_PRESET", "UNKNOWN_TARGET",
    "UTILITY_TARGET_REFUSED", "action_value", "build_error",
]
