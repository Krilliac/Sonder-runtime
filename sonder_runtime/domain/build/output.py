"""Build-output layers on top of ``domain.diagnostics``.

``domain.diagnostics.parsers.parse_diagnostics`` stays the one diagnostic
grammar; this module adds what a build needs around it:

- which step (ninja edge, make rule, MSBuild project) printed which lines,
- CMake configure errors,
- include traces (``-H`` and ``/showIncludes``),
- the F3 fallback for MSVC-shaped diagnostics without a ``C####`` code
  (clang-cl's ``file(l,c): error: msg``), merged into the diagnostic set.

Line numbers are 1-based over ``str.splitlines()`` -- the same numbering
``parse_diagnostics`` uses for ``Diagnostic.raw_line_no`` -- so every function
here must be given the very string that was parsed.
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass

from ..diagnostics.model import (
    MAX_GROUP_FILES,
    SEVERITY_RANK,
    Diagnostic,
    DiagnosticGroup,
    DiagnosticSet,
    DiagnosticTool,
    make_diagnostic,
    message_template,
    normalize_file,
    strip_ansi,
)
from ..diagnostics.parsers import HARD_MAX_DIAGNOSTICS, HARD_MAX_LINE_CHARS, parse_diagnostics
from .compile_db import SOURCE_SUFFIXES


MAX_SEGMENTS = 20_000
MAX_TRACE_EDGES = 5000
MAX_CONFIGURE_MESSAGE_LINES = 20
_NINJA_STEP_RE = re.compile(r"^\[(\d{1,9})/(\d{1,9})\] (.{0,4096})$")
_NINJA_FAILED = "FAILED: "
_NINJA_END_RE = re.compile(r"^ninja: (?:build stopped|error|fatal)")
_MAKE_STEP_RE = re.compile(r"^\[\s*\d{1,3}%\] (?:Building|Linking|Generating|Built) ")
_MAKE_BUILDING_RE = re.compile(r"^\[\s*\d{1,3}%\] Building \w+ object (\S{1,4096})$")
_MAKE_LINKING_RE = re.compile(r"^\[\s*\d{1,3}%\] Linking \w+ \S+ (\S{1,4096})$")
_MAKE_FAIL_RE = re.compile(
    r"^(?:g?make|mingw32-make)(?:\[\d+\])?: \*\*\* \[(?:(?P<file>[^:\]\n]{1,1024}):(?P<line>\d+): )?"
    r"(?P<target>[^\]\n]{1,4096})\] Error \d+"
)
_NINJA_OBJECT_RE = re.compile(r"(?:^|/)CMakeFiles/(?P<target>[^/]{1,256})\.dir/(?P<rest>.+?)\.(?:o|obj)$")
_CMAKE_ERROR_RE = re.compile(
    r"^CMake (?P<kind>Error|Warning|Deprecation Warning|Warning \(dev\))"
    r"(?: at (?P<file>[^:\n]{1,1024}):(?P<line>\d+)(?: \((?P<command>[A-Za-z_0-9]{1,64})\))?)?:?\s*(?P<rest>.*)$"
)
_MSVC_CODELESS_RE = re.compile(
    r"^(?P<file>(?:[A-Za-z]:)?[^(\n]{1,1024})\((?P<line>\d{1,9})(?:,(?P<col>\d{1,9}))?\)\s*:\s*"
    r"(?P<sev>fatal error|error|warning|note)\s*:\s*(?P<msg>.*)$"
)
_MSBUILD_PROJECT_SUFFIX_RE = re.compile(r"\s\[(?P<project>[^\]\n]{1,1024}\.(?:vcx|cs|fs|vb)proj)\]$")
_MSBUILD_NODE_RE = re.compile(r"^\s*(?:\d+>)?(?:Project \"|Build started|Done Building Project)")
_CL_ECHO_RE = re.compile(r"^\s{0,8}(?:\d+>)?\s*([A-Za-z0-9_.+ -]{1,255}\.(?:c|cc|cpp|cxx|c\+\+))\s*$",
                         re.IGNORECASE)
_SHOW_INCLUDES_PREFIXES = (
    "Note: including file:",
    "Hinweis: Einlesen der Datei:",
    "Remarque : inclusion du fichier :",
    "Remarque : inclusion du fichier :",
)
_SEVERITY = {"fatal error": "fatal", "error": "error", "warning": "warning", "note": "note"}


@dataclass(frozen=True, slots=True)
class StepSegment:
    system: str
    step_label: str
    tu_label: str = ""
    project: str = ""
    first_line: int = 0
    last_line: int = 0
    failed: bool = False

    def contains(self, line_no: int) -> bool:
        return self.first_line <= line_no <= self.last_line


@dataclass(frozen=True, slots=True)
class IncludeTrace:
    root_file: str
    edges: tuple[tuple[str, str, int], ...]
    truncated: bool
    unique_headers: int
    max_depth: int
    pch_consumed: bool

    def headers(self) -> tuple[str, ...]:
        seen: list[str] = []
        for _parent, child, _depth in self.edges:
            if child not in seen:
                seen.append(child)
        return tuple(seen)


def _lines(text: str) -> list[str]:
    return str(text or "").splitlines()


def _clean(line: str) -> str:
    return strip_ansi(line[: HARD_MAX_LINE_CHARS * 2])[:HARD_MAX_LINE_CHARS].rstrip()


def _source_from_command(command: str) -> str:
    tokens = command.split()
    for index, token in enumerate(tokens):
        if token in ("-c", "/c") and index + 1 < len(tokens):
            candidate = tokens[index + 1].strip('"')
            if posixpath.splitext(candidate)[1].lower() in SOURCE_SUFFIXES:
                return normalize_file(candidate)
        if token.startswith(("/Tp", "/Tc", "-Tp", "-Tc")) and len(token) > 3:
            return normalize_file(token[3:].strip('"'))
    for token in reversed(tokens):
        candidate = token.strip('"')
        if posixpath.splitext(candidate)[1].lower() in SOURCE_SUFFIXES:
            return normalize_file(candidate)
    return ""


def object_to_source(output: str) -> tuple[str, str]:
    """(target, source path relative to the target dir) for a CMake object path."""
    match = _NINJA_OBJECT_RE.search(normalize_file(output))
    if not match:
        return "", ""
    rest = match.group("rest")
    if posixpath.splitext(rest)[1].lower() not in SOURCE_SUFFIXES:
        return match.group("target"), ""
    return match.group("target"), rest


def parse_ninja_segments(text: str) -> tuple[StepSegment, ...]:
    """One segment per ``[n/m]`` step; ``FAILED:`` marks it failed.

    A failed step owns the ``FAILED:`` line, the command line after it and
    every output line up to the next step or the ``ninja:`` trailer, so
    header diagnostics printed while compiling a TU belong to that TU.
    """
    lines = _lines(text)
    segments: list[StepSegment] = []
    current: dict | None = None

    def close(last: int) -> None:
        nonlocal current
        if current is not None and len(segments) < MAX_SEGMENTS:
            segments.append(StepSegment(
                system="ninja", step_label=current["label"][:1024], tu_label=current["tu"][:1024],
                project=current["project"][:256], first_line=current["first"],
                last_line=max(current["first"], last), failed=current["failed"],
            ))
        current = None

    for index, raw in enumerate(lines):
        line_no = index + 1
        line = _clean(raw)
        step = _NINJA_STEP_RE.match(line)
        if step:
            close(line_no - 1)
            description = step.group(3)
            current = {"label": description, "tu": "", "project": "", "first": line_no,
                       "failed": False, "want_command": False}
            obj = description.rsplit(" ", 1)[-1] if " object " in description else ""
            if obj:
                target, rest = object_to_source(obj)
                current["project"] = target
                if rest:
                    current["tu"] = rest
            continue
        if line.startswith(_NINJA_FAILED):
            if current is None:
                current = {"label": "", "tu": "", "project": "", "first": line_no,
                           "failed": False, "want_command": False}
            outputs = line[len(_NINJA_FAILED):].strip()
            current["failed"] = True
            current["label"] = outputs.split(" ", 1)[0] if outputs else current["label"]
            target, rest = object_to_source(current["label"])
            if target:
                current["project"] = target
            if rest and not current["tu"]:
                current["tu"] = rest
            current["want_command"] = True
            continue
        if current is not None and current.get("want_command"):
            current["want_command"] = False
            source = _source_from_command(line)
            if source:
                current["tu"] = source
            continue
        if _NINJA_END_RE.match(line):
            close(line_no - 1)
            continue
    close(len(lines))
    return tuple(segments)


def parse_make_failures(text: str) -> tuple[StepSegment, ...]:
    """Failed make rules, each spanning from its ``[ xx%] Building`` line."""
    lines = _lines(text)
    starts: dict[str, int] = {}
    last_step = 0
    segments: list[StepSegment] = []
    for index, raw in enumerate(lines):
        line_no = index + 1
        line = _clean(raw)
        building = _MAKE_BUILDING_RE.match(line)
        linking = _MAKE_LINKING_RE.match(line)
        if building:
            starts[normalize_file(building.group(1))] = line_no
            last_step = line_no
            continue
        if linking:
            starts[normalize_file(linking.group(1))] = line_no
            last_step = line_no
            continue
        if _MAKE_STEP_RE.match(line):
            last_step = line_no
            continue
        failure = _MAKE_FAIL_RE.match(line)
        if not failure:
            continue
        target = normalize_file(failure.group("target").strip())
        if posixpath.basename(target) in ("all", "default_target") or target.endswith("/all") \
                or target.endswith("/build"):
            continue
        start = starts.get(target)
        if start is None:
            for key, value in starts.items():
                if key.endswith("/" + target) or target.endswith("/" + key):
                    start = value
                    break
        if start is None:
            start = last_step + 1 if last_step else max(1, line_no - 1)
        owner, rest = object_to_source(target)
        if len(segments) < MAX_SEGMENTS:
            segments.append(StepSegment(
                system="make", step_label=target[:1024], tu_label=rest[:1024],
                project=owner[:256], first_line=start, last_line=line_no, failed=True,
            ))
    return tuple(segments)


def parse_cmake_configure_errors(text: str) -> tuple[Diagnostic, ...]:
    """``CMake Error at CMakeLists.txt:5 (message):`` blocks as diagnostics."""
    lines = _lines(text)
    out: list[Diagnostic] = []
    index = 0
    while index < len(lines) and len(out) < HARD_MAX_DIAGNOSTICS:
        line = _clean(lines[index])
        match = _CMAKE_ERROR_RE.match(line)
        if not match:
            index += 1
            continue
        kind = match.group("kind")
        body: list[str] = []
        rest = (match.group("rest") or "").strip()
        if rest:
            body.append(rest)
        cursor = index + 1
        while cursor < len(lines) and len(body) < MAX_CONFIGURE_MESSAGE_LINES:
            follow = _clean(lines[cursor])
            if not follow.strip():
                if body:
                    break
                cursor += 1
                continue
            if not follow[:1].isspace() and body:
                break
            body.append(follow.strip())
            cursor += 1
        severity = "error" if kind == "Error" else "warning"
        code = "CMAKE" if kind == "Error" else "CMAKE_WARNING"
        command = match.group("command")
        message = " ".join(body) or kind
        if command:
            message = "%s: %s" % (command, message)
        out.append(make_diagnostic(
            tool=DiagnosticTool.GENERIC.value, severity=severity, file=match.group("file") or "",
            line=match.group("line"), code=code, message=message, raw_line_no=index + 1,
        ))
        index = max(cursor, index + 1)
    return tuple(out)


def parse_msvc_codeless(text: str) -> tuple[Diagnostic, ...]:
    """F3 fallback: ``file(l,c): error: msg`` lines (clang-cl) with no ``C####`` code."""
    out: list[Diagnostic] = []
    for index, raw in enumerate(_lines(text)):
        if "(" not in raw or ":" not in raw:
            continue
        line = _clean(raw)
        stripped = _MSBUILD_PROJECT_SUFFIX_RE.sub("", line)
        match = _MSVC_CODELESS_RE.match(stripped)
        if not match:
            continue
        out.append(make_diagnostic(
            tool=DiagnosticTool.MSVC.value, severity=_SEVERITY[match.group("sev")],
            file=match.group("file").strip(), line=match.group("line"), col=match.group("col"),
            message=match.group("msg"), raw_line_no=index + 1,
        ))
        if len(out) >= HARD_MAX_DIAGNOSTICS * 4:
            break
    return tuple(out)


