"""``heaptrack_print`` text reports.

The host template runs ``heaptrack_print <capture> --print-peaks 1
--print-allocators 1 --print-leaks 1 --print-temporary 1 --peak-limit 20``.
Its report has titled sections, each a list of entries::

    10 calls to allocation functions with 40.96K peak consumption from
    leak_buffer(unsigned long)
      at /src/leak.cpp:7
      in /build/leak
    10 calls with 40.96K peak consumption from:
        main
          ...

The unindented block after an entry header is the allocation site (with its
inlined callers); the indented ``from:`` blocks are merged caller paths and
are not needed for a hotspot. Sizes use heaptrack's SI suffixes (K = 1000).
Entries from every section are merged per allocation site into
``AllocationHotspot`` records: calls, peak, leaked, temporary, allocated.
"""
from __future__ import annotations

import re
from typing import Callable

from sonder_runtime.domain.profiling.folded import ProjectPredicate
from sonder_runtime.domain.profiling.model import (
    DEFAULT_LIMITS,
    MAX_ALLOCATIONS,
    AllocationHotspot,
    CaptureMetadata,
    ProfileDigest,
    ProfileFormatUnknown,
    ProfileLimits,
    ProfileParseError,
    ProfileSourceKind,
    WorkBudget,
    clip_name,
    finite_number,
    iter_bounded_lines,
    program_name,
)

_SIZE = r"([0-9]{1,15}(?:\.[0-9]{1,6})?)\s?([KMGTPE]?)i?B?"
_SECTIONS = {
    "MOST CALLS TO ALLOCATION FUNCTIONS": "calls",
    "PEAK MEMORY CONSUMERS": "peak",
    "MEMORY LEAKS": "leaks",
    "MOST TEMPORARY ALLOCATIONS": "temporary",
    "MOST MEMORY ALLOCATED OVER TIME (IGNORING DEALLOCATIONS)": "allocated",
    "MOST MEMORY ALLOCATED OVER TIME": "allocated",
}
_ENTRY = {
    "calls": re.compile(r"^(\d{1,18}) calls to allocation functions with %s peak consumption from$" % _SIZE),
    "peak": re.compile(r"^%s peak memory consumed over (\d{1,18}) calls from$" % _SIZE),
    "leaks": re.compile(r"^%s leaked over (\d{1,18}) calls from$" % _SIZE),
    "temporary": re.compile(r"^(\d{1,18}) temporary allocations of (\d{1,18}) allocations in total"),
    "allocated": re.compile(r"^%s allocated over (\d{1,18}) calls from$" % _SIZE),
}
_AT_RE = re.compile(r"^  at (.{1,4096}?):(\d{1,9})$")
_IN_RE = re.compile(r"^  in (.{1,4096})$")
_FOOTER = {
    "runtime": re.compile(r"^total runtime: ([0-9.]{1,20})s\.?$"),
    "calls": re.compile(r"^calls to allocation functions: (\d{1,18})"),
    "temporary": re.compile(r"^temporary memory allocations: (\d{1,18})"),
    "peak": re.compile(r"^peak heap memory consumption: %s$" % _SIZE),
    "rss": re.compile(r"^peak RSS \(including heaptrack overhead\): %s$" % _SIZE),
    "leaked": re.compile(r"^total memory leaked: %s$" % _SIZE),
}
_DEBUGGEE_RE = re.compile(r"^Debuggee command was: (.{0,1024})$")
# Allocator plumbing that should not be reported as "the" allocation site.
_PLUMBING_RE = re.compile(
    r"^(std::|__gnu_cxx::|operator new|operator delete|malloc|calloc|realloc|free|"
    r"__libc_|__GI_|_IO_|0x[0-9a-fA-F]+$|<unresolved)")
