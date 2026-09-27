"""Typed build and test diagnostics shared by every Sonder surface.

Pure domain module: no I/O, no environment, no clock. A ``Diagnostic`` is one
normalized compiler, linker, type-checker, linter, interpreter, or test-runner
finding; ``DiagnosticSet`` is the bounded, deduplicated, signature-grouped
result of scanning one output window.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum


MAX_FILE_CHARS = 260
MAX_CODE_CHARS = 64
MAX_MESSAGE_CHARS = 400
MAX_TEMPLATE_CHARS = 400
MAX_GROUP_FILES = 8


class Severity(str, Enum):
    FATAL = "fatal"
    ERROR = "error"
    WARNING = "warning"
    NOTE = "note"


# Lower rank sorts first: fatal findings lead, notes trail.
SEVERITY_RANK = {
    Severity.FATAL.value: 0,
    Severity.ERROR.value: 1,
    Severity.WARNING.value: 2,
    Severity.NOTE.value: 3,
}


class DiagnosticTool(str, Enum):
    GNU = "gnu"  # gcc, clang and GNU ld share one message style
    MSVC = "msvc"
    MSVC_LINK = "msvc_link"
    RUSTC = "rustc"
    TSC = "tsc"
    ESLINT = "eslint"
    GO = "go"
    DOTNET = "dotnet"
    PYTHON = "python"
    PYTEST = "pytest"
    GENERIC = "generic"


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]{0,256}(?:\x07|\x1b\\)")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_SPACE_RE = re.compile(r"\s+")

_TEMPLATE_QUOTED_RE = re.compile(
    r"'[^'\n]{0,200}'|\"[^\"\n]{0,200}\"|`[^`\n]{0,200}`|‘[^’\n]{0,200}’"
)
# A path token: starts at a token boundary (so non-token positions fail at
# once) and contains at least one separator. Linear in the token length.
_TEMPLATE_PATH_RE = re.compile(
    r"(?:(?<=[\s'\"`(\[=,:<])|^)[^\s'\"`()\[\],<>]*[/\\][^\s'\"`()\[\],<>]*"
)
_TEMPLATE_HEX_RE = re.compile(r"\b0[xX][0-9A-Fa-f]+\b")
_TEMPLATE_NUMBER_RE = re.compile(r"\d+")


def strip_ansi(text: str) -> str:
    """Remove terminal colour and OSC sequences."""
    return _ANSI_RE.sub("", text)


def clean_text(value: object, limit: int) -> str:
    """One printable line: ANSI stripped, controls removed, capped."""
    text = strip_ansi(str(value if value is not None else ""))
    text = text.replace("\t", " ").replace("\r", " ").replace("\n", " ")
    text = _CONTROL_RE.sub("", text)
    text = _SPACE_RE.sub(" ", text).strip()
    return text[: max(0, int(limit))]


def normalize_file(path: object) -> str:
    """A ``/``-separated file reference, at most ``MAX_FILE_CHARS``."""
    text = clean_text(path, 4096).replace("\\", "/")
    if text.startswith("./") and len(text) > 2:
        text = text[2:]
    return text[:MAX_FILE_CHARS]


def message_template(message: str) -> str:
    """Stable shape of a message: quoted names, paths, hex and numbers masked."""
    value = _TEMPLATE_QUOTED_RE.sub("<q>", str(message or ""))
    value = _TEMPLATE_PATH_RE.sub("<path>", value)
    value = _TEMPLATE_HEX_RE.sub("<hex>", value)
    value = _TEMPLATE_NUMBER_RE.sub("<n>", value)
    return _SPACE_RE.sub(" ", value).strip()[:MAX_TEMPLATE_CHARS]


@dataclass(frozen=True, slots=True)
class Diagnostic:
    tool: str
    severity: str
    file: str
    line: int | None
    col: int | None
    code: str
    message: str
    raw_line_no: int = 0

    def signature(self) -> str:
        """First 16 hex chars of sha256 over tool|severity|code|template."""
        material = "%s|%s|%s|%s" % (
            self.tool, self.severity, self.code, message_template(self.message),
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    def dedupe_key(self) -> tuple:
        return (self.tool, self.severity, self.file, self.line, self.col,
                self.code, self.message)

    def location(self) -> str:
        """``file:line:col`` with absent parts omitted ("" when no file)."""
        if not self.file:
            return ""
        text = self.file
        if self.line is not None:
            text += ":%d" % self.line
            if self.col is not None:
                text += ":%d" % self.col
        return text


def make_diagnostic(
    *,
    tool: str,
    severity: str,
    file: object = "",
    line: object = None,
    col: object = None,
    code: object = "",
    message: object = "",
    raw_line_no: int = 0,
) -> Diagnostic:
    """Build a bounded, normalized ``Diagnostic`` from raw captured parts."""
    return Diagnostic(
        tool=str(tool),
        severity=str(severity),
        file=normalize_file(file) if file else "",
        line=_positive_int(line),
        col=_positive_int(col),
        code=clean_text(code, MAX_CODE_CHARS),
        message=clean_text(message, MAX_MESSAGE_CHARS),
        raw_line_no=max(0, int(raw_line_no)),
    )


def _positive_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    if number < 0 or number > 10_000_000:
        return None
    return number


@dataclass(frozen=True, slots=True)
class DiagnosticGroup:
    signature: str
    tool: str
    severity: str
    code: str
    template: str
    count: int
    first: Diagnostic
    files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DiagnosticSet:
    diagnostics: tuple[Diagnostic, ...]
    groups: tuple[DiagnosticGroup, ...]
    counts: tuple[tuple[str, int], ...]
    truncated: bool
    parsers_matched: tuple[str, ...]

    def count(self, severity: str) -> int:
        return dict(self.counts).get(severity, 0)


def diagnostic_to_wire(diagnostic: Diagnostic) -> dict:
    return {
        "tool": diagnostic.tool,
        "severity": diagnostic.severity,
        "file": diagnostic.file,
        "line": diagnostic.line,
        "col": diagnostic.col,
        "code": diagnostic.code,
        "message": diagnostic.message,
        "raw_line_no": diagnostic.raw_line_no,
        "signature": diagnostic.signature(),
    }


def group_to_wire(group: DiagnosticGroup) -> dict:
    return {
        "signature": group.signature,
        "tool": group.tool,
        "severity": group.severity,
        "code": group.code,
        "template": group.template,
        "count": group.count,
        "first": diagnostic_to_wire(group.first),
        "files": list(group.files),
    }


def render_diagnostic(diagnostic: Diagnostic, *, max_chars: int = 300) -> str:
    """``file:line:col [code] message`` on one line."""
    parts = []
    location = diagnostic.location()
    if location:
        parts.append(location)
    parts.append(diagnostic.severity)
    if diagnostic.code:
        parts.append("[%s]" % diagnostic.code)
    parts.append(diagnostic.message)
    return " ".join(part for part in parts if part)[:max_chars]


__all__ = [
    "DiagnosticTool", "Diagnostic", "DiagnosticGroup", "DiagnosticSet",
    "MAX_CODE_CHARS", "MAX_FILE_CHARS", "MAX_GROUP_FILES", "MAX_MESSAGE_CHARS",
    "SEVERITY_RANK", "Severity", "clean_text", "diagnostic_to_wire",
    "group_to_wire", "make_diagnostic", "message_template", "normalize_file",
    "render_diagnostic", "strip_ansi",
]