def _rebuild(diagnostics: list[Diagnostic], *, truncated: bool, max_diagnostics: int,
             max_groups: int = 50) -> DiagnosticSet:
    """A DiagnosticSet over an explicit list (same dedupe/grouping rules as -4)."""
    seen: set[tuple] = set()
    kept: list[Diagnostic] = []
    counts: dict[str, int] = {}
    groups: dict[str, dict] = {}
    matched: list[str] = []
    capped = False
    for item in sorted(diagnostics, key=lambda d: d.raw_line_no):
        key = item.dedupe_key()
        if key in seen:
            continue
        seen.add(key)
        counts[item.severity] = counts.get(item.severity, 0) + 1
        if item.tool not in matched:
            matched.append(item.tool)
        signature = item.signature()
        group = groups.get(signature)
        if group is None:
            groups[signature] = {"first": item, "count": 1, "files": [item.file] if item.file else []}
        else:
            group["count"] += 1
            if item.file and item.file not in group["files"] and len(group["files"]) < MAX_GROUP_FILES:
                group["files"].append(item.file)
        if len(kept) < max_diagnostics:
            kept.append(item)
        else:
            capped = True
    built = [
        DiagnosticGroup(signature=signature, tool=row["first"].tool, severity=row["first"].severity,
                        code=row["first"].code, template=message_template(row["first"].message),
                        count=row["count"], first=row["first"], files=tuple(row["files"]))
        for signature, row in groups.items()
    ]
    built.sort(key=lambda g: (SEVERITY_RANK.get(g.severity, 9), -g.count, g.first.raw_line_no))
    return DiagnosticSet(
        diagnostics=tuple(kept), groups=tuple(built[:max_groups]),
        counts=tuple((sev, counts[sev]) for sev in sorted(counts, key=lambda s: SEVERITY_RANK.get(s, 9))),
        truncated=bool(truncated or capped or len(built) > max_groups),
        parsers_matched=tuple(matched),
    )


