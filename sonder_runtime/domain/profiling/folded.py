"""Folded stacks: the common intermediate form for sampled and zone profiles.

A ``FoldedProfile`` is a bounded map from a root-to-leaf stack tuple to a
weight (samples, nanoseconds, instructions). ``digest_folded`` turns it into a
``ProfileDigest``:

- self value: weight of stacks whose leaf is the function;
- total value: weight of stacks containing the function, counted once per
  stack, so recursion never double-counts;
- hot paths: heaviest root-to-leaf chains, merged at the longest common prefix
  where the weight stops concentrating in one child.
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Callable, Hashable, Iterable, Sequence

from sonder_runtime.domain.profiling.frames import detect_spikes, frame_stats
from sonder_runtime.domain.profiling.model import (
    DEFAULT_LIMITS,
    MAX_HOT_PATHS,
    MAX_TOP_FUNCTIONS,
    TRUNCATED_FRAME,
    CaptureMetadata,
    FrameStats,
    HotPath,
    ProfileDigest,
    ProfileFormatUnknown,
    ProfileFunction,
    ProfileLimits,
    ProfileParseError,
    Spike,
    WorkBudget,
    clip_name,
    iter_bounded_lines,
    percent,
)


# (name, module, file) -> in project. Injected by the adapter that knows the
# project roots; the domain never looks at the filesystem.
ProjectPredicate = Callable[[str, "str | None", "str | None"], bool]

# Stacks deeper than max_depth keep this many root frames, a marker, and the
# leaf-side remainder (where self time lives).
_ROOT_KEEP = 16
# Hot paths are computed over at most this many of the heaviest stacks.
_HOT_PATH_STACKS = 5000
_MAX_PATH_POPS = 20_000
_MAX_PATH_DEPTH = 64


@dataclass(frozen=True, slots=True)
class FrameInfo:
    module: str | None = None
    file: str | None = None
    line: int | None = None


class FoldedProfile:
    """Bounded stack -> weight map. Overflow collapses into ``[truncated]``."""

    __slots__ = ("max_stacks", "max_depth", "max_functions", "stacks", "total",
                 "truncated", "depth_clipped", "overflow_value", "frame_info", "notes")

    def __init__(self, *, max_stacks: int = DEFAULT_LIMITS.max_stacks,
                 max_depth: int = DEFAULT_LIMITS.max_depth,
                 max_functions: int = DEFAULT_LIMITS.max_functions) -> None:
        self.max_stacks = max(1, int(max_stacks))
        self.max_depth = max(2, int(max_depth))
        self.max_functions = max(1, int(max_functions))
        self.stacks: dict[tuple[str, ...], int] = {}
        self.total = 0
        self.truncated = False
        self.depth_clipped = 0
        self.overflow_value = 0
        self.frame_info: dict[str, FrameInfo] = {}
        self.notes: list[str] = []

    @classmethod
    def from_limits(cls, limits: ProfileLimits) -> "FoldedProfile":
        return cls(max_stacks=limits.max_stacks, max_depth=limits.max_depth,
                   max_functions=limits.max_functions)

    def note(self, text: str) -> None:
        if text not in self.notes and len(self.notes) < 16:
            self.notes.append(text)


def fold_add(folded: FoldedProfile, stack: Sequence[str], value: int = 1,
             *, info: dict[str, FrameInfo] | None = None) -> None:
    """Add ``value`` to ``stack`` (root first). Non-positive values are ignored."""
    try:
        weight = int(value)
    except (TypeError, ValueError, OverflowError):
        return
    if weight <= 0:
        return
    depth = len(stack)
    if depth > folded.max_depth:
        # Only the kept head and tail are touched, so a hostile 2M-deep stack
        # costs O(max_depth) here, not O(depth).
        tail = folded.max_depth - _ROOT_KEEP - 1
        frames = (tuple(clip_name(frame) for frame in stack[:_ROOT_KEEP]) + (TRUNCATED_FRAME,)
                  + tuple(clip_name(frame) for frame in stack[depth - tail:]))
        folded.depth_clipped += 1
        folded.truncated = True
    else:
        frames = tuple(clip_name(frame) for frame in stack)
    if not frames:
        frames = ("?",)
    folded.total += weight
    if frames in folded.stacks:
        folded.stacks[frames] += weight
    elif len(folded.stacks) < folded.max_stacks:
        folded.stacks[frames] = weight
    else:
        folded.truncated = True
        folded.overflow_value += weight
        key = (TRUNCATED_FRAME,)
        folded.stacks[key] = folded.stacks.get(key, 0) + weight
    if info:
        for name, detail in info.items():
            if name in folded.frame_info or len(folded.frame_info) >= folded.max_functions:
                continue
            folded.frame_info[clip_name(name)] = detail


def parse_folded_text(text: str, *, limits: ProfileLimits = DEFAULT_LIMITS,
                      clock: Callable[[], float] | None = None) -> FoldedProfile:
    """Brendan-Gregg folded lines: ``root;child;leaf 123``.

    Raises ``ProfileFormatUnknown`` when no line has that shape.
    """
    folded = FoldedProfile.from_limits(limits)
    budget = WorkBudget(limits.max_seconds, clock)
    good = bad = oversize = 0
    for count, line in enumerate(iter_bounded_lines(text, limits.max_line_chars)):
        if count >= limits.max_lines or not budget.tick():
            folded.truncated = True
            folded.note("input clipped at %d lines" % count)
            break
        if line is None:
            oversize += 1
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        stack_text, sep, number = stripped.rpartition(" ")
        if not sep or not number.isdigit() or len(number) > 18 or not stack_text:
            bad += 1
            continue
        fold_add(folded, stack_text.split(";"), int(number))
        good += 1
    if not good:
        raise ProfileFormatUnknown("no folded stack lines ('a;b;c 123') found")
    if oversize:
        folded.note("%d oversize lines skipped" % oversize)
    if bad:
        folded.note("%d malformed lines skipped" % bad)
    return folded


def fold_zones(folded: FoldedProfile, zones: Iterable[tuple[int, int, str]],
               *, budget: WorkBudget | None = None) -> int:
    """Fold timed zones of ONE thread into self-time stacks.

    ``zones`` are ``(start_ns, duration_ns, name)``. They are sorted by start
    (longest first on ties) and nested by containment; a child that overruns
    its parent is clipped to the parent's end. Returns the zone count folded.
    Iterative, so hostile nesting depth cannot recurse.
    """
    ordered = sorted(((int(s), max(0, int(d)), n) for s, d, n in zones),
                     key=lambda item: (item[0], -item[1]))
    # stack entries: [end, name, child_ns, duration]
    stack: list[list] = []
    names: list[str] = []
    folded_count = 0
    stopped = False

    def within_budget() -> bool:
        nonlocal stopped
        if not stopped and budget is not None and not budget.tick():
            stopped = True
            folded.truncated = True
            folded.note("zone folding stopped at the time budget")
        return not stopped

    def close() -> None:
        end, name, child_ns, duration = stack.pop()
        # Each close folds a stack of up to max_depth frames, so closes are
        # budgeted too: a hostile 2M-deep nesting would otherwise run
        # O(zones * max_depth) after the last budget check.
        if within_budget():
            fold_add(folded, names, max(0, duration - child_ns))
        names.pop()

    for start, duration, name in ordered:
        if not within_budget():
            break
        while stack and stack[-1][0] <= start:
            close()
        end = start + duration
        if stack and end > stack[-1][0]:
            end = stack[-1][0]
            duration = max(0, end - start)
        if stack:
            stack[-1][2] += duration
        stack.append([end, name, 0, duration])
        names.append(name)
        folded_count += 1
    while stack:
        close()
    return folded_count


# --------------------------------------------------------------------------
# Hot paths


def heaviest_paths(
    roots: Sequence[tuple[Hashable, int]],
    expand: Callable[[Hashable], Sequence[tuple[Hashable, int]]],
    self_value: Callable[[Hashable], int],
    label: Callable[[Hashable], str],
    *,
    total: int,
    path_n: int = MAX_HOT_PATHS,
    min_share: float = 0.01,
    branch_share: float = 0.10,
) -> tuple[HotPath, ...]:
    """Best-first descent over a weighted tree or call graph.

    A chain keeps descending into every child that holds at least
    ``branch_share`` of the chain's weight (and ``min_share`` of the total). It
    ends where no child qualifies: that is the longest common prefix of the
    stacks below it. A node whose own self weight is at least half of the
    chain's weight is also reported. Paths are ordered heaviest first; cycles
    and depth are bounded, so hostile graphs terminate.
    """
    if total <= 0:
        return ()
    floor = max(1, int(total * min_share))
    heap: list[tuple[int, int, tuple, tuple]] = []
    counter = 0
    for key, value in roots:
        if value >= floor:
            heap.append((-int(value), counter, (key,), (label(key),)))
            counter += 1
    heapq.heapify(heap)
    found: list[HotPath] = []
    seen: set[tuple] = set()
    pops = 0
    while heap and len(found) < path_n and pops < _MAX_PATH_POPS:
        pops += 1
        neg, _, keys, labels = heapq.heappop(heap)
        value = -neg
        node = keys[-1]
        children = []
        if len(keys) < _MAX_PATH_DEPTH:
            for child, child_value in expand(node):
                if child in keys:
                    continue
                weight = min(int(child_value), value)
                if weight >= floor and weight >= value * branch_share:
                    children.append((child, weight))
        own = self_value(node)
        if (not children or own * 2 >= value) and labels not in seen:
            seen.add(labels)
            found.append(HotPath(frames=labels, pct=percent(value, total), value=value))
        for child, weight in children:
            heapq.heappush(heap, (-weight, counter, keys + (child,), labels + (label(child),)))
            counter += 1
    found.sort(key=lambda path: (-path.value, path.frames))
    return tuple(found[:path_n])


class _Trie:
    __slots__ = ("names", "values", "selfs", "children")

    def __init__(self) -> None:
        self.names: list[str] = [""]
        self.values: list[int] = [0]
        self.selfs: list[int] = [0]
        self.children: list[dict[str, int]] = [{}]

    def add(self, stack: tuple[str, ...], weight: int) -> None:
        node = 0
        self.values[0] += weight
        for name in stack:
            nxt = self.children[node].get(name)
            if nxt is None:
                nxt = len(self.names)
                self.names.append(name)
                self.values.append(0)
                self.selfs.append(0)
                self.children.append({})
                self.children[node][name] = nxt
            self.values[nxt] += weight
            node = nxt
        self.selfs[node] += weight


def folded_hot_paths(folded: FoldedProfile, *, path_n: int = MAX_HOT_PATHS) -> tuple[HotPath, ...]:
    heaviest = heapq.nlargest(_HOT_PATH_STACKS, folded.stacks.items(), key=lambda item: item[1])
    trie = _Trie()
    for stack, weight in heaviest:
        trie.add(stack, weight)

    def expand(node):
        return [(child, trie.values[child]) for child in trie.children[node].values()]

    roots = [(child, trie.values[child]) for child in trie.children[0].values()]
    return heaviest_paths(
        roots, expand, lambda node: trie.selfs[node], lambda node: trie.names[node],
        total=folded.total, path_n=path_n,
    )


# --------------------------------------------------------------------------
# Digest


def _function(name: str, folded: FoldedProfile, self_value: int, total_value: int,
              project: ProjectPredicate | None) -> ProfileFunction:
    info = folded.frame_info.get(name) or FrameInfo()
    in_project = bool(project(name, info.module, info.file)) if project is not None else False
    return ProfileFunction(
        name=name, module=info.module, file=info.file, line=info.line,
        self_pct=percent(self_value, folded.total), total_pct=percent(total_value, folded.total),
        self_value=self_value, total_value=total_value, calls=None, in_project=in_project,
    )


def function_values(folded: FoldedProfile) -> tuple[dict[str, int], dict[str, int]]:
    """(self, total) per function; total counts each function once per stack."""
    self_values: dict[str, int] = {}
    total_values: dict[str, int] = {}
    for stack, weight in folded.stacks.items():
        leaf = stack[-1]
        self_values[leaf] = self_values.get(leaf, 0) + weight
        for name in set(stack):
            total_values[name] = total_values.get(name, 0) + weight
    return self_values, total_values


def digest_folded(
    folded: FoldedProfile,
    *,
    metric: str,
    unit: str,
    source_kind: str,
    metadata: CaptureMetadata | None = None,
    top_n: int = MAX_TOP_FUNCTIONS,
    path_n: int = MAX_HOT_PATHS,
    project: ProjectPredicate | None = None,
    spikes: tuple[Spike, ...] = (),
    frames: FrameStats | None = None,
    notes: Iterable[str] = (),
    truncated: bool = False,
    engines: tuple[str, ...] = ("pure",),
) -> ProfileDigest:
    top_n = max(1, min(int(top_n), MAX_TOP_FUNCTIONS))
    path_n = max(0, min(int(path_n), MAX_HOT_PATHS))
    self_values, total_values = function_values(folded)
    self_values.pop(TRUNCATED_FRAME, None)
    total_values.pop(TRUNCATED_FRAME, None)
    top_self_names = heapq.nsmallest(top_n, self_values, key=lambda n: (-self_values[n], n))
    top_total_names = heapq.nsmallest(top_n, total_values, key=lambda n: (-total_values[n], n))
    top_self = tuple(_function(n, folded, self_values[n], total_values.get(n, 0), project)
                     for n in top_self_names)
    top_total = tuple(_function(n, folded, self_values.get(n, 0), total_values[n], project)
                      for n in top_total_names)
    all_notes = list(notes) + list(folded.notes)
    if folded.depth_clipped:
        all_notes.append("%d stacks deeper than %d frames were collapsed"
                         % (folded.depth_clipped, folded.max_depth))
    if folded.overflow_value:
        all_notes.append("%.1f%% of the weight fell past %d unique stacks into [truncated]"
                         % (percent(folded.overflow_value, folded.total), folded.max_stacks))
    return ProfileDigest(
        source_kind=source_kind,
        engines=engines,
        metric=metric,
        unit=unit,
        metadata=metadata or CaptureMetadata(),
        top_self=top_self,
        top_total=top_total,
        hot_paths=folded_hot_paths(folded, path_n=path_n) if path_n else (),
        spikes=spikes,
        frames=frames,
        notes=tuple(all_notes),
        truncated=bool(truncated or folded.truncated),
    )


# --------------------------------------------------------------------------
# Timed zones (Chrome X/B/E, Tracy unwrap, PIX timing) to a digest


DEFAULT_FRAME_NAMES = frozenset({"Frame", "FrameMark", "GameFrame"})


def digest_zones(
    zones_by_thread: dict[str, list[tuple[int, int, str]]],
    *,
    source_kind: str,
    metadata: CaptureMetadata,
    thread_names: dict[str, str] | None = None,
    frame_names: Iterable[str] = DEFAULT_FRAME_NAMES,
    frame_zone: str = "",
    frame_marks: Sequence[int] = (),
    frame_budget_ms: float | None = None,
    thread: str = "",
    top_n: int = MAX_TOP_FUNCTIONS,
    path_n: int = MAX_HOT_PATHS,
    project: ProjectPredicate | None = None,
    limits: ProfileLimits = DEFAULT_LIMITS,
    budget: WorkBudget | None = None,
    notes: Iterable[str] = (),
    truncated: bool = False,
    engines: tuple[str, ...] = ("pure",),
) -> ProfileDigest:
    """Zones per thread -> self/total by nesting, hot paths, frame stats and spikes.

    Frames come from zones named ``frame_zone`` (when given) or any of
    ``frame_names``, on the thread with the most such zones; failing that, from
    the intervals between ``frame_marks`` instants (nanoseconds).
    """
    names = thread_names or {}
    notes = list(notes)
    wanted = thread.strip()
    selected = {
        key: zones for key, zones in zones_by_thread.items()
        if not wanted or wanted in (key, key.rsplit(":", 1)[-1], names.get(key, ""))
    }
    if wanted and not selected:
        notes.append("no thread named %r; all threads used" % wanted[:64])
        selected = dict(zones_by_thread)
    folded = FoldedProfile.from_limits(limits)
    frame_set = {frame_zone} if frame_zone else set(frame_names)
    best_thread = ""
    best_frames: list[tuple[int, int]] = []
    for key in sorted(selected):
        zones = selected[key]
        fold_zones(folded, zones, budget=budget)
        frames = sorted((start, duration) for start, duration, name in zones if name in frame_set)
        if len(frames) > len(best_frames):
            best_thread, best_frames = key, frames
    durations: list[int] = []
    starts: list[int] = []
    if best_frames:
        starts = [start for start, _ in best_frames]
        durations = [duration for _, duration in best_frames]
    elif len(frame_marks) >= 2:
        marks = sorted(frame_marks)
        starts = marks[:-1]
        durations = [later - earlier for earlier, later in zip(marks, marks[1:])]
        best_thread = ""
    elif frame_zone:
        notes.append("no zones named %r for frame statistics" % frame_zone[:64])
    stats = frame_stats(durations, frame_budget_ms) if durations else None
    spikes: tuple[Spike, ...] = ()
    if durations:
        spikes = detect_spikes(durations, starts_ns=starts, label=frame_zone or "frame",
                               thread=names.get(best_thread, best_thread) or None)
    if not folded.stacks and not durations:
        if budget is not None and budget.exceeded:
            raise ProfileParseError("time budget ran out before any zone was folded")
        raise ProfileParseError("no timed zones or frame markers found")
    return digest_folded(
        folded, metric="wall_time", unit="ns", source_kind=source_kind, metadata=metadata,
        top_n=top_n, path_n=path_n, project=project, spikes=spikes, frames=stats,
        notes=notes, truncated=truncated, engines=engines,
    )
