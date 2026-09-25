"""Streaming callgrind.out reader (valgrind --tool=callgrind).

Understands the parts of the format a digest needs:

- headers ``positions:`` (``line``, ``instr``, ``instr line``), ``events:``,
  ``summary:``/``totals:``, ``cmd:``, ``creator:``, ``pid:``;
- ``ob=``/``fl=``/``fi=``/``fe=``/``fn=`` and the call specs
  ``cob=``/``cfi=``/``cfl=``/``cfn=``/``calls=``; ``jump=``/``jcnd=`` are skipped;
- name compression ``(id) name`` / ``(id)``, with separate id spaces for
  objects, files and functions (undefined ids are ignored and noted);
- cost lines with relative subpositions (``+N``, ``-N``, ``*``) and hex or
  decimal positions.

Self cost is every cost line not preceded by ``calls=``; the line after
``calls=`` is the inclusive cost of that call. A function's total is its self
cost plus its outgoing calls to OTHER functions (recursion levels ``'N`` are
merged, so a recursive call never double-counts). Bounded by line and function
caps and a wall-clock budget.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable

from sonder_runtime.domain.profiling.folded import ProjectPredicate, heaviest_paths
from sonder_runtime.domain.profiling.model import (
    DEFAULT_LIMITS,
    MAX_HOT_PATHS,
    MAX_TOP_FUNCTIONS,
    TRUNCATED_FRAME,
    CaptureMetadata,
    ProfileDigest,
    ProfileFormatUnknown,
    ProfileFunction,
    ProfileLimits,
    ProfileParseError,
    ProfileSourceKind,
    WorkBudget,
    clip_name,
    percent,
)

_NAME_RE = re.compile(r"^\((\d{1,12})\)(?:\s(.*))?$")
_RECURSION_SUFFIX_RE = re.compile(r"'\d{1,6}$")
_CREATOR_VERSION_RE = re.compile(r"-(\d[\w.]{0,32})$")
_MAX_NUMBER_CHARS = 40

_FILE_KEYS = frozenset({"fl", "fi", "fe", "cfi", "cfl"})
_FN_KEYS = frozenset({"fn", "cfn"})
_OBJ_KEYS = frozenset({"ob", "cob"})


@dataclass(frozen=True, slots=True)
class CallgrindLimits:
    max_lines: int = DEFAULT_LIMITS.max_lines
    max_functions: int = DEFAULT_LIMITS.max_functions
    max_line_chars: int = DEFAULT_LIMITS.max_line_chars
    max_seconds: float = DEFAULT_LIMITS.max_seconds

    @classmethod
    def from_profile_limits(cls, limits: ProfileLimits) -> "CallgrindLimits":
        return cls(max_lines=limits.max_lines, max_functions=limits.max_functions,
                   max_line_chars=limits.max_line_chars, max_seconds=limits.max_seconds)


class _Names:
    """One compressed-name id space."""

    __slots__ = ("table", "undefined", "cap")

    def __init__(self, cap: int) -> None:
        self.table: dict[str, str] = {}
        self.undefined = 0
        self.cap = cap

    def resolve(self, value: str) -> str | None:
        value = value.strip()
        match = _NAME_RE.match(value)
        if not match:
            return clip_name(value) if value else None
        ident, name = match.group(1), match.group(2)
        if name is not None and name.strip():
            name = clip_name(name)
            if ident in self.table or len(self.table) < self.cap:
                self.table[ident] = name
            return name
        known = self.table.get(ident)
        if known is None:
            self.undefined += 1
        return known


def _parse_int(token: str) -> int | None:
    if len(token) > _MAX_NUMBER_CHARS:
        return None
    try:
        return int(token, 16) if token[:2] in ("0x", "0X") else int(token)
    except ValueError:
        return None


class _Function:
    __slots__ = ("name", "obj", "file", "line", "self_cost", "calls_in", "out")

    def __init__(self, name: str, obj: str | None, file: str | None) -> None:
        self.name = name
        self.obj = obj
        self.file = file
        self.line: int | None = None
        self.self_cost = 0
        self.calls_in = 0
        self.out: dict[tuple, int] = {}


def parse_callgrind(
    lines: Iterable[str],
    limits: CallgrindLimits | ProfileLimits = CallgrindLimits(),
    *,
    event: str | None = None,
    top_n: int = MAX_TOP_FUNCTIONS,
    path_n: int = MAX_HOT_PATHS,
    project: ProjectPredicate | None = None,
    clock: Callable[[], float] | None = None,
) -> ProfileDigest:
    """Digest a callgrind.out file given as an iterator of text lines."""
    if isinstance(limits, ProfileLimits):
        limits = CallgrindLimits.from_profile_limits(limits)
    budget = WorkBudget(limits.max_seconds, clock)
    files = _Names(limits.max_functions)
    fns = _Names(limits.max_functions)
    objs = _Names(limits.max_functions)
    functions: dict[tuple, _Function] = {}
    headers: dict[str, str] = {}
    events: list[str] = []
    positions = ["line"]
    event_index = 0
    last_pos: list[int] = []
    current_obj: str | None = None
    current_file: str | None = None
    current_fn: _Function | None = None
    call_obj: str | None = None
    call_file: str | None = None
    call_fn: str | None = None
    pending_calls: int | None = None
    skip_next_cost = False
    seen_format = False
    malformed = oversize = 0
    truncated = False
    line_count = 0

    def function_for(name: str, obj: str | None, file: str | None) -> _Function:
        nonlocal truncated
        name = _RECURSION_SUFFIX_RE.sub("", name) or name
        key = (name, obj, file)
        found = functions.get(key)
        if found is None:
            if len(functions) >= limits.max_functions:
                truncated = True
                key = (TRUNCATED_FRAME, None, None)
                found = functions.get(key)
                if found is None:
                    found = functions[key] = _Function(TRUNCATED_FRAME, None, None)
                return found
            found = functions[key] = _Function(name, obj, file)
        return found

    for raw in lines:
        line_count += 1
        if line_count > limits.max_lines or not budget.tick():
            truncated = True
            break
        if len(raw) > limits.max_line_chars:
            oversize += 1
            continue
        line = raw.rstrip("\r\n")
        if not line or line.startswith("#"):
            if line.startswith("# callgrind format"):
                seen_format = True
            continue
        first = line[0]
        if first.isdigit() or first in "+-*" or line.startswith("0x"):
            if not events:
                raise ProfileFormatUnknown("callgrind cost line before an 'events:' header")
            tokens = line.split()
            npos = len(positions)
            if len(tokens) < npos:
                malformed += 1
                continue
            new_pos: list[int] = []
            ok = True
            for index in range(npos):
                token = tokens[index]
                previous = last_pos[index] if index < len(last_pos) else 0
                if token == "*":
                    new_pos.append(previous)
                elif token[0] in "+-":
                    delta = _parse_int(token[1:])
                    if delta is None:
                        ok = False
                        break
                    new_pos.append(previous + delta if token[0] == "+" else previous - delta)
                else:
                    value = _parse_int(token)
                    if value is None:
                        ok = False
                        break
                    new_pos.append(value)
            if not ok:
                malformed += 1
                continue
            last_pos = new_pos
            cost = 0
            if event_index + npos < len(tokens):
                value = _parse_int(tokens[event_index + npos])
                if value is None or value < 0:
                    malformed += 1
                    continue
                cost = value
            if skip_next_cost:
                skip_next_cost = False
                continue
            if current_fn is None:
                malformed += 1
                pending_calls = None
                continue
            if pending_calls is not None:
                callee_name = call_fn
                if callee_name is not None:
                    callee = function_for(callee_name, call_obj or current_obj,
                                          call_file or current_file)
                    callee.calls_in += pending_calls
                    if callee is not current_fn:
                        edge = (callee.name, callee.obj, callee.file)
                        current_fn.out[edge] = current_fn.out.get(edge, 0) + cost
                pending_calls = None
                call_obj = call_file = call_fn = None
                continue
            current_fn.self_cost += cost
            if current_fn.line is None and "line" in positions:
                current_fn.line = new_pos[positions.index("line")]
            continue
        key, sep, value = line.partition("=")
        if sep and key in _FILE_KEYS | _FN_KEYS | _OBJ_KEYS | {"calls", "jump", "jcnd"}:
            if key == "ob":
                current_obj = objs.resolve(value)
            elif key == "cob":
                call_obj = objs.resolve(value)
            elif key == "fl":
                current_file = files.resolve(value)
            elif key in ("fi", "fe"):
                files.resolve(value)  # inlined file: defines ids, not the function
            elif key in ("cfi", "cfl"):
                call_file = files.resolve(value)
            elif key == "fn":
                name = fns.resolve(value)
                current_fn = function_for(name, current_obj, current_file) if name else None
                call_obj = call_file = call_fn = None
                pending_calls = None
            elif key == "cfn":
                call_fn = fns.resolve(value)
            elif key == "calls":
                count = _parse_int(value.split()[0]) if value.split() else None
                pending_calls = count if count is not None and count >= 0 else 0
            else:  # jump / jcnd: the following cost line is a jump source, not cost
                skip_next_cost = True
            continue
        name, colon, rest = line.partition(":")
        if colon and name and " " not in name:
            name = name.strip()
            rest = rest.strip()
            if name == "events":
                events = rest.split()[:64]
                wanted = event or ("Ir" if "Ir" in events else (events[0] if events else ""))
                event_index = events.index(wanted) if wanted in events else 0
            elif name == "positions":
                parsed = [item for item in rest.split() if item in ("line", "instr")]
                positions = parsed or ["line"]
            elif name in ("summary", "totals", "cmd", "creator", "pid", "version", "part"):
                headers.setdefault(name, rest[:1024])
                if name in ("creator", "version"):
                    seen_format = True
            continue
        malformed += 1

    if not events:
        raise ProfileFormatUnknown("not a callgrind.out file (no 'events:' header)",
                                   hint="expected valgrind --tool=callgrind output")
    if not functions:
        if not seen_format:
            raise ProfileFormatUnknown("not a callgrind.out file")
        raise ProfileParseError("callgrind file has no function costs")
    return _digest(functions, headers, events, event_index, files, fns, objs,
                   top_n=top_n, path_n=path_n, project=project,
                   truncated=truncated, malformed=malformed, oversize=oversize)


def _summary_total(headers: dict[str, str], event_index: int) -> int | None:
    for key in ("summary", "totals"):
        tokens = headers.get(key, "").split()
        if event_index < len(tokens):
            value = _parse_int(tokens[event_index])
            if value is not None and value > 0:
                return value
    return None


def _digest(functions, headers, events, event_index, files, fns, objs, *, top_n, path_n,
            project, truncated, malformed, oversize) -> ProfileDigest:
    top_n = max(1, min(int(top_n), MAX_TOP_FUNCTIONS))
    self_sum = sum(fn.self_cost for fn in functions.values())
    total = _summary_total(headers, event_index) or self_sum
    total = max(total, 1)
    inclusive: dict[tuple, int] = {}
    for key, fn in functions.items():
        inclusive[key] = min(total, fn.self_cost + sum(fn.out.values()))

    def function(key: tuple) -> ProfileFunction:
        fn = functions[key]
        module = fn.obj
        return ProfileFunction(
            name=fn.name, module=module, file=fn.file, line=fn.line,
            self_pct=percent(fn.self_cost, total), total_pct=percent(inclusive[key], total),
            self_value=fn.self_cost, total_value=inclusive[key], calls=fn.calls_in or None,
            in_project=bool(project(fn.name, module, fn.file)) if project is not None else False,
        )

    by_self = sorted((k for k in functions if functions[k].self_cost > 0),
                     key=lambda k: (-functions[k].self_cost, k[0]))
    by_total = sorted(functions, key=lambda k: (-inclusive[k], k[0]))

    called = {edge for fn in functions.values() for edge in fn.out}
    roots = [(k, inclusive[k]) for k in functions if k not in called]
    roots.sort(key=lambda item: -item[1])

    def expand(key):
        return sorted(functions[key].out.items(), key=lambda item: -item[1]) if key in functions else []

    hot = heaviest_paths(roots[:32], expand,
                         lambda key: functions[key].self_cost if key in functions else 0,
                         lambda key: key[0], total=total, path_n=path_n) if path_n else ()
    creator = headers.get("creator", "")
    version = _CREATOR_VERSION_RE.search(creator)
    notes = []
    if files.undefined or fns.undefined or objs.undefined:
        notes.append("ignored %d references to undefined compressed ids"
                     % (files.undefined + fns.undefined + objs.undefined))
    if malformed:
        notes.append("%d malformed lines skipped" % malformed)
    if oversize:
        notes.append("%d oversize lines skipped" % oversize)
    if truncated:
        notes.append("input clipped by the line, function or time budget")
    if self_sum and abs(self_sum - total) > total * 0.01:
        notes.append("self costs sum to %d but the header total is %d" % (self_sum, total))
    return ProfileDigest(
        source_kind=ProfileSourceKind.CALLGRIND.value,
        metric=events[event_index] if events else "",
        unit="events",
        metadata=CaptureMetadata(
            tool="callgrind", tool_version=version.group(1) if version else None,
            event=events[event_index] if events else "", process=headers.get("cmd") or None,
            sample_count=None, threads=None, duration_ns=None,
        ),
        top_self=tuple(function(k) for k in by_self[:top_n]),
        top_total=tuple(function(k) for k in by_total[:top_n]),
        hot_paths=hot,
        notes=tuple(notes),
        truncated=truncated,
    )