def parse_build_diagnostics(text: str, *, max_diagnostics: int = HARD_MAX_DIAGNOSTICS,
                            configure: bool = False) -> DiagnosticSet:
    """-4 ``parse_diagnostics`` plus codeless MSVC and (optionally) CMake configure errors."""
    limit = max(1, min(int(max_diagnostics), HARD_MAX_DIAGNOSTICS))
    base = parse_diagnostics(text, max_diagnostics=limit)
    extra: list[Diagnostic] = []
    parsed_lines = {item.raw_line_no for item in base.diagnostics}
    for item in parse_msvc_codeless(text):
        if item.raw_line_no not in parsed_lines:
            extra.append(item)
    if configure:
        extra.extend(parse_cmake_configure_errors(text))
    if not extra:
        return base
    return _rebuild(list(base.diagnostics) + extra, truncated=base.truncated,
                    max_diagnostics=limit)


def msbuild_project_of_line(line: str) -> str:
    match = _MSBUILD_PROJECT_SUFFIX_RE.search(_clean(line))
    return normalize_file(match.group("project")) if match else ""


@dataclass(frozen=True, slots=True)
class ProjectAttribution:
    project: str
    raw_line_no: int
    tu_label: str = ""


def parse_msbuild_projects(text: str, dset: DiagnosticSet) -> tuple[ProjectAttribution, ...]:
    """Project (and, only where provable, TU) of each diagnostic from its raw line.

    ``text`` must be the string ``dset`` was parsed from: the ``[x.vcxproj]``
    suffix -4 strips is recovered at ``Diagnostic.raw_line_no``. A diagnostic
    located in a source file is attributed to that TU. A header diagnostic is
    attributed to a TU only when a single cl echo line directly precedes it
    with nothing but same-project diagnostics in between; under ``/MP`` cl
    echoes several file names first, so such a TU stays unknown.
    """
    lines = _lines(text)
    out: list[ProjectAttribution] = []
    for item in dset.diagnostics:
        index = item.raw_line_no - 1
        if index < 0 or index >= len(lines):
            continue
        project = msbuild_project_of_line(lines[index])
        tu = ""
        if project and posixpath.splitext(item.file)[1].lower() in SOURCE_SUFFIXES:
            # A diagnostic located in a source file belongs to that TU.
            tu = item.file
        elif project and item.tool == DiagnosticTool.MSVC.value:
            # Header diagnostics only; linker and MSBuild findings stay per project.
            cursor = index - 1
            while cursor >= 0 and index - cursor <= 200:
                previous = _clean(lines[cursor])
                if msbuild_project_of_line(previous) == project and not _CL_ECHO_RE.match(previous):
                    cursor -= 1
                    continue
                echo = _CL_ECHO_RE.match(previous)
                if echo and not _MSBUILD_NODE_RE.match(previous):
                    before = _clean(lines[cursor - 1]) if cursor > 0 else ""
                    if not _CL_ECHO_RE.match(before):
                        tu = normalize_file(echo.group(1).strip())
                break
        out.append(ProjectAttribution(project=project, raw_line_no=item.raw_line_no, tu_label=tu))
    return tuple(out)


