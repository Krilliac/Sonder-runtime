"""The one diagnostics parser: compiler, linker, linter and test output.

Pure and bounded. Nothing here opens a file, runs a process or evaluates
anything; every grammar is compiled once at import and applied to lines that
were first capped at ``max_line_chars``. Regexes avoid nested unbounded
quantifiers, and the few suffix captures (``[-Wflag]``, eslint rule names,
msbuild project suffixes) are split with string operations rather than a
backtracking tail, so a pathological 4096-character line stays linear.

``error_lines`` is the codegen loop's historical "distinct error lines"
counter, moved here so the repository has one diagnostics module.
``codegen_loop.count_errors`` delegates to it byte-for-byte.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from .model import (
    MAX_GROUP_FILES,
    SEVERITY_RANK,
    Diagnostic,
    DiagnosticGroup,
    DiagnosticSet,
    DiagnosticTool,
    Severity,
    make_diagnostic,
    message_template,
    strip_ansi,
)


DEFAULT_ERROR_LINE_PATTERN = r"(?i)\b(?:error|fatal)\b"

HARD_MAX_DIAGNOSTICS = 200
HARD_MAX_GROUPS = 50
HARD_MAX_LINES = 50_000
HARD_MAX_LINE_CHARS = 4096
_TRACEBACK_MAX_LINES = 200
_RUST_LOOKAHEAD = 4


def error_lines(text: str, pattern: str = DEFAULT_ERROR_LINE_PATTERN) -> list[str]:
    """Distinct error lines in build output, order preserved.

    Exactly the semantics ``codegen_loop.count_errors`` has always had: every
    line is stripped, blank and non-matching lines are skipped, and each
    distinct stripped line is kept once in first-seen order.
    """
    compiled = re.compile(pattern)
    seen, out = set(), []
    for line in text.split("\n"):
        line = line.strip()
        if not line or not compiled.search(line):
            continue
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out


# --- grammars --------------------------------------------------------------

_SEV = r"(?P<sev>fatal error|error|warning|note)"

_GNU_RE = re.compile(
    r"^(?P<file>(?:[A-Za-z]:)?[^:\n]+?):(?P<line>\d+):(?:(?P<col>\d+):)?\s*"
    + _SEV + r":\s*(?P<msg>.*)$"
)
_GNU_FLAG_SUFFIX_RE = re.compile(r"^-W[\w=+-]+$")
_LD_REFERENCE_RE = re.compile(
    r"^(?P<file>[^:\n]+):\(\.\w+[^)]*\): (?P<msg>undefined reference to .*)$"
)
_LD_PREFIX_RE = re.compile(
    r"^(?:\S*/)?(?:ld|collect2)(?:\.\w+)?: (?:error: )?(?P<msg>.*)$"
)
_MSVC_CL_RE = re.compile(
    r"^(?P<file>(?:[A-Za-z]:)?[^(\n]+)\((?P<line>\d+)(?:,(?P<col>\d+))?\)\s*:\s*"
    + _SEV + r"\s+(?P<code>[A-Z]{1,6}\d{4})\s*:\s*(?P<msg>.*)$"
)
_MSVC_LINK_RE = re.compile(
    r"^(?P<file>[^\n]*?)\s*:\s*(?P<sev>fatal error|error|warning)\s+"
    r"(?P<code>LNK\d{4})\s*:\s*(?P<msg>.*)$"
)
# A project-level msbuild/dotnet diagnostic has no (line,col) group.
_MSBUILD_RE = re.compile(
    r"^(?P<file>[^\n]*?)\s*:\s*(?P<sev>error|warning)\s+"
    r"(?P<code>(?:MSB|NETSDK|CS|NU)\d{4})\s*:\s*(?P<msg>.*)$"
)
_MSBUILD_PROJECT_SUFFIX_RE = re.compile(r"\s+\[[^\]\n]{1,1024}\.(?:vcx|cs|fs|vb)proj\]$")
_DOTNET_CODE_RE = re.compile(r"^(?:CS|MSB|NETSDK|NU)\d{4}$")
_RUST_HEADER_RE = re.compile(
    r"^(?P<sev>error|warning)(?:\[(?P<code>E\d{4})\])?: (?P<msg>.+)$"
)
_RUST_LOCATION_RE = re.compile(r"^\s*--> (?P<file>.+?):(?P<line>\d+):(?P<col>\d+)$")
_RUST_DROPPED_RE = re.compile(
    r"^(?:warning: .* generated \d+ warnings?(?: \(.*\))?|error: aborting due to\b.*)$"
)
_TSC_RE = re.compile(
    r"^(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)\): (?P<sev>error|warning) "
    r"(?P<code>TS\d+): (?P<msg>.*)$"
)
_TSC_PRETTY_RE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+) - (?P<sev>error|warning) "
    r"(?P<code>TS\d+): (?P<msg>.*)$"
)
_ESLINT_ROW_RE = re.compile(
    r"^\s+(?P<line>\d+):(?P<col>\d+)\s+(?P<sev>error|warning)\s+(?P<rest>\S.*)$"
)
_ESLINT_UNIX_RE = re.compile(
    r"^(?P<file>[^\n]+?):(?P<line>\d+):(?P<col>\d+): (?P<msg>.*) "
    r"\[(?P<sev>Error|Warning)/(?P<code>[@\w/-]+)\]$"
)
_ESLINT_RULE_RE = re.compile(r"^[@\w/-]+$")
_ESLINT_HEADER_RE = re.compile(
    r"^(?:[A-Za-z]:[\\/]|/|\.{1,2}[\\/])?[^\s:*?\"<>|][^:*?\"<>|\n]*"
    r"\.(?:[cm]?[jt]sx?|vue|svelte|astro|json|md|html?|ya?ml)$"
)
_GO_RE = re.compile(
    r"^(?:\./)?(?P<file>[^:\s]+\.go):(?P<line>\d+):(?P<col>\d+): (?P<msg>.*)$"
)
_TRACEBACK_HEADER = "Traceback (most recent call last):"
_PY_FRAME_RE = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)')
_PY_EXCEPTION_RE = re.compile(r"^(?P<exc>[A-Za-z_][\w.]*)(?::\s?(?P<msg>.*))?$")
_PY_SYNTAX_RE = re.compile(
    r"^(?P<exc>(?:SyntaxError|IndentationError|TabError))(?::\s?(?P<msg>.*))?$"
)
_PYTEST_RE = re.compile(
    r"^(?P<kind>FAILED|ERROR) (?P<nodeid>\S+?)(?: - (?P<msg>.*))?$"
)

_SEVERITY_WORDS = {
    "fatal error": Severity.FATAL.value,
    "error": Severity.ERROR.value,
    "warning": Severity.WARNING.value,
    "note": Severity.NOTE.value,
    "Error": Severity.ERROR.value,
    "Warning": Severity.WARNING.value,
}

ALL_TOOLS = frozenset(tool.value for tool in DiagnosticTool)


def _allowed(tools: Iterable[str] | None) -> frozenset[str]:
    if tools is None:
        return ALL_TOOLS
    return frozenset(str(tool).strip().lower() for tool in tools) & ALL_TOOLS


def _severity(word: str) -> str:
    return _SEVERITY_WORDS.get(word, Severity.ERROR.value)


def _split_gnu_flag(message: str) -> tuple[str, str]:
    """Peel a trailing ``[-Wflag]`` off a GNU message with string operations."""
    text = message.rstrip()
    if not text.endswith("]"):
        return text, ""
    start = text.rfind(" [")
    if start < 0:
        return text, ""
    candidate = text[start + 2:-1]
    if _GNU_FLAG_SUFFIX_RE.match(candidate):
        return text[:start].rstrip(), candidate
    return text, ""


def _strip_msbuild_suffix(line: str) -> str:
    if not line.endswith("proj]"):
        return line
    start = line.rfind(" [")
    if start < 0:
        return line
    tail = line[start:]
    if _MSBUILD_PROJECT_SUFFIX_RE.match(tail):
        return line[:start].rstrip()
    return line


def _parse_gnu(line: str, line_no: int) -> Diagnostic | None:
    if ":" not in line:
        return None
    match = _GNU_RE.match(line)
    if match:
        message, flag = _split_gnu_flag(match.group("msg"))
        return make_diagnostic(
            tool=DiagnosticTool.GNU.value, severity=_severity(match.group("sev")),
            file=match.group("file"), line=match.group("line"), col=match.group("col"),
            code=flag, message=message, raw_line_no=line_no,
        )
    match = _LD_REFERENCE_RE.match(line)
    if match:
        return make_diagnostic(
            tool=DiagnosticTool.GNU.value, severity=Severity.ERROR.value,
            file=match.group("file"), message=match.group("msg"), raw_line_no=line_no,
        )
    match = _LD_PREFIX_RE.match(line)
    if match:
        message = match.group("msg")
        inner = _LD_REFERENCE_RE.match(message)
        if inner:
            return make_diagnostic(
                tool=DiagnosticTool.GNU.value, severity=Severity.ERROR.value,
                file=inner.group("file"), message=inner.group("msg"),
                raw_line_no=line_no,
            )
        severity = Severity.ERROR.value
        if message.rstrip().endswith("':") and " in function " in message:
            # "in function `main':" introduces the next reference; it is context.
            severity = Severity.NOTE.value
        elif message.startswith("warning: "):
            severity = Severity.WARNING.value
            message = message[len("warning: "):]
        return make_diagnostic(
            tool=DiagnosticTool.GNU.value, severity=severity,
            message=message, raw_line_no=line_no,
        )
    return None


def _parse_msvc(line: str, line_no: int) -> Diagnostic | None:
    if "(" not in line and "LNK" not in line and ":" not in line:
        return None
    stripped = _strip_msbuild_suffix(line)
    match = _MSVC_CL_RE.match(stripped)
    if match:
        code = match.group("code")
        tool = (
            DiagnosticTool.DOTNET.value if _DOTNET_CODE_RE.match(code)
            else DiagnosticTool.MSVC.value
        )
        return make_diagnostic(
            tool=tool, severity=_severity(match.group("sev")),
            file=match.group("file").strip(), line=match.group("line"),
            col=match.group("col"), code=code, message=match.group("msg"),
            raw_line_no=line_no,
        )
    if "LNK" in stripped:
        match = _MSVC_LINK_RE.match(stripped)
        if match:
            return make_diagnostic(
                tool=DiagnosticTool.MSVC_LINK.value, severity=_severity(match.group("sev")),
                file=match.group("file").strip(), code=match.group("code"),
                message=match.group("msg"), raw_line_no=line_no,
            )
    match = _MSBUILD_RE.match(stripped)
    if match:
        return make_diagnostic(
            tool=DiagnosticTool.DOTNET.value, severity=_severity(match.group("sev")),
            file=match.group("file").strip(), code=match.group("code"),
            message=match.group("msg"), raw_line_no=line_no,
        )
    return None


def _parse_tsc(line: str, line_no: int) -> Diagnostic | None:
    if " TS" not in line:
        return None
    match = _TSC_RE.match(line) or _TSC_PRETTY_RE.match(line)
    if not match:
        return None
    return make_diagnostic(
        tool=DiagnosticTool.TSC.value, severity=_severity(match.group("sev")),
        file=match.group("file"), line=match.group("line"), col=match.group("col"),
        code=match.group("code"), message=match.group("msg"), raw_line_no=line_no,
    )


def _parse_eslint_unix(line: str, line_no: int) -> Diagnostic | None:
    if not line.endswith("]") or "/" not in line:
        return None
    match = _ESLINT_UNIX_RE.match(line)
    if not match:
        return None
    return make_diagnostic(
        tool=DiagnosticTool.ESLINT.value, severity=_severity(match.group("sev")),
        file=match.group("file"), line=match.group("line"), col=match.group("col"),
        code=match.group("code"), message=match.group("msg"), raw_line_no=line_no,
    )


def _parse_eslint_row(line: str, header: str, line_no: int) -> Diagnostic | None:
    match = _ESLINT_ROW_RE.match(line)
    if not match:
        return None
    rest = match.group("rest").rstrip()
    split = max(rest.rfind("  "), -1)
    if split < 0:
        return None
    message = rest[:split].rstrip()
    rule = rest[split:].strip()
    if not message or not _ESLINT_RULE_RE.match(rule):
        return None
    return make_diagnostic(
        tool=DiagnosticTool.ESLINT.value, severity=_severity(match.group("sev")),
        file=header, line=match.group("line"), col=match.group("col"),
        code=rule, message=message, raw_line_no=line_no,
    )


def _parse_go(line: str, line_no: int) -> Diagnostic | None:
    if ".go:" not in line:
        return None
    match = _GO_RE.match(line)
    if not match:
        return None
    return make_diagnostic(
        tool=DiagnosticTool.GO.value, severity=Severity.ERROR.value,
        file=match.group("file"), line=match.group("line"), col=match.group("col"),
        message=match.group("msg"), raw_line_no=line_no,
    )


def _parse_pytest(line: str, line_no: int) -> Diagnostic | None:
    if not (line.startswith("FAILED ") or line.startswith("ERROR ")):
        return None
    match = _PYTEST_RE.match(line)
    if not match:
        return None
    nodeid = match.group("nodeid")
    return make_diagnostic(
        tool=DiagnosticTool.PYTEST.value, severity=Severity.ERROR.value,
        file=nodeid.split("::", 1)[0], code=match.group("kind"),
        message=match.group("msg") or nodeid, raw_line_no=line_no,
    )


def _parse_rust_header(line: str, line_no: int) -> tuple[Diagnostic | None, bool]:
    """(diagnostic, is_rust_header) for a location-less ``error:`` line."""
    if not (line.startswith("error") or line.startswith("warning")):
        return None, False
    if _RUST_DROPPED_RE.match(line):
        return None, True
    match = _RUST_HEADER_RE.match(line)
    if not match:
        return None, False
    return make_diagnostic(
        tool=DiagnosticTool.RUSTC.value, severity=_severity(match.group("sev")),
        code=match.group("code") or "", message=match.group("msg"),
        raw_line_no=line_no,
    ), True


_SINGLE_LINE_PARSERS = (
    (DiagnosticTool.TSC.value, _parse_tsc),
    (DiagnosticTool.MSVC.value, _parse_msvc),
    (DiagnosticTool.GNU.value, _parse_gnu),
    (DiagnosticTool.ESLINT.value, _parse_eslint_unix),
    (DiagnosticTool.GO.value, _parse_go),
    (DiagnosticTool.PYTEST.value, _parse_pytest),
)

# A single grammar may emit a tool other than its own name (MSVC emits
# dotnet and msvc_link; GNU covers ld).  Map every emitted tool to the
# grammar whose filter admits it.
_GRAMMAR_FOR_TOOL = {
    DiagnosticTool.DOTNET.value: DiagnosticTool.MSVC.value,
    DiagnosticTool.MSVC_LINK.value: DiagnosticTool.MSVC.value,
}


def _grammar_enabled(grammar: str, allowed: frozenset[str]) -> bool:
    if grammar in allowed:
        return True
    return any(_GRAMMAR_FOR_TOOL.get(tool) == grammar for tool in allowed)


def _admit(diagnostic: Diagnostic | None, allowed: frozenset[str]) -> Diagnostic | None:
    if diagnostic is None or diagnostic.tool not in allowed:
        return None
    return diagnostic


def _prepare(line: str, max_line_chars: int) -> str:
    text = strip_ansi(line[: max_line_chars * 2])[:max_line_chars]
    return text.rstrip("\r\n").rstrip()


def parse_line(line: str, *, tools: Iterable[str] | None = None) -> Diagnostic | None:
    """Parse one self-contained diagnostic line, or return None.

    Multi-line shapes (rustc ``-->`` locations, eslint stylish blocks, Python
    tracebacks) need ``parse_diagnostics``; here a rustc header yields its
    location-less diagnostic.
    """
    allowed = _allowed(tools)
    text = _prepare(str(line or ""), HARD_MAX_LINE_CHARS)
    if not text.strip():
        return None
    for grammar, parser in _SINGLE_LINE_PARSERS:
        if not _grammar_enabled(grammar, allowed):
            continue
        found = _admit(parser(text, 0), allowed)
        if found is not None:
            return found
    if DiagnosticTool.RUSTC.value in allowed:
        diagnostic, _ = _parse_rust_header(text, 0)
        return diagnostic
    return None


class _Collector:
    """Bounded dedupe + grouping accumulator."""

    def __init__(self, max_diagnostics: int) -> None:
        self.max = max_diagnostics
        self.kept: list[Diagnostic] = []
        self.seen: set[tuple] = set()
        self.counts: dict[str, int] = {}
        self.groups: dict[str, dict] = {}
        self.matched: list[str] = []
        self.truncated = False

    def add(self, diagnostic: Diagnostic) -> None:
        key = diagnostic.dedupe_key()
        if key in self.seen:
            return
        self.seen.add(key)
        self.counts[diagnostic.severity] = self.counts.get(diagnostic.severity, 0) + 1
        if diagnostic.tool not in self.matched:
            self.matched.append(diagnostic.tool)
        signature = diagnostic.signature()
        group = self.groups.get(signature)
        if group is None:
            self.groups[signature] = {
                "first": diagnostic, "count": 1,
                "files": [diagnostic.file] if diagnostic.file else [],
            }
        else:
            group["count"] += 1
            if (diagnostic.file and diagnostic.file not in group["files"]
                    and len(group["files"]) < MAX_GROUP_FILES):
                group["files"].append(diagnostic.file)
        if len(self.kept) < self.max:
            self.kept.append(diagnostic)
        else:
            self.truncated = True

    def result(self, *, truncated: bool, max_groups: int) -> DiagnosticSet:
        groups = []
        for signature, row in self.groups.items():
            first = row["first"]
            groups.append(DiagnosticGroup(
                signature=signature, tool=first.tool, severity=first.severity,
                code=first.code, template=message_template(first.message),
                count=row["count"], first=first, files=tuple(row["files"]),
            ))
        groups.sort(key=lambda group: (
            SEVERITY_RANK.get(group.severity, 9), -group.count, group.first.raw_line_no,
        ))
        grouped_truncated = len(groups) > max_groups
        counts = tuple(
            (severity, self.counts[severity])
            for severity in sorted(self.counts, key=lambda s: SEVERITY_RANK.get(s, 9))
        )
        return DiagnosticSet(
            diagnostics=tuple(self.kept),
            groups=tuple(groups[:max_groups]),
            counts=counts,
            truncated=bool(truncated or self.truncated or grouped_truncated),
            parsers_matched=tuple(self.matched),
        )


def parse_diagnostics(
    text: str,
    *,
    tools: Iterable[str] | None = None,
    max_diagnostics: int = HARD_MAX_DIAGNOSTICS,
    max_lines: int = HARD_MAX_LINES,
    max_line_chars: int = HARD_MAX_LINE_CHARS,
    max_groups: int = HARD_MAX_GROUPS,
) -> DiagnosticSet:
    """Scan bounded output for every supported diagnostic shape.

    ``counts`` are per-severity totals over every distinct diagnostic found in
    the scanned window, taken before ``max_diagnostics`` truncation, so a
    capped list never under-reports how many errors there were.
    """
    allowed = _allowed(tools)
    max_diagnostics = max(1, min(int(max_diagnostics), HARD_MAX_DIAGNOSTICS))
    max_lines = max(1, min(int(max_lines), HARD_MAX_LINES))
    max_line_chars = max(64, min(int(max_line_chars), HARD_MAX_LINE_CHARS))
    max_groups = max(1, min(int(max_groups), HARD_MAX_GROUPS))
    raw_lines = str(text or "").splitlines()
    scan_truncated = len(raw_lines) > max_lines
    lines = [_prepare(line, max_line_chars) for line in raw_lines[:max_lines]]
    collector = _Collector(max_diagnostics)
    python_on = DiagnosticTool.PYTHON.value in allowed
    rust_on = DiagnosticTool.RUSTC.value in allowed
    eslint_on = DiagnosticTool.ESLINT.value in allowed
    eslint_header = ""
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        line_no = index + 1
        if not line.strip():
            eslint_header = ""
            index += 1
            continue

        if python_on and line.strip() == _TRACEBACK_HEADER:
            consumed, diagnostic = _parse_traceback(lines, index)
            if diagnostic is not None:
                collector.add(diagnostic)
            index += max(1, consumed)
            continue
        if python_on and line.startswith("  File \""):
            consumed, diagnostic = _parse_bare_syntax_error(lines, index)
            if diagnostic is not None:
                collector.add(diagnostic)
                index += consumed
                continue

        if eslint_on and eslint_header and line[:1].isspace():
            diagnostic = _parse_eslint_row(line, eslint_header, line_no)
            if diagnostic is not None:
                collector.add(diagnostic)
                index += 1
                continue
        if not line[:1].isspace():
            eslint_header = line if (eslint_on and _ESLINT_HEADER_RE.match(line)) else ""

        found = None
        for grammar, parser in _SINGLE_LINE_PARSERS:
            if not _grammar_enabled(grammar, allowed):
                continue
            found = _admit(parser(line, line_no), allowed)
            if found is not None:
                break
        if found is not None:
            collector.add(found)
            index += 1
            continue

        if rust_on:
            diagnostic, is_header = _parse_rust_header(line, line_no)
            located = _admit(
                _locate_rust(diagnostic, lines, index) if diagnostic is not None else None,
                allowed,
            )
            if located is not None:
                collector.add(located)
            if is_header:
                index += 1
                continue
        index += 1
    return collector.result(truncated=scan_truncated, max_groups=max_groups)


def _locate_rust(diagnostic: Diagnostic, lines: list[str], index: int) -> Diagnostic:
    """Attach the ``-->`` location printed within the next few lines."""
    for offset in range(1, _RUST_LOOKAHEAD + 1):
        if index + offset >= len(lines):
            break
        candidate = lines[index + offset]
        match = _RUST_LOCATION_RE.match(candidate)
        if match:
            return make_diagnostic(
                tool=diagnostic.tool, severity=diagnostic.severity,
                file=match.group("file"), line=match.group("line"),
                col=match.group("col"), code=diagnostic.code,
                message=diagnostic.message, raw_line_no=diagnostic.raw_line_no,
            )
        if _RUST_HEADER_RE.match(candidate):
            break
    if diagnostic.code or diagnostic.message.startswith("could not compile"):
        return diagnostic
    # A bare ``error: <text>`` with no rustc location or code is some other
    # tool's diagnostic in the same shape; keep it, attributed honestly.
    return make_diagnostic(
        tool=DiagnosticTool.GENERIC.value, severity=diagnostic.severity,
        message=diagnostic.message, raw_line_no=diagnostic.raw_line_no,
    )


def _parse_traceback(lines: list[str], start: int) -> tuple[int, Diagnostic | None]:
    """One ``Traceback (most recent call last):`` block (bounded)."""
    last_file = ""
    last_line: str | None = None
    end = min(len(lines), start + 1 + _TRACEBACK_MAX_LINES)
    index = start + 1
    while index < end:
        line = lines[index]
        frame = _PY_FRAME_RE.match(line)
        if frame:
            last_file, last_line = frame.group("file"), frame.group("line")
            index += 1
            continue
        if not line.strip() or line[:1].isspace():
            index += 1
            continue
        exception = _PY_EXCEPTION_RE.match(line.strip())
        if exception and last_file:
            code = exception.group("exc")
            message = exception.group("msg") or code
            return index - start + 1, make_diagnostic(
                tool=DiagnosticTool.PYTHON.value, severity=Severity.ERROR.value,
                file=last_file, line=last_line, code=code, message=message,
                raw_line_no=index + 1,
            )
        return index - start, None
    return index - start, None


def _parse_bare_syntax_error(lines: list[str], start: int) -> tuple[int, Diagnostic | None]:
    """``File "x", line N`` + source + caret + ``SyntaxError: ...`` with no header."""
    frame = _PY_FRAME_RE.match(lines[start])
    if not frame:
        return 1, None
    for offset in range(1, 5):
        index = start + offset
        if index >= len(lines):
            break
        candidate = lines[index]
        if not candidate[:1].isspace():
            match = _PY_SYNTAX_RE.match(candidate.strip())
            if match:
                code = match.group("exc")
                return offset + 1, make_diagnostic(
                    tool=DiagnosticTool.PYTHON.value, severity=Severity.ERROR.value,
                    file=frame.group("file"), line=frame.group("line"),
                    code=code, message=match.group("msg") or code,
                    raw_line_no=index + 1,
                )
            break
    return 1, None


__all__ = [
    "ALL_TOOLS", "DEFAULT_ERROR_LINE_PATTERN", "HARD_MAX_DIAGNOSTICS",
    "HARD_MAX_GROUPS", "HARD_MAX_LINES", "HARD_MAX_LINE_CHARS",
    "error_lines", "parse_diagnostics", "parse_line",
]
