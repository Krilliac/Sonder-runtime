"""Pure orchestration capacity calculation, request policy parsing, and formatting."""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import sonder_runtime.domain.fleet_briefing as fleet_briefing


DEFAULT_MAX_AGENTS = 16
ABSOLUTE_MAX_AGENTS = 64
DEFAULT_MAX_WORKERS = 8
STANDARD_MAX_WORKERS = 16
ABSOLUTE_MAX_WORKERS = 64
MAX_WORKER_COUNT_DIGITS = 64

RAM_RESERVE_BYTES = int(1.5 * 1024 ** 3)
RAM_PER_WORKER_BYTES = int(0.25 * 1024 ** 3)
GPU_RESERVE_BYTES = int(0.5 * 1024 ** 3)
GPU_KV_CACHE_PER_WORKER_BYTES = int(0.5 * 1024 ** 3)

ABORT_MARKERS = ("CANCELLED", "INTERRUPTED")

REPOSITORY_EVIDENCE_TOOLS = frozenset({
    "workspace_inventory", "directory_tree", "file_find", "file_read",
    "file_read_range", "text_search", "script_search", "image_inspect",
    "document_extract", "media_inspect", "code_metrics", "schema_validate",
    "code_graph", "diff_semantic", "symbol_verify", "citation_audit",
})

EVIDENCE_REQUIRED = (
    "EVIDENCE_REQUIRED: guarded source evidence was unavailable. Authorize the "
    "repository in file_roots.local, embed the relevant source excerpts, or use "
    "the tool-using agent surface to inspect it first. No unsupported answer was produced."
)

TRANSIENT_FAILURE_CLASSES = frozenset((
    "timeout", "unavailable", "throttled", "transport",
))

_REPOSITORY_REQUEST = re.compile(
    r"(?:\brepository\s*:|\brepo\s*:|\bcurrent\s+(?:file|code|diff|uncommitted)|"
    r"\b(?:inspect|read|review|audit|edit|fix)\b.{0,40}(?:\b(?:repo|repository|codebase|"
    r"workspace|files?|modules?|packages?|source|src|code)\b|(?<![A-Za-z0-9])[A-Za-z]:[\\/](?=[\w.~-])|(?<![\w:./\\-])/(?=[\w.~-]))|\buse\s+(?:local\s+)?file[- ]reading\s+tools?\b)",
    re.IGNORECASE | re.DOTALL,
)
_EMBEDDED_EVIDENCE = re.compile(
    r"(?:```|\bsource\s+excerpts?\s*:|\bbegin\s+file\b|\bpatch\s*:|\bdiff\s+--git\b)",
    re.IGNORECASE,
)
_REPO_FILE_PATH = re.compile(
    r"(?:[A-Za-z]:[\\/]|/)[^\s'\"]+\."
    r"(?:py|js|ts|tsx|jsx|cpp|cc|cxx|c|h|hpp|hxx|cs|java|go|rs|rb|php|swift|kt|"
    r"md|txt|rst|json|ya?ml|toml|ini|cfg|conf|xml|html?|css|scss|sh|ps1|bat|"
    r"cmake|gradle|sql|proto|glsl|hlsl)\b",
    re.IGNORECASE,
)
_FLEET_REQUEST = re.compile(
    r"(?:\bfleet\b|\bswarm\b|\bfan[- ]?out\b|\bparallel\s+agents?\b|"
    r"\bspawn\s+(?:as\s+much|as\s+many|the\s+maximum|maximum|parallel|subagents?)\b|"
    r"\bas\s+many\s+(?:subagents?|agents?)\b|\bparallel\s+workflow\b|(?<!\bnot\s)(?<!n't\s)(?<!never\s)\b(?:hand|delegate)\s+(?:this|it|that)?\s*(?:off\s+)?to\s+(?:sub)?agents?\b|"
    r"\bspawn\s+workflow\b|\bworkflow\b|\bmax(?:imum)?\s+agents?\b)",
    re.IGNORECASE,
)

