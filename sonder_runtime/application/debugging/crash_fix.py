"""Crash-to-fix hand-off: a CrashReport becomes typed build diagnostics.

Application layer, pure: nothing here launches a process, touches the network
or reads a file on its own. Source excerpts and repro candidates arrive
through injected callables (``SourceLookup`` / ``ReproLookup``) so the REPL
can bind them to the guarded file reader and the caller's own test reports.

The report is read by attribute (``CrashReport``/``StackFrame`` field names
from ``sonder_runtime.domain.crash.model``), so this module only needs
``dataclasses.replace`` to annotate mapped frames.

Every string that came from the crashed process -- function names, module
names, exception detail -- is untrusted (SEC-006). The diagnostic messages
say so in their text, and the rendered brief labels the excerpt the same way.

The rendered diagnostic lines are GNU-style ``path:line:col: fatal error:
<message> [CRASH:<name>]`` so the existing consumers read them unchanged:
``codegen_loop.count_errors`` counts each one, and the -4
``parse_diagnostics`` GNU grammar yields ``severity=fatal`` with the same file
and line.
"""
from __future__ import annotations

import dataclasses
import hashlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

from ...domain.diagnostics.model import (
    Diagnostic,
    DiagnosticTool,
    Severity,
    clean_text,
    make_diagnostic,
    strip_ansi,
)
from ...domain.common.errors import InvalidInput
from ...domain.strategy.models import FailureClass, FailureObservation, ProgressMetric
from ...domain.testing.selectors import parse_selector


MAX_DIAGNOSTICS = 24
MAX_EXCERPT_LINES = 40
MAX_WIRE_CHARS = 240
MAX_BRIEF_CHARS = 12_000
_MAX_SCANNED_LINES = 200_000
UNTRUSTED_LABEL = "from the crashed process, untrusted"
CRASH_OBSERVATION_CODE = "CRASH_REPRODUCED"
CRASH_METRIC = "crash_reproduced"
REPRO_RUNNERS = ("ctest", "pytest", "cargo", "go", "dotnet", "gradle", "maven")

# ctest prints "***Exception: SegFault" / "Subprocess aborted"; other runners
# print "Segmentation fault" or an unhandled "Exception". Matched on the
# bounded message excerpt of one failure, never on raw output.
_CRASH_FAILURE_RE = re.compile(
    r"seg(?:mentation)?\s*fault|\bexception\b|subprocess aborted", re.I,
)
_EXE_SUFFIX_RE = re.compile(r"\.(?:exe|out|bin)$", re.I)
# Excerpt lines keep their indentation but lose every terminal control: C0
# and C1 controls (C1 CSI is an escape introducer on some terminals) and the
# bidi overrides/isolates that make displayed source differ from real source.
_EXCERPT_CONTROL_RE = re.compile(
    r"[\x00-\x08\x0a-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069\ud800-\udfff]")
# What ``clean_text`` leaves that is still unsafe in one wire line: C1
# controls, bidi overrides/isolates and lone surrogates (which cannot even be
# encoded as UTF-8, so they would break the digest and every JSON writer).
_WIRE_EXTRA_RE = re.compile(r"[\x80-\x9f\u202a-\u202e\u2066-\u2069\ud800-\udfff]")
# User-identifying path prefixes in model-visible text (the brief goes to the
# model): POSIX homes, root's home, Windows profiles and UNC hosts.
_HOME_PREFIX_RES = (
    (re.compile(r"(?<![\w.~-])/(?:home|Users)/[^/\s:;,()\[\]'\"]+"), "~"),
    (re.compile(r"(?<![\w.~-])/root(?=[/\s:;,()\[\]'\"]|$)"), "~"),
    (re.compile(r"(?<![\w])[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/][^\\/\s:;,()\[\]'\"]+",
                re.I), "%USERPROFILE%"),
    (re.compile(r"(?<![\w\\])\\\\[^\\/\s]+\\[^\\/\s]+"), r"\\\\<unc>"),
)


# --- untrusted text --------------------------------------------------------------


