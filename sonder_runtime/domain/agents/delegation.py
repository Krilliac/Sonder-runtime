"""Pure domain contracts for depth-1 agent delegation, peer board coordination, and personas.

This module defines the immutable contracts shared by depth-0 delegation
entry points, depth-1 child guards, shared progress/peer boards, persona
hints, and SSE streaming frames.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

MAX_DELEGATION_DEPTH = 1
MAX_DELEGATED_TASKS = 8
DEFAULT_CHILD_MAX_STEPS = 8
MAX_CHILD_MAX_STEPS = 16
DEFAULT_SUMMARY_CHARS = 240
DEFAULT_THINKING_CHARS = 280
DEFAULT_EVENT_BACKLOG = 256

DELEGATION_TOOLS = frozenset({"delegate_task", "spawn_agents"})
PROGRESS_TOOLS = frozenset({"report_progress", "post_status"})
PEER_BOARD_TOOLS = frozenset({"post_to_board", "read_board"})
COORDINATION_TOOLS = PROGRESS_TOOLS | PEER_BOARD_TOOLS

AGENT_PHASES = frozenset({
    "queued",
    "starting",
    "thinking",
    "tool_call",
    "tool_result",
    "progress",
    "completed",
    "failed",
    "refused",
})

BOARD_MESSAGE_KINDS = frozenset({
    "chat",
    "status",
    "handoff",
    "praise",
    "complaint",
    "joke",
    "result",
})

DEFAULT_CHILD_ROSTER: tuple[str, ...] = (
    "coder",
    "scout",
    "security",
    "skeptic",
    "reviewer",
    "builder",
    "analyst",
    "tester",
)

# Role -> (personality label, behavioral voice hint, optional prompts/personas/<file>)
DEFAULT_AGENT_PERSONAS: dict[str, tuple[str, str, str]] = {
    "coordinator": (
        "decisive, direct, witty-under-pressure lead",
        "Keep teammates unblocked, call out sloppy assumptions, acknowledge sharp catches, and synthesize only grounded tool evidence.",
        "",
    ),
    "coder": (
        "pragmatic, fast-iterating craftsperson",
        "Inspect code first, ship minimal targeted edits, verify every change with real tools, and hand concrete diffs or line numbers to peers.",
        "coder.md",
    ),
    "scout": (
        "curious, high-signal pathfinder",
        "Map files, symbols, and call sites quickly, warn peers about surprises before they step on them, and cite exact paths and lines.",
        "",
    ),
    "security": (
        "paranoid, receipts-first boundary auditor",
        "Probe trust boundaries, inputs, permissions, and refusal paths ruthlessly; never trust unverified claims or silent no-op checks.",
        "security.md",
    ),
    "skeptic": (
        "blunt, edge-case-hunting contrarian",
        "Challenge optimistic claims, demand reproduction evidence, poke at failure modes, and give credit only when a check genuinely proves the fix.",
        "reviewer.md",
    ),
    "reviewer": (
        "exacting, edge-case-focused code reviewer",
        "Critique correctness, invariants, and subtle regressions directly; pair every complaint with the exact line and concrete fix.",
        "reviewer.md",
    ),
    "builder": (
        "methodical build-and-test operator",
        "Run real builds/tests, diagnose failures down to the root error, pivot cleanly on dead ends, and report exact counts without padding.",
        "",
    ),
    "analyst": (
        "structured root-cause investigator",
        "Trace data and control flow step by step, compare hypothesis against observed outputs, and summarize clearly.",
        "explainer.md",
    ),
    "tester": (
        "relentless adversarial verifier",
        "Write and run checks that fail on the bug and pass on the fix; flag weak assertions or skipped gates immediately.",
        "",
    ),
}

_THINK_TAG_RE = re.compile(
    r"<(?:think|thought|\|thought\|)>(.*?)</(?:think|thought|\|thought\|)>",
    re.DOTALL | re.IGNORECASE,
)
_THOUGHT_PREFIX_RE = re.compile(r"^\s*(?:THOUGHT|THINKING|PLAN)\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z][A-Za-z0-9_-]{0,31})")


def compact_text(value: object, *, limit: int = DEFAULT_SUMMARY_CHARS) -> str:
    """Collapse internal whitespace and bound ``value`` to ``limit`` characters."""
    text = " ".join(str(value or "").split()).strip()
    if limit <= 0 or len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def extract_thinking_summary(text: object, *, limit: int = DEFAULT_THINKING_CHARS) -> str:
    """Extract a bounded thinking/reasoning excerpt from model output."""
    raw = str(text or "")
    if not raw.strip():
        return ""
    matches = [m.group(1).strip() for m in _THINK_TAG_RE.finditer(raw) if m.group(1).strip()]
    if matches:
        return compact_text(" ".join(matches), limit=limit)
    prefix_matches = [m.group(1).strip() for m in _THOUGHT_PREFIX_RE.finditer(raw) if m.group(1).strip()]
    if prefix_matches:
        return compact_text(" ".join(prefix_matches), limit=limit)
    return ""


def strip_thinking_tags(text: object) -> str:
    """Remove explicit ``<think>`` blocks before extracting visible answer prose."""
    return _THINK_TAG_RE.sub("", str(text or "")).strip()


def extract_mentions(text: object) -> tuple[str, ...]:
    """Return unique ``@handle`` mentions from ``text`` in first-seen order."""
    seen: list[str] = []
    seen_lower: set[str] = set()
    for match in _MENTION_RE.finditer(str(text or "")):
        handle = match.group(1)
        lowered = handle.lower()
        if lowered not in seen_lower:
            seen_lower.add(lowered)
            seen.append(handle)
    return tuple(seen)


def persona_for(
    handle: str,
    *,
    explicit_role: str = "",
    explicit_personality: str = "",
) -> tuple[str, str, str, str]:
    """Resolve ``(role, personality, voice_hint, persona_file)`` for a handle."""
    clean_handle = str(handle or "coder").strip().lower()
    base = re.sub(r"-\d+$", "", clean_handle)
    clean_role = str(explicit_role or "").strip().lower() or (base if base in DEFAULT_AGENT_PERSONAS else "specialist")
    preset = DEFAULT_AGENT_PERSONAS.get(clean_role) or DEFAULT_AGENT_PERSONAS.get(base)
    if preset is not None:
        default_personality, voice, persona_file = preset
    else:
        default_personality = "focused, direct problem-solver"
        voice = "Work methodically through real tools, coordinate with peers via @mentions, and report only verified results."
        persona_file = ""
    personality = str(explicit_personality or "").strip() or default_personality
    return clean_role, personality, voice, persona_file


def build_agent_drive_preamble(
    *,
    handle: str,
    role: str,
    personality: str,
    voice_hint: str = "",
    persona_text: str = "",
    peers: Sequence[str] = (),
    depth: int = 0,
) -> str:
    """Render concise persona, peer-board, relentless-drive, and honesty guidance."""
    lines = [
        f"[AGENT IDENTITY] handle=@{handle} role={role} depth={depth} | personality: {personality}",
    ]
    if voice_hint:
        lines.append(f"Voice: {voice_hint}")
    if persona_text:
        lines.append(f"Role profile: {compact_text(persona_text, limit=360)}")
    if peers:
        lines.append("Active teammates: " + ", ".join(f"@{p}" for p in peers))
    lines.append(
        "Execution drive: Exhaust every valid tool and strategy within your step budget before giving up; "
        "on errors or empty leads, diagnose why and pivot rather than repeating identical failing calls."
    )
    lines.append(
        "Honesty & boundaries: Never invent tool outputs, file reads, paths, or test passes. "
        "Respect all permission/role/project/depth rules."
    )
    if depth >= 1:
        lines.append(
            "Workspace discipline: Stay focused on your assigned files and task; do not re-read or poll "
            "teammates' unrelated files. Peer board updates are auto-injected between steps, so post to "
            "the board only on handoffs, blockers, or completion."
        )
    lines.append(
        "Peer board: Use post_to_board to share discoveries, handoffs (@name), praise, pushback/complaints, "
        "or a brief quip; use read_board to inspect updates from teammates."
    )
    return "\n".join(lines)


def clamp_child_max_steps(value: object, *, default: int = DEFAULT_CHILD_MAX_STEPS) -> int:
    """Clamp a requested child step budget into ``[1, MAX_CHILD_MAX_STEPS]``."""
    if value is None or isinstance(value, bool):
        return max(1, min(MAX_CHILD_MAX_STEPS, int(default)))
    if isinstance(value, (int, float)):
        parsed = int(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return max(1, min(MAX_CHILD_MAX_STEPS, int(default)))
        try:
            parsed = int(stripped)
        except ValueError:
            return max(1, min(MAX_CHILD_MAX_STEPS, int(default)))
    else:
        return max(1, min(MAX_CHILD_MAX_STEPS, int(default)))
    return max(1, min(MAX_CHILD_MAX_STEPS, parsed))


@dataclass(frozen=True)
class DelegationTaskSpec:
    """Normalized specification for a single depth-1 delegated child agent."""

    task: str
    agent_id: str = ""
    tier: str = ""
    model: str = ""
    project: str = ""
    cwd: str = ""
    max_steps: int = DEFAULT_CHILD_MAX_STEPS
    read_only: bool | None = None
    role: str = ""
    personality: str = ""


@dataclass(frozen=True)
class DelegationChildResult:
    """Execution outcome reported by one depth-1 delegated child agent."""

    agent_id: str
    task: str
    status: str
    ok: bool
    summary: str
    output: str = ""
    tier: str = ""
    model: str = ""
    origin: str = ""
    elapsed_ms: int = 0
    steps: int = 0
    error: str = ""
    role: str = ""
    personality: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AgentProgressEvent:
    """Single immutable live event on the per-run agent progress board and SSE stream."""

    run_id: str
    seq: int
    ts: float
    agent_id: str
    parent_id: str
    depth: int
    phase: str
    tier: str = ""
    model: str = ""
    origin: str = ""
    step: int = 0
    max_steps: int = 0
    summary: str = ""
    thinking: str = ""
    tool: str = ""
    ok: bool | None = None
    elapsed_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "seq": self.seq,
            "ts": self.ts,
            "agent_id": self.agent_id,
            "parent_id": self.parent_id,
            "depth": self.depth,
            "phase": self.phase,
            "tier": self.tier,
            "model": self.model,
            "origin": self.origin,
            "step": self.step,
            "max_steps": self.max_steps,
            "summary": self.summary,
            "thinking": self.thinking,
            "tool": self.tool,
            "ok": self.ok,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass(frozen=True)
class PeerBoardMessage:
    """Structured peer-to-peer coordination message on the run board."""

    run_id: str
    seq: int
    ts: float
    agent: str
    role: str
    model: str
    origin: str
    personality: str
    kind: str
    text: str
    mentions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "seq": self.seq,
            "ts": self.ts,
            "agent": self.agent,
            "role": self.role,
            "model": self.model,
            "origin": self.origin,
            "personality": self.personality,
            "kind": self.kind,
            "text": self.text,
            "mentions": list(self.mentions),
        }


def _sanitize_handle(raw: object, fallback: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_-]+", "-", str(raw or "").strip()).strip("-_")
    if not text or not text[0].isalpha():
        return fallback
    return text[:32].lower()


def _coerce_spec_item(
    raw: object,
    *,
    index: int,
    default_tier: str = "",
    default_model: str = "",
    default_project: str = "",
    default_cwd: str = "",
    default_max_steps: int = DEFAULT_CHILD_MAX_STEPS,
    default_read_only: bool | None = None,
) -> DelegationTaskSpec:
    fallback_handle = DEFAULT_CHILD_ROSTER[(index - 1) % len(DEFAULT_CHILD_ROSTER)]
    if isinstance(raw, str):
        task = raw.strip()
        if not task:
            raise ValueError("delegated task must be a non-empty string")
        role, personality, _, _ = persona_for(fallback_handle)
        return DelegationTaskSpec(
            task=task,
            agent_id=fallback_handle,
            tier=default_tier,
            model=default_model,
            project=default_project,
            cwd=default_cwd,
            max_steps=clamp_child_max_steps(default_max_steps),
            read_only=default_read_only,
            role=role,
            personality=personality,
        )
    if not isinstance(raw, Mapping):
        raise TypeError(f"tasks[{index - 1}] must be a string or object")
    task = str(raw.get("task") or raw.get("prompt") or "").strip()
    if not task:
        raise ValueError(f"tasks[{index - 1}].task must be a non-empty string")
    raw_id = str(raw.get("agent_id") or raw.get("id") or raw.get("name") or raw.get("handle") or "").strip()
    agent_id = _sanitize_handle(raw_id, fallback_handle) if raw_id else fallback_handle
    read_only_raw = raw.get("read_only")
    read_only = read_only_raw if isinstance(read_only_raw, bool) else default_read_only
    explicit_role = str(raw.get("role") or "").strip()
    explicit_personality = str(raw.get("personality") or "").strip()
    role, personality, _, _ = persona_for(
        agent_id,
        explicit_role=explicit_role,
        explicit_personality=explicit_personality,
    )
    return DelegationTaskSpec(
        task=task,
        agent_id=agent_id,
        tier=str(raw.get("tier") or default_tier or "").strip(),
        model=str(raw.get("model") or default_model or "").strip(),
        project=str(raw.get("project") or default_project or "").strip(),
        cwd=str(raw.get("cwd") or default_cwd or "").strip(),
        max_steps=clamp_child_max_steps(raw.get("max_steps"), default=default_max_steps),
        read_only=read_only,
        role=role,
        personality=personality,
    )


def normalize_single_task(args: Mapping[str, Any] | None) -> tuple[DelegationTaskSpec, ...]:
    """Validate and normalize ``delegate_task`` tool arguments into a 1-item tuple."""
    data = args if isinstance(args, Mapping) else {}
    task = str(data.get("task") or data.get("prompt") or "").strip()
    if not task:
        raise ValueError("delegate_task requires a non-empty task")
    return (_coerce_spec_item(dict(data, task=task), index=1),)


def normalize_batch_tasks(
    args: Mapping[str, Any] | None,
    *,
    max_tasks: int = MAX_DELEGATED_TASKS,
) -> tuple[DelegationTaskSpec, ...]:
    """Validate and normalize ``spawn_agents`` tool arguments."""
    data = args if isinstance(args, Mapping) else {}
    raw_tasks = data.get("tasks") if "tasks" in data else data.get("tasks_json")
    if isinstance(raw_tasks, str):
        try:
            raw_tasks = json.loads(raw_tasks)
        except ValueError:
            raw_tasks = [line.strip() for line in raw_tasks.splitlines() if line.strip()]
    if not isinstance(raw_tasks, Sequence) or isinstance(raw_tasks, (str, bytes)):
        raise TypeError("spawn_agents requires a non-empty list of tasks")
    items = list(raw_tasks)
    if not items:
        raise ValueError("spawn_agents requires at least one task")
    bound = max(1, min(MAX_DELEGATED_TASKS, int(max_tasks)))
    if len(items) > bound:
        raise ValueError(f"spawn_agents supports at most {bound} parallel tasks per call")
    default_tier = str(data.get("tier") or "").strip()
    default_model = str(data.get("model") or "").strip()
    default_project = str(data.get("project") or "").strip()
    default_cwd = str(data.get("cwd") or "").strip()
    default_max_steps = clamp_child_max_steps(data.get("max_steps"))
    default_ro = data.get("read_only") if isinstance(data.get("read_only"), bool) else None
    seen_ids: set[str] = set()
    specs: list[DelegationTaskSpec] = []
    for idx, item in enumerate(items, start=1):
        spec = _coerce_spec_item(
            item,
            index=idx,
            default_tier=default_tier,
            default_model=default_model,
            default_project=default_project,
            default_cwd=default_cwd,
            default_max_steps=default_max_steps,
            default_read_only=default_ro,
        )
        candidate_id = spec.agent_id
        suffix = 2
        while candidate_id in seen_ids:
            candidate_id = f"{spec.agent_id}-{suffix}"
            suffix += 1
        seen_ids.add(candidate_id)
        if candidate_id != spec.agent_id:
            spec = DelegationTaskSpec(
                task=spec.task,
                agent_id=candidate_id,
                tier=spec.tier,
                model=spec.model,
                project=spec.project,
                cwd=spec.cwd,
                max_steps=spec.max_steps,
                read_only=spec.read_only,
                role=spec.role,
                personality=spec.personality,
            )
        specs.append(spec)
    return tuple(specs)


DEFAULT_REPORT_OUTPUT_BUDGET = 6000
MIN_CHILD_OUTPUT_CHARS = 300


def _fair_share_child_details(
    details: Sequence[str],
    *,
    total_budget: int = DEFAULT_REPORT_OUTPUT_BUDGET,
) -> list[str]:
    """Bound per-child output via water-filling so verbose early children don't starve siblings."""
    items = list(details)
    if not items or total_budget <= 0:
        return items
    if sum(len(text) for text in items) <= total_budget:
        return items
    remaining = int(total_budget)
    caps: dict[int, int] = {}
    ordered_indices = sorted(range(len(items)), key=lambda idx: len(items[idx]))
    for pos, idx in enumerate(ordered_indices):
        left = len(ordered_indices) - pos
        share = max(MIN_CHILD_OUTPUT_CHARS, remaining // max(1, left))
        if len(items[idx]) <= share:
            caps[idx] = len(items[idx])
            remaining = max(0, remaining - len(items[idx]))
        else:
            caps[idx] = share
            remaining = max(0, remaining - share)
    bounded: list[str] = []
    for idx, text in enumerate(items):
        cap = caps.get(idx, len(text))
        if len(text) <= cap:
            bounded.append(text)
        else:
            omitted = len(text) - cap
            bounded.append(f"{text[:cap]}... (truncated {omitted} chars)")
    return bounded


def format_delegation_report(
    results: Sequence[DelegationChildResult],
    *,
    events_summary: Sequence[str] = (),
    board_summary: Sequence[str] = (),
    max_total_output_chars: int = DEFAULT_REPORT_OUTPUT_BUDGET,
) -> str:
    """Format child outcomes and board coordination into a concise grounded observation."""
    rows = list(results)
    succeeded = sum(1 for item in rows if item.ok)
    prefix = "ERROR: " if (rows and succeeded == 0) else ""
    lines = [f"{prefix}delegation completed: {succeeded}/{len(rows)} succeeded"]
    raw_details = [item.output or item.summary or item.error or "(no output)" for item in rows]
    bounded_details = _fair_share_child_details(raw_details, total_budget=max_total_output_chars)
    for item, detail in zip(rows, bounded_details, strict=True):
        meta = [part for part in (item.status, item.role, item.origin or item.tier or item.model) if part]
        meta_label = f" [{', '.join(meta)}]" if meta else ""
        lines.append(f"- @{item.agent_id}{meta_label} ({item.elapsed_ms}ms): {detail}")
    if board_summary:
        lines.append("peer board:")
        lines.extend(f"  {line}" for line in board_summary[-8:])
    if events_summary:
        lines.append("progress log:")
        lines.extend(f"  {line}" for line in events_summary[-8:])
    return "\n".join(lines)


def make_event_timestamp() -> float:
    return round(time.time(), 3)


def render_progress_snapshot(events: Sequence[AgentProgressEvent], *, limit: int = 20) -> str:
    """Render recent progress board events as concise plain-text lines."""
    tail = list(events)[-max(1, int(limit)) :] if limit > 0 else list(events)
    if not tail:
        return "no agent progress events recorded yet"
    lines: list[str] = []
    for ev in tail:
        origin_part = f"@{ev.origin}" if ev.origin else ""
        model_part = f"/{ev.model or ev.tier}{origin_part}" if (ev.model or ev.tier or origin_part) else ""
        step_part = f" step={ev.step}/{ev.max_steps}" if ev.max_steps > 0 else (f" step={ev.step}" if ev.step > 0 else "")
        tool_part = f" tool={ev.tool}" if ev.tool else ""
        detail = compact_text(ev.summary or ev.thinking, limit=180)
        lines.append(f"#{ev.seq} [{ev.agent_id} d={ev.depth} {ev.phase}{model_part}{step_part}{tool_part}] {detail}".rstrip())
    return "\n".join(lines)


__all__ = [
    "AGENT_PHASES",
    "BOARD_MESSAGE_KINDS",
    "COORDINATION_TOOLS",
    "DEFAULT_AGENT_PERSONAS",
    "DEFAULT_CHILD_MAX_STEPS",
    "DEFAULT_CHILD_ROSTER",
    "DEFAULT_EVENT_BACKLOG",
    "DEFAULT_SUMMARY_CHARS",
    "DEFAULT_THINKING_CHARS",
    "DELEGATION_TOOLS",
    "MAX_CHILD_MAX_STEPS",
    "MAX_DELEGATED_TASKS",
    "MAX_DELEGATION_DEPTH",
    "PEER_BOARD_TOOLS",
    "PROGRESS_TOOLS",
    "AgentProgressEvent",
    "DelegationChildResult",
    "DelegationTaskSpec",
    "PeerBoardMessage",
    "build_agent_drive_preamble",
    "clamp_child_max_steps",
    "compact_text",
    "extract_mentions",
    "extract_thinking_summary",
    "format_delegation_report",
    "make_event_timestamp",
    "normalize_batch_tasks",
    "normalize_single_task",
    "persona_for",
    "render_progress_snapshot",
    "strip_thinking_tags",
]