_AFFIRMATIVE_WORKER_REQUEST = re.compile(
    r"(?i)^\s*(?:please\s+)?(?:use|run|spawn|launch)\s+"
    r"(?P<count>" + fleet_briefing.COUNT_TOKEN + r")\s+(?:concurrent\s+)?(?:workers?|agents?)\b"
)
_WORKER_COUNT_MENTION = re.compile(
    r"(?i)(?<!\w)(?P<count>" + fleet_briefing.COUNT_TOKEN + r")(?!\w)\s+"
    r"(?:concurrent\s+)?(?:workers?|agents?)\b"
)
_WORKER_REQUEST_NEGATION = re.compile(
    r"(?i)\b(?:do\s+not|don't|never|not|no)\b"
)
_WORKER_REQUEST_META = re.compile(
    r"(?i)\b(?:ignore|quoted?|phrase|document|instruction|example|"
    r"says?|mentions?|explain|why)\b"
)
_WORKER_REQUEST_COMPARATIVE = re.compile(
    r"(?i)\b(?:more|fewer|less|greater|lower)\s+than\b"
)
_WORKER_REQUEST_QUOTES = frozenset("\"'`“”‘’")

_THROTTLED_MESSAGE = re.compile(
    r"(?i)\b(?:429|too\s+many\s+requests|rate[- ]limit)",
)
_TIMEOUT_MESSAGE = re.compile(r"(?i)\btime[d]?\s*[- ]?out\b|\btimeout\b")
_UNAVAILABLE_MESSAGE = re.compile(
    r"(?i)connection\s+(?:refused|reset|aborted)|actively\s+refused|"
    r"\bunreachable\b|name\s+or\s+service\s+not\s+known|"
    r"temporarily\s+unavailable|service\s+unavailable|\b50[234]\b|"
    r"getaddrinfo\s+failed|remote\s+end\s+closed",
)

ADAPTIVE_OFF_VALUES = frozenset({"0", "false", "no", "off"})
ADAPTIVE_ON_VALUES = frozenset({"", "1", "true", "yes", "on"})


@dataclass(frozen=True)
class CapacityInputs:
    """Measured hardware, cluster, and operator inputs for :func:`compute_capacity`."""

    logical_cpus: int
    total_memory_bytes: int
    available_memory_bytes: int
    gpu_total_memory_bytes: int
    gpu_free_memory_bytes: int
    fleet_model_bytes: int
    gpu_slots: int
    ollama_parallel: int
    pool_route_active: bool
    reserved_slots: int
    fleet_worker_cap: int | None
    agent_ceiling: int
    requested_agents: int
    explicit_cap: int | None
    cap_error: str
    operator_ceiling: int
    operator_error: str
    parallel_workers_raw: str
    pool_note_resolver: Callable[[int, int, str], tuple[str, int, str]]


def positive_int(value: object) -> tuple[int | None, str]:
    """Parse a positive decimal integer without accepting bools/floats."""
    if isinstance(value, bool):
        return None, "boolean values are not worker counts"
    if isinstance(value, int):
        return (value, "") if value > 0 else (None, "worker count must be positive")
    if isinstance(value, str):
        raw = value.strip()
        if raw.isdecimal():
            if len(raw) > MAX_WORKER_COUNT_DIGITS:
                return None, "worker count has too many digits"
            try:
                parsed = int(raw)
            except (ValueError, OverflowError):
                return None, "worker count must be a positive decimal integer"
            return (parsed, "") if parsed > 0 else (None, "worker count must be positive")
        return None, "worker count must be a positive decimal integer"
    return None, "worker count must be a positive decimal integer"


def parse_operator_worker_ceiling(raw: str) -> tuple[int, str]:
    """Parse ``SONDER_MAX_WORKER_CAP`` into ``(ceiling, warning)``."""
    cleaned = raw.strip()
    if not cleaned:
        return ABSOLUTE_MAX_WORKERS, ""
    value, error = positive_int(cleaned)
    if error or value is None:
        return STANDARD_MAX_WORKERS, "invalid SONDER_MAX_WORKER_CAP: %s" % error
    return min(int(value), ABSOLUTE_MAX_WORKERS), ""


def requested_worker_cap(task: str) -> int | None:
    """Extract one tightly anchored, affirmative worker-count directive."""
    text = str(task or "")
    match = _AFFIRMATIVE_WORKER_REQUEST.match(text)
    if not match:
        return None
    if (
        _WORKER_REQUEST_NEGATION.search(text)
        or _WORKER_REQUEST_META.search(text)
        or _WORKER_REQUEST_COMPARATIVE.search(text)
        or any(quote in text for quote in _WORKER_REQUEST_QUOTES)
    ):
        return None
    mentions = list(_WORKER_COUNT_MENTION.finditer(text))
    if len(mentions) != 1 or mentions[0].start("count") != match.start("count"):
        return None
    token = match.group("count")
    value, error = positive_int(
        str(fleet_briefing.NUMBER_WORDS.get(token.lower(), token))
    )
    return None if (error or value is None) else min(int(value), ABSOLUTE_MAX_WORKERS)