def redact_user_paths(text: str) -> str:
    """Replace home/profile/UNC prefixes so no user or host name reaches the model."""
    value = str(text or "")
    for pattern, replacement in _HOME_PREFIX_RES:
        value = pattern.sub(replacement, value)
    return value


def project_relative(path: Any) -> str:
    """``path`` as a clean project-relative path, or "" when it is anything else.

    Absolute POSIX paths, drive-letter, UNC and alternate-stream forms, ``~``
    and any ``..`` (or other dots-only) component are refused: a diagnostic the fix loop may open and edit must
    name a file inside the checkout, whatever a lookup or a report claims.
    """
    text = _text(path, 260).replace("\\", "/")
    # ":" also covers drive letters and NTFS alternate data streams.
    if not text or text.startswith(("/", "~")) or ":" in text:
        return ""
    parts = [part.strip() for part in text.split("/")]
    parts = [part for part in parts if part not in ("", ".")]
    if not parts or any(not part.strip(".") for part in parts):
        return ""
    return "/".join(parts)


def _excerpt_line(value: Any) -> str:
    text = strip_ansi(str(value)[:2_000]).rstrip("\r\n").replace("\t", "    ")
    return _EXCERPT_CONTROL_RE.sub("", text)[:400]


def _display_path(value: Any, limit: int) -> str:
    return redact_user_paths(_text(value, 4_096))[:limit]


def _module_name(value: Any) -> str:
    """A module's file name (``app``, ``ntdll.dll``), never its directory."""
    text = redact_user_paths(_text(value, 4_096)).replace("\\", "/").rstrip("/")
    return text.rsplit("/", 1)[-1][:80]


# --- types ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """A project-relative location with a guarded excerpt of at most 40 lines."""

    path: str
    line: int | None
    excerpt: tuple[str, ...] = ()
    symbol: str = ""
    first_line: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", project_relative(self.path))
        object.__setattr__(self, "symbol", _text(self.symbol, MAX_WIRE_CHARS))
        lines = tuple(_excerpt_line(line) for line in tuple(self.excerpt)[:MAX_EXCERPT_LINES])
        object.__setattr__(self, "excerpt", lines)
        if self.line is not None and (
                not isinstance(self.line, int) or isinstance(self.line, bool) or self.line < 1):
            object.__setattr__(self, "line", None)
        if not isinstance(self.first_line, int) or self.first_line < 1:
            object.__setattr__(self, "first_line", 1)


@dataclass(frozen=True, slots=True)
class ReproSpec:
    """A test the operator (or model) may run through the gated test_run tool."""

    kind: Literal["test_run"]
    runner: str
    selector: str
    display: str


@dataclass(frozen=True, slots=True)
class CrashFixHandoff:
    signature: str
    signature_basis: str
    summary: str
    top_frame: str
    source: SourceSpan | None
    hints: tuple[str, ...]
    diagnostics: tuple[Diagnostic, ...]
    lines: tuple[str, ...]
    repro: ReproSpec | None
    failure_class: FailureClass


# ``(recorded_or_local_path, line, symbol) -> SourceSpan | None``. The path is
# the frame's ``local_file`` when the service already mapped it, otherwise
# the path recorded in debug info (a CI build path, for example).
SourceLookup = Callable[[str, "int | None", str], "SourceSpan | None"]
ReproLookup = Callable[[Any], "ReproSpec | None"]


# --- report accessors (attribute reads only) --------------------------------------


def _text(value: Any, limit: int = MAX_WIRE_CHARS) -> str:
    raw = getattr(value, "value", value) if value is not None else ""
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw[: limit * 4]).decode("utf-8", errors="replace")
    text = _WIRE_EXTRA_RE.sub("", str(raw)[: max(0, int(limit)) * 8])
    return clean_text(text, limit)


def _crashing_thread(report: Any) -> Any | None:
    threads = tuple(getattr(report, "threads", ()) or ())
    wanted = getattr(report, "crashing_thread_id", None)
    for thread in threads:
        if getattr(thread, "crashed", False):
            return thread
    for thread in threads:
        if wanted is not None and getattr(thread, "thread_id", None) == wanted:
            return thread
    return threads[0] if threads else None