def _trace(root_file: str, entries: list[tuple[int, str]], *, pch: bool,
           truncated: bool) -> IncludeTrace:
    edges: list[tuple[str, str, int]] = []
    stack: list[str] = [normalize_file(root_file)]
    unique: set[str] = set()
    deepest = 0
    for depth, header in entries:
        if depth < 1:
            continue
        if len(edges) >= MAX_TRACE_EDGES:
            truncated = True
            break
        del stack[depth:]
        parent = stack[-1] if stack else normalize_file(root_file)
        child = normalize_file(header)
        edges.append((parent, child, depth))
        unique.add(child)
        deepest = max(deepest, depth)
        stack.append(child)
    return IncludeTrace(root_file=normalize_file(root_file), edges=tuple(edges),
                        truncated=truncated, unique_headers=len(unique), max_depth=deepest,
                        pch_consumed=pch)


def parse_dash_H(text: str, *, root_file: str,  # noqa: N802 - flag name
                 forced_includes: tuple[str, ...] = ()) -> IncludeTrace:
    """GCC/Clang ``-H`` output: ``. hdr``, ``.. nested``; ``!`` marks a used PCH.

    ``-H`` does not list ``-include`` headers, so the ones the trace argv
    forced (``SanitizedArgv.forced_includes``) lead the trace at depth 1.
    """
    entries: list[tuple[int, str]] = [(1, item) for item in forced_includes[:16] if item]
    pch = False
    truncated = False
    for raw in _lines(text):
        line = _clean(raw)
        if line.startswith("Multiple include guards may be useful for:"):
            break
        if line.startswith("! "):
            pch = True
            continue
        if not line.startswith("."):
            continue
        depth = len(line) - len(line.lstrip("."))
        header = line[depth:].strip()
        if not header or depth > 256:
            continue
        entries.append((depth, header))
        if len(entries) > MAX_TRACE_EDGES:
            truncated = True
            break
    return _trace(root_file, entries, pch=pch, truncated=truncated)


def parse_show_includes(text: str, *, root_file: str) -> IncludeTrace:
    """cl/clang-cl ``/showIncludes``: nesting is the space count after the prefix."""
    entries: list[tuple[int, str]] = []
    truncated = False
    for raw in _lines(text):
        line = _clean(raw)
        for prefix in _SHOW_INCLUDES_PREFIXES:
            if line.startswith(prefix):
                rest = line[len(prefix):]
                header = rest.lstrip(" ")
                depth = len(rest) - len(header)
                if header and depth <= 256:
                    entries.append((max(1, depth), header))
                break
        if len(entries) > MAX_TRACE_EDGES:
            truncated = True
            break
    return _trace(root_file, entries, pch=False, truncated=truncated)


__all__ = [
    "IncludeTrace", "MAX_TRACE_EDGES", "ProjectAttribution", "StepSegment",
    "msbuild_project_of_line", "object_to_source", "parse_build_diagnostics",
    "parse_cmake_configure_errors", "parse_dash_H", "parse_make_failures",
    "parse_msbuild_projects", "parse_msvc_codeless", "parse_ninja_segments",
    "parse_show_includes",
]
