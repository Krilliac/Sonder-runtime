"""Typed crash results: ``CrashReport`` (``sonder.crash_report/1``) and parts.

Every value here may come from a crashed (possibly hostile) process: module
names, thread names, annotations, function names and file paths are all
attacker-influenced. Each ``str`` field therefore passes through
``clean_text(value, 240)`` at construction (ANSI and control characters
stripped, whitespace collapsed, clipped), enums are stored as their string
values, and ``CrashReport.untrusted_strings`` is always true so renderers
label the content.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import Enum

from ..binaries.reader import BinaryFormatError
from ..diagnostics.model import clean_text


SCHEMA = "sonder.crash_report/1"
WIRE_STRING_CHARS = 240
MAX_CRASHING_FRAMES = 64
MAX_OTHER_THREADS = 15
MAX_OTHER_FRAMES = 8
MAX_MODULES = 128
MAX_ANNOTATIONS = 32
MAX_NOTES = 32
MAX_HINTS = 12


class CrashSourceKind(str, Enum):
    WINDOWS_MINIDUMP = "windows_minidump"
    BREAKPAD_MINIDUMP = "breakpad_minidump"
    CRASHPAD_MINIDUMP = "crashpad_minidump"
    ELF_CORE = "elf_core"
    SANITIZER_REPORT = "sanitizer_report"
    VALGRIND_XML = "valgrind_xml"
    APPLE_IPS = "apple_ips"


class CrashEngine(str, Enum):
    PURE = "pure"
    CDB = "cdb"
    GDB = "gdb"
    LLDB = "lldb"
    EU_STACK = "eu_stack"
    MINIDUMP_STACKWALK = "minidump_stackwalk"
    LLVM_SYMBOLIZER = "llvm_symbolizer"


class FrameTrust(str, Enum):
    CONTEXT = "context"
    CFI = "cfi"
    FRAME_POINTER = "frame_pointer"
    SCAN = "scan"
    DEBUGGER = "debugger"
    SANITIZER = "sanitizer"
    SYMBOLIZER = "symbolizer"


SYMBOL_STATES = ("loaded", "not_found", "mismatch", "not_attempted")
CONFIDENCES = ("high", "medium", "low")
SIGNATURE_BASES = ("functions", "module_offsets", "exception_only")
SYMBOLICATION_LEVELS = ("full", "partial", "none")
EGRESS_ISOLATION = ("netns", "none", "n/a")


class CaptureFormatError(BinaryFormatError):
    """A crash capture is malformed, unsupported, or exceeds a bound.

    ``code``: NOT_MINIDUMP, NOT_CORE, NOT_SANITIZER, NOT_VALGRIND, NOT_IPS,
    TRUNCATED, OUT_OF_BOUNDS, LIMIT_EXCEEDED, UNSUPPORTED_ARCH or
    TIME_EXCEEDED.
    """


def _clean_value(value, limit: int):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, str):
        return clean_text(value, limit)
    return value


def _sanitize(obj, *, tuples_of_str: tuple[str, ...] = ()) -> None:
    for item in fields(obj):
        value = getattr(obj, item.name)
        if item.name in tuples_of_str:
            cleaned = tuple(clean_text(_clean_value(v, WIRE_STRING_CHARS), WIRE_STRING_CHARS)
                            for v in (value or ()))
            object.__setattr__(obj, item.name, cleaned)
            continue
        cleaned = _clean_value(value, WIRE_STRING_CHARS)
        if cleaned is not value:
            object.__setattr__(obj, item.name, cleaned)


@dataclass(frozen=True, slots=True)
class StackFrame:
    index: int
    address: int | None = None
    module: str = ""
    module_offset: int | None = None
    function: str = ""
    file: str = ""           # path as recorded in debug info
    line: int | None = None
    column: int | None = None
    inline: bool = False
    trust: str = FrameTrust.SCAN.value
    in_project: bool = False
    local_file: str | None = None  # project-relative path after source mapping

    def __post_init__(self) -> None:
        _sanitize(self)


@dataclass(frozen=True, slots=True)
class ThreadSummary:
    thread_id: int
    name: str = ""
    crashed: bool = False
    frames: tuple[StackFrame, ...] = ()
    frames_truncated: bool = False

    def __post_init__(self) -> None:
        _sanitize(self)
        object.__setattr__(self, "frames", tuple(self.frames))


@dataclass(frozen=True, slots=True)
class ModuleInfo:
    name: str
    path: str = ""
    base: int = 0
    size: int = 0
    version: str = ""
    timestamp: int | None = None
    debug_id: str = ""
    debug_file: str = ""
    symbols: str = "not_attempted"
    in_project: bool = False
    managed_runtime: bool = False

    def __post_init__(self) -> None:
        _sanitize(self)
        if self.symbols not in SYMBOL_STATES:
            object.__setattr__(self, "symbols", "not_attempted")

    def contains(self, address: int | None) -> bool:
        return address is not None and self.size > 0 and self.base <= address < self.base + self.size


@dataclass(frozen=True, slots=True)
class CrashException:
    code: str = ""
    name: str = ""
    signal: str = ""
    address: int | None = None
    access: str = ""
    access_address: int | None = None
    thread_id: int | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        _sanitize(self)


@dataclass(frozen=True, slots=True)
class CauseHint:
    kind: str
    confidence: str
    evidence: str = ""

    def __post_init__(self) -> None:
        _sanitize(self)
        if self.confidence not in CONFIDENCES:
            object.__setattr__(self, "confidence", "low")


@dataclass(frozen=True, slots=True)
class Annotation:
    key: str
    value: str

    def __post_init__(self) -> None:
        _sanitize(self)


@dataclass(frozen=True, slots=True)
class CrashReport:
    source_kind: str
    engines: tuple[str, ...] = (CrashEngine.PURE.value,)
    source_label: str = ""
    input_sha256: str = ""
    input_bytes: int = 0
    os: str = ""
    cpu: str = ""
    process_name: str = ""
    pid: int | None = None
    captured_at: int | None = None       # epoch seconds, when the capture says
    exception: CrashException | None = None
    crashing_thread_id: int | None = None
    threads: tuple[ThreadSummary, ...] = ()
    threads_total: int = 0
    modules: tuple[ModuleInfo, ...] = ()
    modules_total: int = 0
    annotations: tuple[Annotation, ...] = ()
    hints: tuple[CauseHint, ...] = ()
    signature: str = ""
    signature_basis: str = "exception_only"
    symbolication: str = "none"
    egress_isolation: str = "n/a"
    notes: tuple[str, ...] = ()
    truncated: bool = False
    untrusted_strings: bool = True
    schema: str = field(default=SCHEMA)

    def __post_init__(self) -> None:
        _sanitize(self, tuples_of_str=("engines", "notes"))
        object.__setattr__(self, "schema", SCHEMA)
        object.__setattr__(self, "untrusted_strings", True)
        object.__setattr__(self, "notes", self.notes[:MAX_NOTES])
        object.__setattr__(self, "annotations", tuple(self.annotations)[:MAX_ANNOTATIONS])
        object.__setattr__(self, "hints", tuple(self.hints)[:MAX_HINTS])
        if self.signature_basis not in SIGNATURE_BASES:
            object.__setattr__(self, "signature_basis", "exception_only")
        if self.symbolication not in SYMBOLICATION_LEVELS:
            object.__setattr__(self, "symbolication", "none")
        if self.egress_isolation not in EGRESS_ISOLATION:
            object.__setattr__(self, "egress_isolation", "n/a")

    def crashing_thread(self) -> ThreadSummary | None:
        for thread in self.threads:
            if thread.crashed:
                return thread
        return self.threads[0] if self.threads else None

    def module_named(self, name: str) -> ModuleInfo | None:
        wanted = str(name or "").lower()
        for module in self.modules:
            if module.name.lower() == wanted:
                return module
        return None


@dataclass(frozen=True, slots=True)
class CrashBucket:
    signature: str
    basis: str
    count: int
    exception_name: str
    top_frame: str
    sample_labels: tuple[str, ...] = ()   # at most 5

    def __post_init__(self) -> None:
        _sanitize(self, tuples_of_str=("sample_labels",))
        object.__setattr__(self, "sample_labels", self.sample_labels[:5])


def frame_label(frame: StackFrame) -> str:
    """``module!function`` or ``module+0xoffset`` or ``0xaddress``."""
    if frame.function:
        text = "%s!%s" % (frame.module, frame.function) if frame.module else frame.function
        if frame.file and frame.line:
            text += " [%s:%d]" % (frame.local_file or frame.file, frame.line)
        return text
    if frame.module and frame.module_offset is not None:
        return "%s+0x%x" % (frame.module, frame.module_offset)
    if frame.address is not None:
        return "0x%x" % frame.address
    return "?"


def module_basename(path: str) -> str:
    return str(path or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


__all__ = [
    "Annotation", "CaptureFormatError", "CrashBucket", "CauseHint", "CrashEngine", "CrashException",
    "CrashReport", "CrashSourceKind", "FrameTrust", "MAX_ANNOTATIONS", "MAX_CRASHING_FRAMES",
    "MAX_MODULES", "MAX_OTHER_FRAMES", "MAX_OTHER_THREADS", "ModuleInfo", "SCHEMA",
    "StackFrame", "ThreadSummary", "WIRE_STRING_CHARS", "frame_label", "module_basename",
]