def _frames(report: Any) -> tuple[Any, ...]:
    thread = _crashing_thread(report)
    return tuple(getattr(thread, "frames", ()) or ()) if thread is not None else ()


def _exception_name(report: Any) -> str:
    exception = getattr(report, "exception", None)
    name = redact_user_paths(_text(getattr(exception, "name", ""), 64)) if exception is not None else ""
    return re.sub(r"[^A-Za-z0-9_.:-]", "_", name) or "UNKNOWN_CRASH"


def _hex(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int):
        return "0x%x" % value if value >= 0 else ""
    return _text(value, 40)


def _hint_kinds(report: Any) -> tuple[str, ...]:
    return tuple(
        _text(getattr(hint, "kind", ""), 40)
        for hint in tuple(getattr(report, "hints", ()) or ())[:8]
        if _text(getattr(hint, "kind", ""), 40)
    )


def _frame_label(frame: Any) -> str:
    module = _module_name(getattr(frame, "module", ""))
    function = _text(getattr(frame, "function", ""), 160)
    if function:
        label = "%s!%s" % (module, function) if module else function
    else:
        offset = getattr(frame, "module_offset", None)
        address = getattr(frame, "address", None)
        label = "%s+%s" % (module or "?", _hex(offset) or "?") if module else (_hex(address) or "?")
    location = project_relative(getattr(frame, "local_file", None) or "") or _display_path(
        getattr(frame, "file", ""), 160)
    line = getattr(frame, "line", None)
    if location:
        label += " (%s%s)" % (location, ":%d" % line if isinstance(line, int) else "")
    return redact_user_paths(label)[:MAX_WIRE_CHARS]


# --- diagnostics ---------------------------------------------------------------------


def _message(report: Any, frame: Any) -> str:
    exception = getattr(report, "exception", None)
    parts = [_exception_name(report)]
    if exception is not None:
        access = _text(getattr(exception, "access", ""), 16)
        if access:
            parts.append(access)
        address = _hex(getattr(exception, "access_address", None)) or _hex(
            getattr(exception, "address", None))
        if address:
            parts.append(address)
    function = _text(getattr(frame, "function", ""), 160) or "?"
    parts.append("in %s" % function)
    hints = _hint_kinds(report)
    if hints:
        parts.append("[%s]" % hints[0])
    parts.append("(%s)" % UNTRUSTED_LABEL)
    return redact_user_paths(" ".join(parts))


def crash_diagnostics(report: Any, max_items: int = MAX_DIAGNOSTICS) -> tuple[Diagnostic, ...]:
    """One FATAL diagnostic per in-project crashing-thread frame with a local file.

    Frames without ``local_file`` (unmapped, system or third-party code) and
    frames without a line produce nothing: a diagnostic nobody can open is
    noise to a fix loop.
    """
    limit = max(1, min(int(max_items), MAX_DIAGNOSTICS))
    code = "CRASH:" + _exception_name(report)
    out: list[Diagnostic] = []
    for frame in _frames(report):
        local = project_relative(getattr(frame, "local_file", None) or "")
        line = getattr(frame, "line", None)
        if not local or not getattr(frame, "in_project", False):
            continue
        if not isinstance(line, int) or isinstance(line, bool) or line < 1:
            continue
        column = getattr(frame, "column", None)
        out.append(make_diagnostic(
            tool=DiagnosticTool.GENERIC.value,
            severity=Severity.FATAL.value,
            file=local,
            line=line,
            col=column if isinstance(column, int) and column > 0 else None,
            code=code,
            message=_message(report, frame),
        ))
        if len(out) >= limit:
            break
    return tuple(out)


def render_crash_diagnostic(diagnostic: Diagnostic) -> str:
    """``path:line[:col]: fatal error: <message> [<code>]``."""
    location = diagnostic.location() or "<unknown>"
    text = "%s: fatal error: %s" % (location, diagnostic.message)
    if diagnostic.code:
        text += " [%s]" % diagnostic.code
    return text