_MULT = {"": 1, "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15, "E": 10**18}
_MAX_SITES = 4096


def parse_size(number: str, suffix: str) -> int:
    value = finite_number(number) or 0.0
    return int(round(value * _MULT.get(suffix.upper(), 1)))


class _Site:
    __slots__ = ("function", "file", "line", "calls", "peak", "leaked", "temporary", "allocated")

    def __init__(self, function: str, file: str | None, line: int | None) -> None:
        self.function = function
        self.file = file
        self.line = line
        self.calls = 0
        self.peak: int | None = None
        self.leaked: int | None = None
        self.temporary: int | None = None
        self.allocated: int | None = None


def _pick(frames: list[tuple[str, str | None, int | None, str | None]],
          project: ProjectPredicate | None) -> tuple[str, str | None, int | None]:
    if project is not None:
        for name, file, line, module in frames:
            if project(name, module, file):
                return name, file, line
    for name, file, line, _module in frames:
        if not _PLUMBING_RE.match(name):
            return name, file, line
    name, file, line, _module = frames[0]
    return name, file, line


def parse_heaptrack_print(
    text: str,
    *,
    limits: ProfileLimits = DEFAULT_LIMITS,
    project: ProjectPredicate | None = None,
    clock: Callable[[], float] | None = None,
) -> ProfileDigest:
    budget = WorkBudget(limits.max_seconds, clock)
    sites: dict[tuple, _Site] = {}
    footer: dict[str, int | float] = {}
    process: str | None = None
    section: str | None = None
    entry: tuple[str, re.Match] | None = None
    frames: list[tuple[str, str | None, int | None, str | None]] = []
    in_site = False
    seen_marker = False
    truncated = False
    oversize = 0

    def flush() -> None:
        nonlocal entry, frames, in_site, truncated
        if entry is not None and frames:
            kind, match = entry
            name, file, line = _pick(frames, project)
            key = (name, file, line)
            site = sites.get(key)
            if site is None:
                if len(sites) >= _MAX_SITES:
                    truncated = True
                    entry, frames, in_site = None, [], False
                    return
                site = sites[key] = _Site(name, file, line)
            if kind == "calls":
                site.calls = max(site.calls, int(match.group(1)))
                site.peak = max(site.peak or 0, parse_size(match.group(2), match.group(3)))
            elif kind == "peak":
                site.peak = max(site.peak or 0, parse_size(match.group(1), match.group(2)))
                site.calls = max(site.calls, int(match.group(3)))
            elif kind == "leaks":
                site.leaked = (site.leaked or 0) + parse_size(match.group(1), match.group(2))
                site.calls = max(site.calls, int(match.group(3)))
            elif kind == "temporary":
                site.temporary = max(site.temporary or 0, int(match.group(1)))
                site.calls = max(site.calls, int(match.group(2)))
            elif kind == "allocated":
                site.allocated = max(site.allocated or 0, parse_size(match.group(1), match.group(2)))
                site.calls = max(site.calls, int(match.group(3)))
        entry, frames, in_site = None, [], False

    for count, line in enumerate(iter_bounded_lines(text, limits.max_line_chars)):
        if count >= limits.max_lines or not budget.tick():
            truncated = True
            break
        if line is None:
            oversize += 1
            continue
        stripped = line.rstrip()
        if stripped in _SECTIONS:
            flush()
            section = _SECTIONS[stripped]
            seen_marker = True
            continue
        debuggee = _DEBUGGEE_RE.match(stripped)
        if debuggee:
            process = debuggee.group(1)
            seen_marker = True
            continue
        footer_hit = False
        for key, pattern in _FOOTER.items():
            match = pattern.match(stripped)
            if match:
                flush()
                section = None
                footer_hit = seen_marker = True
                if key in ("peak", "rss", "leaked"):
                    footer[key] = parse_size(match.group(1), match.group(2))
                elif key == "runtime":
                    footer[key] = finite_number(match.group(1)) or 0.0
                else:
                    footer[key] = int(match.group(1))
                break
        if footer_hit:
            continue
        if not stripped:
            flush()
            continue
        if section is None:
            continue
        match = _ENTRY[section].match(stripped)
        if match:
            flush()
            entry = (section, match)
            in_site = True
            continue
        if not in_site or entry is None:
            continue
        if line.startswith("    ") or stripped.endswith("from:"):
            in_site = False  # merged caller paths follow; the site block is done
            continue
        at = _AT_RE.match(stripped)
        if at and frames:
            name, _file, _line, module = frames[-1]
            line_no = int(at.group(2))
            frames[-1] = (name, clip_name(at.group(1)), line_no, module)
            continue
        found_in = _IN_RE.match(stripped)
        if found_in and frames:
            name, file, line_no, _module = frames[-1]
            frames[-1] = (name, file, line_no, clip_name(found_in.group(1)))
            continue
        if not line.startswith(" ") and len(frames) < 64:
            frames.append((clip_name(stripped), None, None, None))
    flush()
    if not seen_marker:
        raise ProfileFormatUnknown("not heaptrack_print output",
                                   hint="run heaptrack_print on the capture first")
    if not sites and not footer:
        raise ProfileParseError("heaptrack_print output has no allocation entries")
    ordered = sorted(
        sites.values(),
        key=lambda s: (-(s.leaked or 0), -(s.peak or 0), -s.calls, s.function),
    )
    by_calls = sorted(sites.values(), key=lambda s: (-s.calls, s.function))
    chosen: list[_Site] = []
    # Keep the heaviest leak/peak sites and the busiest allocator sites both in view.
    for site in ordered[: MAX_ALLOCATIONS - 5] + by_calls[:5] + ordered[MAX_ALLOCATIONS - 5:]:
        if site not in chosen:
            chosen.append(site)
    allocations = tuple(
        AllocationHotspot(
            function=site.function, bytes_total=site.allocated or 0, allocations=site.calls,
            peak_bytes=site.peak, leaked_bytes=site.leaked, file=site.file, line=site.line,
            temporary=site.temporary,
        )
        for site in chosen[:MAX_ALLOCATIONS]
    )
    notes = []
    if "peak" in footer:
        notes.append("peak heap memory consumption: %d bytes" % footer["peak"])
    if "leaked" in footer:
        notes.append("total memory leaked: %d bytes" % footer["leaked"])
    if "temporary" in footer:
        notes.append("temporary allocations: %d" % footer["temporary"])
    if oversize:
        notes.append("%d oversize lines skipped" % oversize)
    runtime = footer.get("runtime")
    return ProfileDigest(
        source_kind=ProfileSourceKind.HEAPTRACK_TEXT.value,
        metric="heap",
        unit="bytes",
        metadata=CaptureMetadata(
            tool="heaptrack", event="allocations",
            sample_count=int(footer["calls"]) if "calls" in footer else None,
            duration_ns=int(runtime * 1e9) if isinstance(runtime, float) else None,
            process=program_name(process),
        ),
        allocations=allocations,
        notes=tuple(notes),
        truncated=truncated,
    )
