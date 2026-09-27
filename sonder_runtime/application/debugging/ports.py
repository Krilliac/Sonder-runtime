"""Ports and plan types for crash and profile digests.

A caller (the model, the REPL, HTTP) chooses only an engine, contained paths,
bounded numbers and, at the attended console only, the symbol-server flag.
Everything a process sees -- argv, environment, working directory -- comes
from host-owned templates (``domain.debugging.templates``) through the
planner. Plans carry ``{nonce}``/``{rundir}`` placeholders, never the values:
the launcher binds a fresh nonce and a private run directory at start, so the
evaluator's plan and the executor's plan digest identically.

The Tier-0 report and profile objects are the lane A/B domain types
(``domain.crash.model.CrashReport``, ``domain.profiling.model.ProfileDigest``).
This module refers to them only by annotation so the ports stay importable on
their own; the adapters own the readers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Mapping, Protocol

from ...domain.common.errors import (
    CapacityExceeded,
    DependencyUnavailable,
    Forbidden,
    InvalidInput,
    NotFound,
    SonderError,
)
from ..context import OperationContext
from ..diagnostics.ports import JobOutputReader, TextWindow

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from ...domain.crash.model import CrashBucket, CrashReport
    from ...domain.profiling.model import ProfileDigest

CRASH_JOB_KIND = "tool.crash_digest"
PROFILE_JOB_KIND = "tool.profile_digest"
JOB_ID_PREFIX = "debug-run-"

# Stable error codes (interfaces section of the crash-profile spec).
CAPTURE_REJECTED = "CAPTURE_REJECTED"
CAPTURE_FORMAT_UNKNOWN = "CAPTURE_FORMAT_UNKNOWN"
CAPTURE_TOO_LARGE = "CAPTURE_TOO_LARGE"
CAPTURE_NEEDS_HOST_TOOL = "CAPTURE_NEEDS_HOST_TOOL"
INPUT_CHANGED = "INPUT_CHANGED"
ENGINE_UNAVAILABLE = "ENGINE_UNAVAILABLE"
ENGINE_UNSUPPORTED_ON_PLATFORM = "ENGINE_UNSUPPORTED_ON_PLATFORM"
ENGINE_REFUSED_MANAGED_DUMP = "ENGINE_REFUSED_MANAGED_DUMP"
SYMBOL_PATH_REJECTED = "SYMBOL_PATH_REJECTED"
SYMBOL_STORE_REJECTED = "SYMBOL_STORE_REJECTED"
SYMBOL_SERVER_CONSENT_REQUIRED = "SYMBOL_SERVER_CONSENT_REQUIRED"
SYMBOL_SERVER_NEEDS_CONSOLE = "SYMBOL_SERVER_NEEDS_CONSOLE"
EXECUTABLE_REQUIRED = "EXECUTABLE_REQUIRED"
PDB_MISMATCH = "PDB_MISMATCH"
OUTPUT_LIMIT = "OUTPUT_LIMIT"
DEBUG_RUN_BUSY = "DEBUG_RUN_BUSY"
JOB_NOT_FOUND = "JOB_NOT_FOUND"
PARSE_FAILED = "PARSE_FAILED"
DEBUG_TOOLS_UNAVAILABLE = "DEBUG_TOOLS_UNAVAILABLE"

ERROR_CODES = frozenset({
    CAPTURE_REJECTED, CAPTURE_FORMAT_UNKNOWN, CAPTURE_TOO_LARGE, CAPTURE_NEEDS_HOST_TOOL,
    INPUT_CHANGED, ENGINE_UNAVAILABLE, ENGINE_UNSUPPORTED_ON_PLATFORM,
    ENGINE_REFUSED_MANAGED_DUMP, SYMBOL_PATH_REJECTED, SYMBOL_STORE_REJECTED,
    SYMBOL_SERVER_CONSENT_REQUIRED, SYMBOL_SERVER_NEEDS_CONSOLE, EXECUTABLE_REQUIRED,
    PDB_MISMATCH, OUTPUT_LIMIT, DEBUG_RUN_BUSY, JOB_NOT_FOUND, PARSE_FAILED,
    DEBUG_TOOLS_UNAVAILABLE,
})

_CODE_CLASSES: Mapping[str, type[SonderError]] = {
    SYMBOL_SERVER_CONSENT_REQUIRED: Forbidden,
    SYMBOL_SERVER_NEEDS_CONSOLE: Forbidden,
    ENGINE_REFUSED_MANAGED_DUMP: Forbidden,
    DEBUG_RUN_BUSY: CapacityExceeded,
    JOB_NOT_FOUND: NotFound,
    ENGINE_UNAVAILABLE: DependencyUnavailable,
    DEBUG_TOOLS_UNAVAILABLE: DependencyUnavailable,
}


def debug_error(code: str, message: str) -> SonderError:
    """A typed error carrying one of the stable ``code`` values above."""
    cls = _CODE_CLASSES.get(code, InvalidInput)
    error = cls(str(message)[:400])
    error.code = code
    return error


CRASH_ENGINES = ("auto", "cdb", "gdb", "lldb", "eu_stack", "minidump_stackwalk",
                 "llvm_symbolizer", "pure")
PROFILE_ENGINES = ("auto", "perf", "heaptrack_print", "tracy_csvexport", "xperf", "wpaexporter")

# Capture kinds ``sniff`` can name. Crash kinds are ``CrashSourceKind``
# values, profile kinds ``ProfileSourceKind`` values; the rest are refusals.
CRASH_KINDS = frozenset({
    "windows_minidump", "breakpad_minidump", "crashpad_minidump", "elf_core",
    "sanitizer_report", "valgrind_xml", "apple_ips",
})
MINIDUMP_KINDS = frozenset({"windows_minidump", "breakpad_minidump", "crashpad_minidump"})
PURE_PROFILE_KINDS = frozenset({
    "perf_text", "callgrind", "chrome_trace", "tracy_csv", "wpa_csv", "pix_csv",
    "superluminal_csv", "heaptrack_text",
})
HOST_PROFILE_KINDS = frozenset({"perf_data", "tracy_capture", "etw_etl", "heaptrack_capture"})
PROFILE_KINDS = PURE_PROFILE_KINDS | HOST_PROFILE_KINDS | frozenset({"profile_csv"})
REFUSED_KINDS = frozenset({"perfetto_protobuf", "unknown"})

RunStatus = Literal["complete", "running", "failed", "cancelled", "timed_out", "refused", "partial"]


# -- requests ----------------------------------------------------------------


@dataclass(frozen=True)
class CrashDigestRequest:
    path: str
    executable: str = ""
    symbol_dirs: tuple[str, ...] = ()
    engine: str = "auto"
    symbol_server: bool = False
    timeout_seconds: int | None = None


@dataclass(frozen=True)
class CrashTriageRequest:
    path: str
    max_threads: int = 16
    max_files: int = 64


@dataclass(frozen=True)
class ProfileDigestRequest:
    path: str
    executable: str = ""
    engine: str = "auto"
    symbol_dirs: tuple[str, ...] = ()
    top_n: int = 25
    frame_budget_ms: float | None = None
    thread: str = ""
    frame_zone: str = ""
    timeout_seconds: int | None = None


# -- identity and plan -------------------------------------------------------


@dataclass(frozen=True)
class CaptureIdentity:
    """A contained, opened capture: where it is and what it was when hashed."""

    path: str            # canonical host path (never shown to a model)
    label: str           # display label (project-relative or the file name)
    size: int
    dev: int
    ino: int
    mtime_ns: int
    sha256: str          # 64 hex, or ``partial:<sha>+<size>`` past the budget
    kind: str = "unknown"

    def same_file(self, other: "CaptureIdentity") -> bool:
        return (self.dev, self.ino, self.size, self.mtime_ns) == (
            other.dev, other.ino, other.size, other.mtime_ns)


@dataclass(frozen=True)
class DebugStep:
    """One host process of a plan; no nonce, rundir or cwd (the launcher binds them)."""

    engine: str
    template_argv: tuple[str, ...]
    display_argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    timeout_seconds: int
    max_output_bytes: int
    memory_limit_bytes: int
    parser: str
    isolation: Literal["netns", "none"] = "none"
    reads_output_via: str = "argv"   # "argv" (job output) | "file:<rel>" | "dir:<rel>"


@dataclass(frozen=True)
class DebugPlan:
    kind: str                         # "crash" | "profile"
    source_kind: str
    input_label: str
    input_sha256: str
    input_bytes: int
    input_identity: CaptureIdentity
    staging: Literal["copy", "hardlink", "path"]
    steps: tuple[DebugStep, ...]
    network: bool
    stores_display: tuple[str, ...]
    verified_modules: tuple[str, ...]
    checked_executables: tuple[str, ...]
    notes: tuple[str, ...]
    command_digest: str
    # Values for the template placeholders other than nonce/rundir/input;
    # they may themselves name ``{rundir}`` (resolved by the launcher first).
    bindings: tuple[tuple[str, str], ...] = ()
    # (host source path, path relative to the run dir) copied at start.
    staged_files: tuple[tuple[str, str], ...] = ()
    # Directories (relative to the run dir) created before the first step.
    mkdirs: tuple[str, ...] = ()
    # (module name, symbols state) the planner established (e.g. mismatch).
    module_symbols: tuple[tuple[str, str], ...] = ()
    egress_isolation: Literal["netns", "none", "n/a"] = "n/a"
    engines: tuple[str, ...] = ("pure",)

    @property
    def isolation(self) -> str:
        values = {step.isolation for step in self.steps}
        if not values:
            return "n/a"
        return "netns" if values == {"netns"} else "none"

    def resolved_command(self) -> dict:
        """The approval-binding view: placeholders only, no nonce or run dir."""
        return {
            "kind": self.kind,
            "engines": list(self.engines),
            "display_argvs": [list(step.display_argv) for step in self.steps],
            "input_label": self.input_label,
            "input_sha256": self.input_sha256,
            "network": bool(self.network),
            "stores_display": list(self.stores_display),
            "isolation": self.isolation,
            "command_digest": self.command_digest,
        }


@dataclass(frozen=True)
class DebugRunState:
    """What the launcher knows about one run (a chain of step jobs)."""

    run_id: str
    principal_id: str
    kind: str
    status: str                       # running | complete | failed | cancelled | timed_out | partial
    step_job_ids: tuple[str, ...]
    step_status: tuple[str, ...] = ()
    step_exit_codes: tuple[int | None, ...] = ()
    nonce: str = ""
    input_changed: bool = False
    output_limit: bool = False
    staging: str = ""
    egress_isolation: str = "n/a"
    command_digest: str = ""
    input_sha256: str = ""
    started_at: float = 0.0
    finished_at: float | None = None
    notes: tuple[str, ...] = ()

    @property
    def done(self) -> bool:
        return self.status != "running"


@dataclass(frozen=True)
class DebugRunOutcome:
    run_id: str
    status: RunStatus
    crash: "CrashReport | None" = None
    profile: "ProfileDigest | None" = None
    error_code: str = ""
    notes: tuple[str, ...] = ()
    command_digest: str = ""
    engines: tuple[str, ...] = ()
    buckets: "tuple[CrashBucket, ...] | None" = None
    display_argvs: tuple[tuple[str, ...], ...] = ()
    egress_isolation: str = "n/a"
    network: bool = False
    staging: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)


# -- protocols ---------------------------------------------------------------


class ClosableByteReader(Protocol):
    @property
    def size(self) -> int: ...

    def read(self, offset: int, length: int) -> bytes: ...

    def close(self) -> None: ...


class CaptureSource(Protocol):
    def open_reader(self, path: str, *, extra_roots: str = "",
                    max_bytes: int | None = None) -> tuple[ClosableByteReader, CaptureIdentity]:
        """Guarded open + hashed identity (``kind`` filled by sniffing)."""

    def sniff(self, reader: ClosableByteReader, name: str) -> str: ...

    def is_dir(self, path: str, *, extra_roots: str = "") -> bool: ...

    def list_dir(self, path: str, *, extra_roots: str = "",
                 max_files: int = 64) -> tuple[CaptureIdentity, ...]: ...

    def contained_file(self, path: str, *, extra_roots: str = "") -> str: ...


class PureTriage(Protocol):
    def crash(self, identity: CaptureIdentity, reader: ClosableByteReader) -> "CrashReport": ...

    def profile(self, identity: CaptureIdentity, reader: ClosableByteReader,
                request: ProfileDigestRequest) -> "ProfileDigest | None": ...

    def bucket(self, reports) -> "tuple[CrashBucket, ...]": ...

    def merge_crash(self, base: "CrashReport", parser: str, text: str, nonce: str,
                    engine: str) -> "CrashReport": ...

    def finish_crash(self, report: "CrashReport", *, engines: tuple[str, ...],
                     module_symbols: tuple[tuple[str, str], ...], egress_isolation: str,
                     notes: tuple[str, ...], truncated: bool) -> "CrashReport": ...

    def profile_from_steps(self, identity_label: str, input_sha256: str, source_kind: str,
                           outputs: tuple[tuple[str, str], ...], request: ProfileDigestRequest,
                           *, engines: tuple[str, ...], egress_isolation: str,
                           notes: tuple[str, ...], truncated: bool) -> "ProfileDigest": ...

    def crash_to_wire(self, report: "CrashReport") -> dict: ...

    def crash_from_wire(self, data: Mapping) -> "CrashReport": ...

    def profile_to_wire(self, digest: "ProfileDigest") -> dict: ...

    def profile_from_wire(self, data: Mapping) -> "ProfileDigest": ...


class DebugPlanner(Protocol):
    def plan_crash(self, request: CrashDigestRequest, context: OperationContext, *,
                   network_allowed: bool, identity: CaptureIdentity,
                   tier0: "CrashReport | None") -> DebugPlan: ...

    def plan_profile(self, request: ProfileDigestRequest, context: OperationContext, *,
                     identity: CaptureIdentity) -> DebugPlan: ...


class DebugLauncher(Protocol):
    def start(self, plan: DebugPlan, context: OperationContext, run_id: str) -> DebugRunState: ...

    def wait(self, run_id: str, timeout: float) -> tuple[DebugRunState, bool]:
        """(state, still_running); never claims done before the chain ended."""

    def cancel(self, run_id: str, reason: str) -> bool: ...

    def metadata(self, run_id: str) -> Mapping[str, str] | None:
        """The run's durable metadata (principal_id, kind, ...); None when unknown."""

    def running_for(self, principal_id: str) -> int: ...

    def step_job_ids(self, run_id: str) -> tuple[str, ...]: ...

    def step_output(self, run_id: str, index: int) -> str | None:
        """File-based step output (``reads_output_via`` file/dir) kept for assembly."""

    def store_json(self, run_id: str, name: str, payload: Mapping) -> None: ...

    def load_json(self, run_id: str, name: str) -> Mapping | None: ...