def crash_diagnostic_lines(
    report: Any, *, diagnostics: tuple[Diagnostic, ...] | None = None,
) -> tuple[str, ...]:
    values = diagnostics if diagnostics is not None else crash_diagnostics(report)
    return tuple(render_crash_diagnostic(item) for item in values)


# --- source mapping -------------------------------------------------------------------


def _replace(value: Any, **changes: Any) -> Any:
    try:
        return dataclasses.replace(value, **changes)
    except (TypeError, ValueError):
        return value


def map_report_sources(
    report: Any, source_lookup: SourceLookup | None, *, max_frames: int = MAX_DIAGNOSTICS,
) -> tuple[Any, SourceSpan | None]:
    """Give crashing-thread frames a ``local_file`` through ``source_lookup``.

    Returns ``(report, first_span)`` where ``first_span`` is the excerpt for
    the topmost mapped frame. A frame the lookup cannot map is left as it is,
    so it produces no diagnostic. Only frames already recorded with a file are
    offered to the lookup; the lookup never sees addresses.
    """
    thread = _crashing_thread(report)
    if thread is None or source_lookup is None:
        return report, None
    frames = list(getattr(thread, "frames", ()) or ())
    first: SourceSpan | None = None
    changed = False
    for index, frame in enumerate(frames[:max(1, int(max_frames))]):
        path = getattr(frame, "local_file", None) or getattr(frame, "file", "")
        if not path:
            continue
        line = getattr(frame, "line", None)
        try:
            span = source_lookup(str(path), line if isinstance(line, int) else None,
                                 _text(getattr(frame, "function", ""), 160))
        except (OSError, ValueError, PermissionError, LookupError):
            span = None
        if span is None or not span.path:
            continue
        if first is None:
            first = span
        if getattr(frame, "local_file", None) != span.path or not getattr(frame, "in_project", False):
            frames[index] = _replace(frame, local_file=span.path, in_project=True)
            changed = True
    if not changed:
        return report, first
    new_thread = _replace(thread, frames=tuple(frames))
    threads = tuple(
        new_thread if item is thread else item
        for item in tuple(getattr(report, "threads", ()) or ())
    )
    return _replace(report, threads=threads), first


def project_source_lookup(
    resolve: Callable[[str], str | None],
    read_lines: Callable[[str], Iterable[str] | None],
    *,
    context_before: int = 12,
    max_lines: int = MAX_EXCERPT_LINES,
) -> SourceLookup:
    """Compose a lookup from a path mapper and a guarded line reader.

    ``resolve`` maps a recorded debug-info path (possibly a CI build path) to
    a project-relative local path, or None; ``read_lines`` returns the file's
    lines through the guarded file reader, or None when it is refused.
    """
    window = max(1, min(int(max_lines), MAX_EXCERPT_LINES))
    before = max(0, min(int(context_before), window - 1))
    # A crashing stack names the same few files over and over; each is read
    # (through the guarded reader) at most once per hand-off.
    cache: dict[str, tuple[str, ...] | None] = {}

    def lines_of(local: str) -> tuple[str, ...] | None:
        if local not in cache:
            if len(cache) >= 8:
                return None
            lines = read_lines(local)
            cache[local] = None if lines is None else tuple(
                str(text) for _, text in zip(range(_MAX_SCANNED_LINES), lines))
        return cache[local]

    def lookup(path: str, line: int | None, symbol: str) -> SourceSpan | None:
        local = resolve(path)
        if not local:
            return None
        local = project_relative(local)
        if not local:
            return None
        excerpt: tuple[str, ...] = ()
        first_line = 1
        lines = lines_of(local)
        if lines is not None:
            start = max(1, (line or 1) - before)
            collected = []
            for number, text in enumerate(lines, 1):
                if number < start:
                    continue
                if number >= start + window:
                    break
                collected.append(str(text).rstrip("\r\n"))
            excerpt, first_line = tuple(collected), start
        return SourceSpan(path=local, line=line, excerpt=excerpt, symbol=symbol,
                          first_line=first_line)

    return lookup


# --- repro lookup ---------------------------------------------------------------------