def worker_request_ignored_reason(task: str) -> str:
    """Explain why a leading ``use N workers`` cue was not applied, or ``""``."""
    text = str(task or "")
    match = _AFFIRMATIVE_WORKER_REQUEST.match(text)
    if not match or requested_worker_cap(text) is not None:
        return ""
    cue = match.group(0).strip()
    detail = ""
    for label, pattern in (
        ("a negation", _WORKER_REQUEST_NEGATION),
        ("an explanatory or quoting word", _WORKER_REQUEST_META),
        ("a comparative", _WORKER_REQUEST_COMPARATIVE),
    ):
        found = pattern.search(text)
        if found:
            detail = "%s (%r)" % (label, found.group(0))
            break
    if not detail:
        if any(quote in text for quote in _WORKER_REQUEST_QUOTES):
            detail = "quotation marks"
        elif len(list(_WORKER_COUNT_MENTION.finditer(text))) != 1:
            detail = "more than one worker count"
        else:
            detail = "an invalid worker count"
    return (
        "%r was not applied because the request also contains %s, which "
        "the worker-count cue treats as a quoted or discussed count; to fan "
        "out anyway, run: /master fleet <task>" % (cue, detail)
    )


def requests_fleet(task: str) -> bool:
    """Recognize explicit natural-language requests for fleet fan-out."""
    return bool(_FLEET_REQUEST.search(task or "") or requested_worker_cap(task))


def requires_repository_tools(task: str) -> bool:
    """Return true when a task asks the model to inspect external repo state."""
    text = task or ""
    if _EMBEDDED_EVIDENCE.search(text):
        return False
    return bool(_REPOSITORY_REQUEST.search(text) or _REPO_FILE_PATH.search(text))


def evidence_gate(task: str, tools_available: bool = True) -> str:
    """Refuse ungrounded repo inspection only when guarded tools are unavailable."""
    if requires_repository_tools(task) and not tools_available:
        return EVIDENCE_REQUIRED
    return ""