class SymbolConsent(Protocol):
    def allowed(self, context: OperationContext) -> bool: ...

    def stores(self) -> tuple[str, ...]: ...

    def set_session(self, context: OperationContext, allowed: bool) -> None: ...

    def mode_permits_network(self) -> bool: ...


class SourceMap(Protocol):
    def map_report(self, report: "CrashReport") -> "CrashReport": ...


__all__ = [
    "CAPTURE_FORMAT_UNKNOWN", "CAPTURE_NEEDS_HOST_TOOL", "CAPTURE_REJECTED", "CAPTURE_TOO_LARGE",
    "CRASH_ENGINES", "CRASH_JOB_KIND", "CRASH_KINDS", "CaptureIdentity", "CaptureSource",
    "ClosableByteReader", "CrashDigestRequest", "CrashTriageRequest", "DEBUG_RUN_BUSY",
    "DEBUG_TOOLS_UNAVAILABLE", "DebugLauncher", "DebugPlan", "DebugPlanner", "DebugRunOutcome",
    "DebugRunState", "DebugStep", "ENGINE_REFUSED_MANAGED_DUMP", "ENGINE_UNAVAILABLE",
    "ENGINE_UNSUPPORTED_ON_PLATFORM", "ERROR_CODES", "EXECUTABLE_REQUIRED", "HOST_PROFILE_KINDS",
    "INPUT_CHANGED", "JOB_ID_PREFIX", "JOB_NOT_FOUND", "JobOutputReader", "MINIDUMP_KINDS",
    "OUTPUT_LIMIT", "PARSE_FAILED", "PDB_MISMATCH", "PROFILE_ENGINES", "PROFILE_JOB_KIND",
    "PROFILE_KINDS", "PURE_PROFILE_KINDS", "ProfileDigestRequest", "PureTriage", "REFUSED_KINDS",
    "SYMBOL_PATH_REJECTED", "SYMBOL_SERVER_CONSENT_REQUIRED", "SYMBOL_SERVER_NEEDS_CONSOLE",
    "SYMBOL_STORE_REJECTED", "SourceMap", "SymbolConsent", "TextWindow", "debug_error",
]