def explicit_repro(selector: str, *, runner: str = "ctest") -> ReproSpec:
    """Validate an operator's ``--repro NAME`` with the -3 selector grammar.

    Raises ``SelectorRejected`` (an ``InvalidInput``) for anything the runner's
    grammar refuses; the value never becomes a flag or shell text.
    """
    name = str(runner or "ctest").strip().lower()
    parsed = parse_selector(name, selector)
    return ReproSpec("test_run", name, parsed.value, "/test %s %s" % (name, parsed.value))


def _binary_name(value: str) -> str:
    base = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    return _EXE_SUFFIX_RE.sub("", base).lower()


def repro_from_test_reports(reports: Iterable[Any], process_name: str) -> ReproSpec | None:
    """The newest owned test report whose crashed test ran ``process_name``.

    ``reports`` are the caller's own ``TestReport`` values, newest first. A
    failure qualifies when its bounded message says the test binary crashed
    (SegFault, an unhandled Exception, "Subprocess aborted") and the test
    binary it names -- its file, or its id for runners that name tests after
    the executable -- equals the crashed process. Nothing is run to find out.
    """
    wanted = _binary_name(process_name)
    if not wanted:
        return None
    for report in reports:
        runner = _text(getattr(report, "runner", ""), 32).lower()
        if runner not in REPRO_RUNNERS:
            continue
        for failure in tuple(getattr(report, "failures", ()) or ()):
            message = _text(getattr(failure, "message_excerpt", ""), 400)
            if not _CRASH_FAILURE_RE.search(message):
                continue
            test_id = _text(getattr(failure, "id", ""), 200)
            names = {_binary_name(getattr(failure, "file", "") or ""), _binary_name(test_id)}
            if wanted not in names:
                continue
            try:
                return explicit_repro(test_id, runner=runner)
            except (InvalidInput, ValueError):
                continue
    return None


def repro_lookup_for(
    *, explicit: str = "", runner: str = "ctest",
    reports: Callable[[], Iterable[Any]] | None = None,
) -> ReproLookup:
    """The v1 rule: an explicit selector wins, else the newest matching report."""

    def lookup(report: Any) -> ReproSpec | None:
        if explicit:
            return explicit_repro(explicit, runner=runner)
        if reports is None:
            return None
        try:
            candidates = tuple(reports() or ())
        except (OSError, ValueError, LookupError):
            return None
        return repro_from_test_reports(candidates, _text(getattr(report, "process_name", ""), 200))

    return lookup


# --- hand-off ---------------------------------------------------------------------------


def _summary(report: Any) -> str:
    exception = getattr(report, "exception", None)
    parts = [_exception_name(report)]
    if exception is not None:
        signal = _text(getattr(exception, "signal", ""), 32)
        if signal and signal not in parts[0]:
            parts.append("(%s)" % signal)
        access = _text(getattr(exception, "access", ""), 16)
        address = _hex(getattr(exception, "access_address", None))
        if access or address:
            parts.append(" ".join(p for p in ("%s at" % access if access else "at", address) if p))
    process = _module_name(getattr(report, "process_name", ""))
    if process:
        parts.append("in %s" % process)
    thread = getattr(report, "crashing_thread_id", None)
    if thread is not None:
        parts.append("thread %s" % _text(thread, 24))
    return redact_user_paths(" ".join(parts))[:MAX_WIRE_CHARS]