def estimate_tokens(text: str) -> int:
    """Estimate rough token count for status display."""
    cleaned = text or ""
    return max(1, (len(cleaned) + 3) // 4) if cleaned else 0


def classify_worker_error(
    exc: BaseException,
    *,
    worker_model_failure_cls: type[BaseException] | None = None,
    timeout_types: tuple[type[BaseException], ...] = (TimeoutError,),
) -> str:
    """Map one worker exception onto the shared fanout failure vocabulary."""
    if worker_model_failure_cls is not None and isinstance(exc, worker_model_failure_cls):
        return str(getattr(exc, "failure_class", "unknown"))
    if isinstance(exc, timeout_types):
        return "timeout"
    if isinstance(exc, ConnectionError):
        return "unavailable"
    code = getattr(exc, "code", None)
    if isinstance(code, int) and not isinstance(code, bool):
        if code == 429:
            return "throttled"
        if 500 <= code <= 599:
            return "unavailable"
        if 400 <= code <= 499:
            return "request_rejected"
    message = str(exc or "")
    if _THROTTLED_MESSAGE.search(message):
        return "throttled"
    if _TIMEOUT_MESSAGE.search(message):
        return "timeout"
    if _UNAVAILABLE_MESSAGE.search(message):
        return "unavailable"
    if isinstance(exc, OSError):
        return "transport"
    return "unknown"


def parse_adaptive_concurrency_flag(raw: str) -> tuple[bool, bool]:
    """Return ``(enabled, unrecognized)`` for ``SONDER_FLEET_ADAPTIVE_CONCURRENCY``."""
    cleaned = raw.strip().lower()
    if cleaned in ADAPTIVE_OFF_VALUES:
        return False, False
    if cleaned not in ADAPTIVE_ON_VALUES:
        return True, True
    return True, False


def parse_transient_retries(raw: str) -> int:
    """Parse ``SONDER_FLEET_TRANSIENT_RETRIES`` clamped to ``0..3``."""
    cleaned = raw.strip()
    if not cleaned:
        return 1
    try:
        value = int(cleaned, 10)
    except ValueError:
        return 1
    return max(0, min(value, 3))


def compute_capacity(inputs: CapacityInputs) -> dict[str, Any]:
    """Compute queued breadth, bounded concurrent slots, limits, and warnings."""
    cpu_slots = max(1, min(DEFAULT_MAX_WORKERS, inputs.logical_cpus // 2 or 1))
    if inputs.available_memory_bytes > 0:
        usable = max(0, inputs.available_memory_bytes - RAM_RESERVE_BYTES)
        ram_slots = max(1, usable // RAM_PER_WORKER_BYTES)
    else:
        ram_slots = DEFAULT_MAX_WORKERS

    limits: dict[str, int] = {
        "requested": int(inputs.requested_agents),
        "cpu": int(cpu_slots),
        "ram": int(ram_slots),
        "policy": int(DEFAULT_MAX_WORKERS),
    }
    if inputs.gpu_slots > 0:
        limits["gpu_vram"] = int(inputs.gpu_slots)
    if inputs.fleet_worker_cap is not None:
        limits["fleet_workers"] = int(inputs.fleet_worker_cap)
    if inputs.ollama_parallel > 0:
        key = "pool_model_capacity" if inputs.pool_route_active else "ollama_num_parallel"
        limits[key] = int(inputs.ollama_parallel)
    if inputs.reserved_slots > 0:
        capacity_values = [v for k, v in limits.items() if k != "requested"]
        total_capacity = (
            min(capacity_values) if capacity_values else int(DEFAULT_MAX_WORKERS)
        )
        limits["reserved_capacity"] = max(1, total_capacity - inputs.reserved_slots)

    automatic = max(1, min(limits.values()))
    bound_by = min(limits, key=lambda key: (limits[key], key))
    source, slots = "auto", automatic
    cap_error = inputs.cap_error

    raw_override = inputs.parallel_workers_raw.strip()
    if raw_override:
        override, legacy_error = positive_int(raw_override)
        if legacy_error or override is None:
            cap_error = cap_error or (
                "invalid SONDER_PARALLEL_WORKERS: %s" % legacy_error
            )
        else:
            slots = min(
                override,
                inputs.requested_agents,
                STANDARD_MAX_WORKERS,
                inputs.operator_ceiling,
            )
            source = bound_by = "SONDER_PARALLEL_WORKERS"

    if inputs.explicit_cap:
        slots = min(
            inputs.explicit_cap,
            inputs.requested_agents,
            inputs.operator_ceiling,
            ABSOLUTE_MAX_WORKERS,
        )
        source = "per-run worker_cap"
        bound_by = (
            "operator worker ceiling"
            if slots == inputs.operator_ceiling
            and inputs.explicit_cap > inputs.operator_ceiling
            else "requested agents"
            if slots == inputs.requested_agents
            and inputs.explicit_cap > inputs.requested_agents
            else "per-run worker_cap"
        )

    pool_note, slots, bound_by = inputs.pool_note_resolver(
        inputs.requested_agents, slots, bound_by
    )
    warnings = [
        message
        for message in (inputs.operator_error, cap_error, pool_note)
        if message
    ]
    if inputs.ollama_parallel <= 0 and slots > 1:
        warnings.append(
            "OLLAMA_NUM_PARALLEL is unset -- Ollama auto-selects its batch width and "
            "collapses to 1 on a VRAM-tight box, which serializes every request and "
            "makes these %d worker slots buy nothing. Set OLLAMA_NUM_PARALLEL=%d and "
            "restart the Ollama server to actually get the concurrency."
            % (slots, slots)
        )
    elif 0 < inputs.ollama_parallel < slots:
        warnings.append(
            "OLLAMA_NUM_PARALLEL=%d is below the selected %d worker slots; raise it "
            "(and restart Ollama) or the extra workers will queue in Ollama."
            % (inputs.ollama_parallel, slots)
        )
    warning = "; ".join(warnings)
    return {
        "logical_cpus": inputs.logical_cpus,
        "total_memory_bytes": inputs.total_memory_bytes,
        "available_memory_bytes": inputs.available_memory_bytes,
        "gpu_total_memory_bytes": inputs.gpu_total_memory_bytes,
        "gpu_free_memory_bytes": inputs.gpu_free_memory_bytes,
        "fleet_model_bytes": inputs.fleet_model_bytes,
        "ollama_num_parallel": int(inputs.ollama_parallel),
        "agent_ceiling": inputs.agent_ceiling,
        "requested_agents": inputs.requested_agents,
        "worker_slots": slots,
        "automatic_worker_slots": automatic,
        "requested_worker_cap": inputs.explicit_cap or 0,
        "operator_worker_ceiling": inputs.operator_ceiling,
        "absolute_worker_ceiling": ABSOLUTE_MAX_WORKERS,
        "worker_cap_error": cap_error,
        "operator_worker_error": inputs.operator_error,
        "slot_limits": limits,
        "bound_by": bound_by,
        "source": source,
        "warning": warning,
        "ram_reserve_bytes": RAM_RESERVE_BYTES,
        "ram_per_worker_bytes": RAM_PER_WORKER_BYTES,
    }


def build_fallback_snapshot(
    agents: Mapping[str, Mapping[str, Any]],
    events_list: Sequence[Mapping[str, Any]],
    *,
    include_finished: bool,
    limit: int,
    max_events: int,
    progress_deadline_seconds: float,
) -> dict[str, Any]:
    """Assemble an in-memory status snapshot when the durable store is unreachable."""
    rows = sorted(
        (dict(row) for row in agents.values()),
        key=lambda row: int(row.get("updated_seq") or 0),
        reverse=True,
    )
    active = [row for row in rows if row.get("status") in ("queued", "running")]
    listed = rows if include_finished else active
    listed = listed[: max(1, int(limit or 20))]
    return {
        "active_agents": len(active),
        "running_agents": sum(1 for row in active if row.get("status") == "running"),
        "queued_agents": sum(1 for row in active if row.get("status") == "queued"),
        "active_model_calls": sum(1 for row in active if row.get("in_model_call")),
        "cancel_pending": sum(1 for row in active if row.get("cancel_requested")),
        "interrupted_agents": sum(
            1 for row in rows if row.get("status") == "interrupted"
        ),
        "task_drift_agents": sum(
            1 for row in rows if row.get("status") == "task_drift"
        ),
        "total_agents": len(rows),
        "total_listed": len(listed),
        "agents": listed,
        "events": list(events_list[-max_events:]),
        "tokens_in": sum(int(row.get("tokens_in") or 0) for row in rows),
        "tokens_out": sum(int(row.get("tokens_out") or 0) for row in rows),
        "latest_master_result": "",
        "latest_master": {},
        "stalled_agents": [],
        "stalled_agent_count": 0,
        "stalled_agent_total": 0,
        "progress_deadline_seconds": float(progress_deadline_seconds),
        "database": "",
    }


def format_capacity(data: Mapping[str, Any]) -> str:
    """Format a capacity dictionary for operator display."""
    gib = float(1024 ** 3)
    total = float(data.get("total_memory_bytes") or 0) / gib
    available = float(data.get("available_memory_bytes") or 0) / gib
    gpu_total = float(data.get("gpu_total_memory_bytes") or 0) / gib
    gpu_free = float(data.get("gpu_free_memory_bytes") or 0) / gib
    model = float(data.get("fleet_model_bytes") or 0) / gib
    limits = data.get("slot_limits") or {}
    lines = [
        "master orchestration capacity",
        "  logical CPUs: %s | RAM: %.1f/%.1f GiB available" % (
            data.get("logical_cpus", 0), available, total,
        ),
    ]
    if gpu_total > 0:
        lines.append(
            "  GPU VRAM: %.1f/%.1f GiB free | fleet model: %.2f GiB on disk" % (
                gpu_free, gpu_total, model,
            )
        )
    else:
        lines.append("  GPU VRAM: not detected (CPU inference; bounded by CPU slots)")
    lines.extend([
        "  agent ceiling: %s queued | concurrent worker slots: %s" % (
            data.get("agent_ceiling", 0), data.get("worker_slots", 0),
        ),
        "  automatic slots: %s | bound by: %s | source: %s" % (
            data.get("automatic_worker_slots", 0),
            data.get("bound_by", "?"),
            data.get("source", "auto"),
        ),
        "  per-run requested cap: %s | operator/absolute ceiling: %s/%s" % (
            data.get("requested_worker_cap", 0) or "none",
            data.get("operator_worker_ceiling", ABSOLUTE_MAX_WORKERS),
            data.get("absolute_worker_ceiling", ABSOLUTE_MAX_WORKERS),
        ),
    ])
    if limits:
        lines.append(
            "  per-constraint max: %s" % ", ".join(
                "%s=%s" % (name, limits[name]) for name in sorted(limits)
            )
        )
    parallel = int(data.get("ollama_num_parallel") or 0)
    lines.append(
        "  ollama batch width: %s" % (
            parallel if parallel > 0 else "unset (auto -- may serialize!)"
        )
    )
    lines.append(
        "  policy: reserve %.1f GiB RAM, %.2f GiB per worker; VRAM %.1f GiB reserved, "
        "%.2f GiB KV cache per parallel sequence" % (
            float(data.get("ram_reserve_bytes") or 0) / gib,
            float(data.get("ram_per_worker_bytes") or 0) / gib,
            GPU_RESERVE_BYTES / gib,
            GPU_KV_CACHE_PER_WORKER_BYTES / gib,
        )
    )
    warning = str(data.get("warning") or "").strip()
    if warning:
        lines.append("  WARNING: %s" % warning)
    return "\n".join(lines)


def format_snapshot(
    data: Mapping[str, Any],
    *,
    retry_lease_prefix: str = "retry-pending:",
) -> str:
    """Format a fleet status snapshot for human inspection."""
    lines = [
        "master orchestrator status",
        "  active agents: %s" % data.get("active_agents", 0),
        "  cancellation pending: %s" % data.get("cancel_pending", 0),
        "  interrupted/recoverable: %s" % data.get("interrupted_agents", 0),
        "  stalled/no-progress: %s (deadline %.1fs)" % (
            data.get("stalled_agent_total", data.get("stalled_agent_count", 0)),
            float(data.get("progress_deadline_seconds") or 0),
        ),
        "  task drift rejected: %s" % data.get("task_drift_agents", 0),
        "  tokens in/out: %s/%s" % (
            data.get("tokens_in", 0), data.get("tokens_out", 0),
        ),
    ]
    if data.get("database"):
        lines.append("  persistence: shared restart-safe fleet ledger")
    if data.get("store_error"):
        lines.append("  persistence warning: %s" % str(data["store_error"])[:240])
    capacity_data = data.get("capacity") or {}
    if capacity_data:
        lines.append("  capacity: %s queued ceiling / %s active worker slot(s) [%s]" % (
            capacity_data.get("agent_ceiling", 0),
            capacity_data.get("worker_slots", 0),
            capacity_data.get("source", "auto"),
        ))
    agents = data.get("agents") or []
    if not agents:
        lines.append("  agents: none yet")
    for row in agents[:12]:
        lines.append("  - %(id)s [%(status)s] %(activity)s" % row)
        if row.get("task_drift"):
            metrics = row.get("drift_metrics") or {}
            lines.append(
                "      task drift: covered=%s/%s missing=%s false_negative=%s loops=%s"
                % (
                    metrics.get("objectives_covered", 0),
                    metrics.get("objectives_total", 0),
                    metrics.get("missing_evidence", 0),
                    metrics.get("false_negative", 0),
                    metrics.get("repeated_tool_loop", 0),
                )
            )
        if row.get("role") == "master" and row.get("requested_agents"):
            lines.append("      schedule: %s agents / %s worker slots" % (
                row.get("requested_agents", 0), row.get("worker_slots", 0),
            ))
        if row.get("retry_of"):
            lines.append("      retry of: %s" % row["retry_of"])
        retried_by = str(row.get("retried_by") or "")
        if retried_by:
            lines.append(
                "      retry claimed: dispatch in progress"
                if retried_by.startswith(retry_lease_prefix)
                else "      retried by: %s" % retried_by
            )
        lines.append("      task: %s" % (row.get("task") or "")[:180])
        if row.get("project"):
            lines.append("      project: %s" % row["project"])
    latest_result = str(data.get("latest_master_result") or "")
    if latest_result:
        latest = data.get("latest_master") or {}
        latest_id = str(latest.get("id") or "").strip()
        latest_task = str(latest.get("task") or "").strip()
        heading = "latest completed master result"
        if latest_id:
            heading += " [%s]" % latest_id
        lines.extend(["", heading + ":"])
        if latest_task:
            lines.append("  task: %s" % latest_task[:240])
        if latest.get("project"):
            lines.append("  project: %s" % latest["project"])
        if int(data.get("active_agents") or 0) > 0:
            lines.append(
                "  note: completed history; this is not the result of the active agents above"
            )
        lines.append(latest_result[:8000])
    return "\n".join(lines)