def build_crash_fix_handoff(
    report: Any,
    *,
    source_lookup: SourceLookup | None = None,
    repro_lookup: ReproLookup | None = None,
) -> CrashFixHandoff:
    """Diagnostics, the top source span and a repro candidate for one report."""
    mapped, span = map_report_sources(report, source_lookup)
    diagnostics = crash_diagnostics(mapped)
    frames = _frames(mapped)
    top = next(
        (frame for frame in frames if getattr(frame, "in_project", False)),
        frames[0] if frames else None,
    )
    repro = repro_lookup(mapped) if repro_lookup is not None else None
    hints = tuple(
        redact_user_paths("%s (%s): %s" % (
            _text(getattr(hint, "kind", ""), 40), _text(getattr(hint, "confidence", ""), 16),
            _text(getattr(hint, "evidence", ""), 160),
        ))
        for hint in tuple(getattr(mapped, "hints", ()) or ())[:8]
    )
    return CrashFixHandoff(
        signature=_text(getattr(mapped, "signature", ""), 64),
        signature_basis=_text(getattr(mapped, "signature_basis", ""), 32),
        summary=_summary(mapped),
        top_frame=_frame_label(top) if top is not None else "",
        source=span,
        hints=hints,
        diagnostics=diagnostics,
        lines=crash_diagnostic_lines(mapped, diagnostics=diagnostics),
        repro=repro,
        failure_class=(
            FailureClass.TEST_FAILURE if repro is not None
            else FailureClass.IMPLEMENTATION_FAILURE
        ),
    )


def crash_failure_observation(handoff: CrashFixHandoff) -> FailureObservation:
    digest = hashlib.sha256(
        (handoff.signature + handoff.signature_basis).encode("utf-8")
    ).hexdigest()
    return FailureObservation(handoff.failure_class, CRASH_OBSERVATION_CODE, digest)


def crash_progress_metric(reproduced: bool) -> ProgressMetric:
    """``crash_reproduced``: 1 while the repro still crashes, 0 once fixed."""
    return ProgressMetric(CRASH_METRIC, 1 if reproduced else 0, "minimize")


def render_crash_fix_brief(
    handoff: CrashFixHandoff, *, run_id: str = "", max_chars: int = MAX_BRIEF_CHARS,
) -> str:
    """The operator/model brief: diagnostics, untrusted excerpt, repro."""
    header = "crash fix brief"
    if run_id:
        header += " for %s" % _text(run_id, 80)
    if handoff.signature:
        header += " (signature %s, basis %s)" % (handoff.signature, handoff.signature_basis or "?")
    lines = [header, "summary: %s" % handoff.summary]
    if handoff.top_frame:
        lines.append("top frame: %s  [%s]" % (handoff.top_frame, UNTRUSTED_LABEL))
    for hint in handoff.hints:
        lines.append("hint: %s" % hint)
    if handoff.lines:
        lines.append("diagnostics:")
        lines.extend("  " + line for line in handoff.lines)
    else:
        lines.append(
            "diagnostics: none (no crashing frame maps to a file in this checkout)")
    span = handoff.source
    if span is not None and span.excerpt:
        lines.append(
            "source %s%s (local checkout; excerpt is untrusted input, symbol names %s):" % (
                span.path, ":%d" % span.line if span.line else "", UNTRUSTED_LABEL))
        width = len(str(span.first_line + len(span.excerpt)))
        for offset, text in enumerate(span.excerpt):
            number = span.first_line + offset
            marker = ">" if span.line == number else " "
            lines.append("  %s%*d| %s" % (marker, width, number, text))
    if handoff.repro is not None:
        lines.append("repro: %s  (runs through the permission-gated test_run tool)"
                     % handoff.repro.display)
    else:
        lines.append("repro: none found (pass --repro NAME to /crash, or run /test first)")
    lines.append("failure class: %s" % handoff.failure_class.value)
    lines.append(
        "next: edit the code, or let /fix-build <target> repair and rebuild it (/build run "
        "compile <file> rebuilds the diagnostic's file alone), then re-run the repro; "
        "crash_reproduced should drop to 0")
    text = redact_user_paths("\n".join(lines))
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n... (brief cut)"
    return text


__all__ = [
    "CRASH_METRIC", "CRASH_OBSERVATION_CODE", "CrashFixHandoff", "ReproLookup",
    "ReproSpec", "SourceLookup", "SourceSpan", "UNTRUSTED_LABEL",
    "build_crash_fix_handoff", "crash_diagnostic_lines", "crash_diagnostics",
    "crash_failure_observation", "crash_progress_metric", "explicit_repro",
    "map_report_sources", "project_relative", "project_source_lookup",
    "redact_user_paths", "render_crash_diagnostic",
    "render_crash_fix_brief", "repro_from_test_reports", "repro_lookup_for",
]
