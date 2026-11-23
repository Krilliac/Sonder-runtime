"""Pure domain engine for model-call resilience, tool-failure classification,
alternative-method planning, context compaction, and safe circuit breaking.

Zero I/O, zero subprocesses, stdlib-only. Safe to import from domain,
application, adapter, and bootstrap layers.
"""
from __future__ import annotations

import ast
from collections import Counter, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import difflib
import json
import re
import threading
import time
from typing import Any

# --- 1. Failure classification ------------------------------------------------

_TRANSIENT_MODEL_MARKERS = (
    "timed out", "timeout", "connection reset", "connection refused",
    "temporarily unavailable", "service unavailable", "bad gateway",
    "gateway timeout", "too many requests", "rate limit", "overloaded",
    "broken pipe", "remote end closed", "incomplete read", "eof occurred",
    "http 429", "http 502", "http 503", "http 504", "502 bad gateway",
    "503 service unavailable", "504 gateway timeout", "empty response",
    "no usable reply", "server disconnected",
)
_CONTEXT_OVERFLOW_MARKERS = (
    "context length", "maximum context", "max_context", "prompt is too long",
    "exceeds the available context", "n_ctx", "too many tokens",
    "input length", "token limit", "context window", "context_length_exceeded",
)
_TOOL_UNSUPPORTED_MARKERS = (
    "does not support tools", "tools not supported", "tool calling is not supported",
    "tool_calls not supported", "function calling not supported",
    "invalid tools parameter",
)
_NON_RETRYABLE_MODEL_MARKERS = (
    "cancelled", "canceled", "aborted", "unauthorized", "forbidden",
    "invalid api key", "authentication required", "http 401", "http 403",
    "budget exhausted", "quota exceeded",
)


def classify_model_error(exc_or_msg: object) -> str:
    """Classify a model-call exception or error message into a recovery action class.

    Returns one of:
    - ``non_retryable`` (cancel/auth/budget — never retry)
    - ``context_overflow`` (compact earlier tool observations and retry)
    - ``tools_unsupported`` (retry once with ``tools=None``)
    - ``transient`` (retry with bounded exponential backoff)
    - ``unknown`` (non-transient permanent error)
    """
    kind_attr = str(getattr(exc_or_msg, "kind", "") or "").strip().lower()
    if kind_attr in {"cancelled", "budget", "auth", "unsupported_feature"}:
        return "tools_unsupported" if kind_attr == "unsupported_feature" else "non_retryable"
    if kind_attr in {"timeout", "transport", "rate_limit", "overloaded", "protocol"}:
        text_check = str(exc_or_msg or "").lower()
        if any(m in text_check for m in _CONTEXT_OVERFLOW_MARKERS):
            return "context_overflow"
        if any(m in text_check for m in _TOOL_UNSUPPORTED_MARKERS):
            return "tools_unsupported"
        return "transient"

    text = f"{type(exc_or_msg).__name__}: {exc_or_msg}".lower()
    if any(m in text for m in _NON_RETRYABLE_MODEL_MARKERS):
        return "non_retryable"
    if any(m in text for m in _CONTEXT_OVERFLOW_MARKERS):
        return "context_overflow"
    if any(m in text for m in _TOOL_UNSUPPORTED_MARKERS):
        return "tools_unsupported"
    if isinstance(exc_or_msg, (TimeoutError, ConnectionError)) or any(m in text for m in _TRANSIENT_MODEL_MARKERS):
        return "transient"
    return "unknown"


def classify_tool_failure(tool_name: str, error_text: str) -> str:
    """Classify a tool failure observation into a concrete root cause."""
    low = str(error_text or "").lower()
    if any(k in low for k in ("host policy", "permission", "refused", "denied", "forbidden", "manual mode", "plan mode", "approval")):
        return "permission_refused"
    if any(k in low for k in ("not found", "no such file", "does not exist", "cannot find the path", "missing file")):
        return "not_found"
    if any(k in low for k in ("is not recognized", "not installed", "no module named", "command not found", "executable not found", "winerror 2")):
        return "missing_dependency"
    if any(k in low for k in ("timed out", "timeout", "deadline exceeded")):
        return "timeout"
    if any(k in low for k in ("not unique", "multiple matches", "old string not found", "patch does not apply", "hunk failed")):
        return "edit_mismatch"
    if any(k in low for k in ("syntaxerror", "invalid json", "malformed", "required", "unexpected keyword", "invalid argument", "typeerror", "valueerror")):
        return "argument_or_syntax"
    if any(k in low for k in ("network", "dns", "ssl", "http 4", "http 5", "unreachable", "offline", "connection")):
        return "network_or_remote"
    if any(k in low for k in ("database is locked", "busy", "resource temporarily unavailable")):
        return "transient_lock"
    if any(k in low for k in ("truncated", "too large", "exceeds", "max_bytes", "limit")):
        return "size_or_budget"
    return "execution_error"


# --- 2. Safe multi-stage tool-argument repair --------------------------------

_FENCE_STRIP_RE = re.compile(r"^\s*```(?:json|python|js)?\s*\r?\n(.*?)\r?\n```\s*$", re.DOTALL | re.IGNORECASE)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def repair_tool_arguments(raw: Any) -> tuple[dict[str, Any], bool]:
    """Parse tool arguments, safely repairing common local-LLM JSON mistakes.

    Returns ``(parsed_dict, was_repaired)``. Never calls ``eval()``/``exec()``.
    Handles:
    - Markdown code fences wrapping JSON
    - Leading/trailing prose around a JSON object
    - Trailing commas before ``}`` or ``]``
    - Python-style single-quoted dicts / ``True`` / ``False`` / ``None`` via ``ast.literal_eval``
    """
    if isinstance(raw, Mapping):
        return dict(raw), False
    if not isinstance(raw, str):
        return {}, False
    text = raw.strip()
    if not text:
        return {}, False
    # Fast path: valid JSON object
    try:
        loaded = json.loads(text)
        if isinstance(loaded, Mapping):
            return dict(loaded), False
    except (TypeError, ValueError):
        pass

    candidate = text
    fence_match = _FENCE_STRIP_RE.match(candidate)
    if fence_match:
        candidate = fence_match.group(1).strip()

    # Extract outermost {...} if model wrapped JSON in prose
    first_brace = candidate.find("{")
    last_brace = candidate.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = candidate[first_brace : last_brace + 1]

    # Strip trailing commas and retry JSON
    cleaned = _TRAILING_COMMA_RE.sub(r"\1", candidate)
    try:
        loaded = json.loads(cleaned)
        if isinstance(loaded, Mapping):
            return dict(loaded), True
    except (TypeError, ValueError):
        pass

    # Safe Python literal fallback for single quotes / True / False / None
    if len(cleaned) <= 32_000:
        try:
            lit = ast.literal_eval(cleaned)
            if isinstance(lit, Mapping):
                return {str(k): v for k, v in lit.items()}, True
        except (SyntaxError, ValueError, TypeError, RecursionError):
            pass

    return {}, False


# --- 3. Canonical tool-name alias & fuzzy resolver ---------------------------

_COMMON_TOOL_ALIASES: dict[str, str] = {
    "read_file": "file_read",
    "open_file": "file_read",
    "cat": "file_read",
    "view_file": "file_read",
    "read_lines": "file_read_range",
    "write_file": "file_write",
    "create_file": "file_write",
    "edit_file": "file_edit",
    "replace_in_file": "file_edit",
    "list_files": "directory_tree",
    "list_dir": "directory_tree",
    "ls": "directory_tree",
    "find_files": "file_find",
    "glob": "file_find",
    "grep": "text_search",
    "search_text": "text_search",
    "search_code": "text_search",
    "rg": "text_search",
    "run_python": "run_code",
    "execute_code": "run_code",
    "eval_code": "run_code",
    "python": "run_code",
    "run_tests": "test_run",
    "pytest": "test_run",
    "calc": "math_eval",
    "calculate": "math_eval",
    "calculator": "math_eval",
    "solve_math": "math_eval",
    "sat": "logic_verify",
    "verify_logic": "logic_verify",
    "test_regex": "regex_lab",
    "regex": "regex_lab",
    "dep_graph": "code_graph",
    "call_graph": "code_graph",
    "impact_analysis": "code_graph",
    "semantic_diff": "diff_semantic",
    "ast_diff": "diff_semantic",
    "scratchpad": "reasoning_board",
    "think": "reasoning_board",
    "verify_symbol": "symbol_verify",
    "check_symbol": "symbol_verify",
    "audit_citations": "citation_audit",
    "check_citations": "citation_audit",
    "cross_check": "fact_cross_check",
    "check_facts": "fact_cross_check",
    "check_consistency": "consistency_check",
    "fallback": "plan_fallback",
    "alternative_methods": "plan_fallback",
    "shield": "prompt_shield",
    "injection_check": "prompt_shield",
    "diagnose": "error_diagnose",
    "parse_error": "error_diagnose",
    "stacktrace": "error_diagnose",
    "audit_security": "code_audit",
    "static_audit": "code_audit",
    "transform_data": "data_transform",
    "convert_data": "data_transform",
    "cron_next": "data_transform",
    "search_memory": "memory_search",
    "search_tools": "tool_search",
}


def resolve_tool_name(
    raw_name: str,
    admitted: Sequence[str] | frozenset[str],
) -> tuple[str, list[str], bool]:
    """Resolve a model-emitted tool name against ``admitted`` tools only.

    Returns ``(resolved_name, close_suggestions, was_aliased)``.
    Never resolves to a tool outside ``admitted`` (preserves all policy gates).
    """
    clean = str(raw_name or "").strip().lstrip("/")
    admitted_set = set(admitted)
    if not clean:
        return "", [], False
    if clean in admitted_set:
        return clean, [], False

    low = clean.lower().replace("-", "_")
    if low in admitted_set:
        return low, [], True

    mapped = _COMMON_TOOL_ALIASES.get(low)
    if mapped and mapped in admitted_set:
        return mapped, [], True

    suggestions = difflib.get_close_matches(low, sorted(admitted_set), n=4, cutoff=0.6)
    return clean, suggestions, False


# --- 4. Alternative-method & recovery playbook engine ------------------------

@dataclass(frozen=True)
class FallbackOption:
    tool: str
    strategy: str
    example_args: dict[str, Any]


# Maps (failed_tool_or_category) -> ordered alternative strategies.
_TOOL_ALTERNATIVES: dict[str, tuple[FallbackOption, ...]] = {
    "run_code": (
        FallbackOption("math_eval", "Compute exact arithmetic, algebra, matrices, statistics, or unit conversions without subprocess execution.", {"action": "eval", "expression": "<expr>"}),
        FallbackOption("logic_verify", "Verify Boolean equivalence, SAT, implications, or constraint satisfaction purely in-process.", {"action": "satisfiable", "formula": "<formula>"}),
        FallbackOption("regex_lab", "Test regexes, capture groups, and substitutions deterministically without running code.", {"pattern": "<regex>", "text": "<sample>"}),
        FallbackOption("symbol_verify", "Statically audit Python code for hallucinated imports, attributes, and kwargs without executing it.", {"action": "audit_code", "code": "<code>"}),
        FallbackOption("reasoning_board", "Break the problem into verified facts and hypotheses when execution is unavailable.", {"action": "init", "goal": "<task>"}),
    ),
    "test_run": (
        FallbackOption("error_diagnose", "Parse existing failure logs/tracebacks to pinpoint the exact repo line and root cause.", {"error_text": "<traceback_or_log>", "root": "."}),
        FallbackOption("code_audit", "Run static security and bug-footgun analysis on modified source files without executing tests.", {"path": "<file>"}),
        FallbackOption("symbol_verify", "Statically verify imports, signatures, and kwargs in modified Python files.", {"action": "audit_code", "code": "<code>"}),
        FallbackOption("code_graph", "Trace callers and blast-radius impact across modules without running a test runner.", {"action": "impact", "target": "<file_or_symbol>"}),
        FallbackOption("diff_semantic", "Compare AST/structural changes before and after edits to verify signature preservation.", {"path_a": "<before>", "path_b": "<after>"}),
        FallbackOption("file_check", "Run lightweight per-file static linters/checkers on changed files.", {"path": "<file>"}),
    ),
    "file_read": (
        FallbackOption("file_find", "Locate the exact path when the guessed filename does not exist.", {"query": "<filename_fragment>", "root": "."}),
        FallbackOption("directory_tree", "List workspace structure to confirm actual directory and file names.", {"path": ".", "depth": 3}),
        FallbackOption("file_read_range", "Read a targeted line window if the file exceeded max_bytes.", {"path": "<path>", "start_line": 1, "end_line": 200}),
        FallbackOption("text_search", "Jump directly to the relevant symbol or line without loading the whole file.", {"query": "<symbol>", "root": "."}),
    ),
    "file_edit": (
        FallbackOption("file_read_range", "Inspect exact surrounding lines and whitespace before retrying an edit.", {"path": "<path>", "start_line": 1, "end_line": 120}),
        FallbackOption("text_search", "Locate every occurrence of the target snippet to make the replacement unique.", {"query": "<snippet>", "root": "."}),
        FallbackOption("text_patch", "Apply a unified diff with line context when a plain string match is ambiguous.", {"path": "<path>", "patch": "<unified_diff>"}),
    ),
    "text_search": (
        FallbackOption("repository_symbol_index", "Index declarations (classes, functions, methods) directly when grep misses or is too noisy.", {"root": "."}),
        FallbackOption("file_find", "Find files by filename pattern instead of content.", {"query": "<pattern>", "root": "."}),
        FallbackOption("code_graph", "Trace module imports and Python call graphs structurally.", {"root": ".", "action": "calls", "target": "<symbol>"}),
    ),
    "web_search": (
        FallbackOption("memory_search", "Check Sonder's local memory for previously learned facts or solutions.", {"query": "<topic>"}),
        FallbackOption("reference_search", "Search the local reference library offline.", {"query": "<topic>"}),
        FallbackOption("symbol_verify", "Verify Python/library API facts directly from the local environment instead of guessing.", {"module": "<mod>", "symbol": "<attr>"}),
    ),
    "web_fetch": (
        FallbackOption("web_search", "Query summarized search snippets when direct page fetching fails or is blocked.", {"query": "<topic>"}),
        FallbackOption("memory_search", "Fall back to local memory if offline or rate-limited.", {"query": "<topic>"}),
    ),
    "build_run": (
        FallbackOption("code_graph", "Inspect dependency cycles and import errors statically.", {"root": ".", "action": "cycles"}),
        FallbackOption("code_metrics", "Audit syntax/complexity hotspots statically.", {"path": "."}),
        FallbackOption("symbol_verify", "Check Python API/import validity statically.", {"action": "audit_code", "code": "<code>"}),
    ),
}

_CAUSE_STRATEGIES: dict[str, str] = {
    "permission_refused": "Do not retry the blocked action or try to bypass permissions. Switch to a read-only/pure analytical alternative or ask the user to approve/run it.",
    "not_found": "Stop guessing paths. Use file_find, directory_tree, or text_search to discover the real path first.",
    "missing_dependency": "The required binary/package is unavailable on this host. Switch to pure stdlib/static tools (math_eval, logic_verify, symbol_verify, code_graph, diff_semantic).",
    "timeout": "Narrow scope (smaller line range, specific file/subdirectory, lower max_depth, or static analysis instead of full execution).",
    "edit_mismatch": "Read the exact target lines with file_read_range before editing, or include more surrounding lines to make the match unique.",
    "argument_or_syntax": "Inspect the tool's exact parameter schema via tool_search/tool_help and pass valid JSON types.",
    "network_or_remote": "Network/remote endpoint failed. Switch to offline local inspection, memory_search, or pure verification tools.",
    "transient_lock": "Resource was briefly busy. Retry once after narrowing scope or switch to read-only inspection.",
    "size_or_budget": "Target exceeded byte/size limits. Use file_read_range, text_search, or targeted subdirectory roots.",
    "execution_error": "Inspect the exact error message, verify assumptions with static/pure tools, and try an alternative approach instead of repeating the same call.",
}


def plan_alternatives(
    *,
    failed_tool: str,
    error: str = "",
    goal: str = "",
    available_tools: Sequence[str] | None = None,
    tried_tools: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Produce ranked, policy-safe alternative tools and recovery steps after a tool failure."""
    tool = str(failed_tool or "").strip().lstrip("/")
    cause = classify_tool_failure(tool, error)
    tried = {str(t).strip().lstrip("/") for t in (tried_tools or ()) if str(t).strip()}
    if tool:
        tried.add(tool)
    avail = set(available_tools) if available_tools is not None else None

    candidates: list[FallbackOption] = list(_TOOL_ALTERNATIVES.get(tool, ()))

    # Add cause-specific general fallbacks
    if cause == "not_found":
        candidates.extend([
            FallbackOption("file_find", "Find the actual file path by name/glob.", {"query": "<name>", "root": "."}),
            FallbackOption("directory_tree", "Inspect directory layout.", {"path": ".", "depth": 3}),
        ])
    elif cause == "argument_or_syntax":
        candidates.append(
            FallbackOption("tool_search", "Inspect the exact JSON schema and parameter names of the tool.", {"name": tool or "<tool_name>"})
        )
    elif cause == "size_or_budget":
        candidates.extend([
            FallbackOption("file_read_range", "Read a bounded slice of lines.", {"path": "<path>", "start_line": 1, "end_line": 150}),
            FallbackOption("text_search", "Search for exact matches instead of reading full files.", {"query": "<term>", "root": "."}),
        ])
    elif cause in {"permission_refused", "missing_dependency"}:
        candidates.extend([
            FallbackOption("math_eval", "Perform pure mathematical/statistical/unit verification without execution.", {"expression": "<expr>"}),
            FallbackOption("symbol_verify", "Verify Python imports/attributes/kwargs statically.", {"action": "audit_code", "code": "<code>"}),
            FallbackOption("code_graph", "Inspect code structure and impact statically.", {"root": ".", "action": "summary"}),
        ])

    # Always offer reasoning_board for multi-step replanning if not yet tried
    candidates.append(
        FallbackOption("reasoning_board", "Record the blocker as a grounded fact and evaluate alternative hypotheses.", {"action": "add_fact", "text": f"{tool} failed: {str(error)[:160]}"})
    )

    seen: set[str] = set()
    alternatives: list[dict[str, Any]] = []
    for opt in candidates:
        if opt.tool in seen:
            continue
        if opt.tool in tried and opt.tool != "tool_search":
            continue
        if avail is not None and opt.tool not in avail:
            continue
        seen.add(opt.tool)
        alternatives.append({
            "tool": opt.tool,
            "strategy": opt.strategy,
            "example_args": opt.example_args,
        })

    return {
        "ok": True,
        "failed_tool": tool,
        "failure_class": cause,
        "goal": str(goal or "")[:300],
        "guidance": _CAUSE_STRATEGIES.get(cause, _CAUSE_STRATEGIES["execution_error"]),
        "alternatives": alternatives[:8],
    }


def format_fallback_hint(
    failed_tool: str,
    error: str,
    *,
    available_tools: Sequence[str] | None = None,
    tried_tools: Sequence[str] | None = None,
    repeated: bool = False,
) -> str:
    """Format a compact, actionable recovery hint appended to a failed tool observation."""
    plan = plan_alternatives(
        failed_tool=failed_tool,
        error=error,
        available_tools=available_tools,
        tried_tools=tried_tools,
    )
    prefix = "[LOOP BREAKER — REPEATED CALL]" if repeated else "[RECOVERY HINT]"
    alt_names = [a["tool"] for a in plan["alternatives"][:4]]
    lines = [f"\n{prefix} ({plan['failure_class']}): {plan['guidance']}"]
    if alt_names:
        lines.append(f"Suggested alternative tool(s) available in this turn: {', '.join(alt_names)}.")
    return "\n".join(lines)


# --- 5. Context-overflow message compactor -----------------------------------

def compact_messages_for_retry(
    messages: Sequence[dict[str, Any]],
    *,
    max_tool_chars: int = 800,
    keep_recent: int = 4,
) -> tuple[list[dict[str, Any]], int]:
    """Shrink oversized tool/assistant observations to recover from context overflow.

    First compacts earlier messages while keeping ``keep_recent`` intact; if that
    saves zero bytes (e.g. the newest tool observation itself overflowed context),
    compacts oversized ``role="tool"`` messages in the tail while always preserving
    the system message and original user prompt.
    """
    def _pass(protect_tail: int) -> tuple[list[dict[str, Any]], int]:
        out: list[dict[str, Any]] = []
        saved = 0
        total = len(messages)
        for idx, msg in enumerate(messages):
            item = dict(msg)
            role = str(item.get("role") or "")
            content = item.get("content")
            is_protected = (
                (idx == 0 and role == "system")
                or (idx <= 1 and role == "user")
                or (protect_tail > 0 and idx >= total - protect_tail)
            )
            if not is_protected and isinstance(content, str) and len(content) > max_tool_chars:
                if role == "tool":
                    head = content[: max_tool_chars // 2]
                    tail = content[-(max_tool_chars // 3) :]
                    trimmed = f"{head}\n... [compacted {len(content) - len(head) - len(tail)} chars for context recovery] ...\n{tail}"
                    saved += max(0, len(content) - len(trimmed))
                    item["content"] = trimmed
                elif role == "assistant" and len(content) > max_tool_chars * 2:
                    trimmed = content[: max_tool_chars * 2] + "\n... [compacted]"
                    saved += max(0, len(content) - len(trimmed))
                    item["content"] = trimmed
            out.append(item)
        return out, saved

    first, saved = _pass(keep_recent)
    if saved == 0 and keep_recent > 0:
        return _pass(0)
    return first, saved


# --- 6. Thread-safe process resilience telemetry & circuit breaker -----------

class CircuitBreaker:
    """Bounded failure-threshold circuit breaker for flaky endpoints/tools."""

    def __init__(self, *, failure_threshold: int = 4, cooldown_seconds: float = 30.0) -> None:
        self._threshold = max(1, int(failure_threshold))
        self._cooldown = max(1.0, float(cooldown_seconds))
        self._lock = threading.Lock()
        self._failures: dict[str, int] = {}
        self._opened_until: dict[str, float] = {}

    def is_open(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            until = self._opened_until.get(key, 0.0)
            if until > now:
                return True
            if until > 0.0:
                # Half-open: allow probe and clear open window
                self._opened_until.pop(key, None)
            return False

    def record_success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
            self._opened_until.pop(key, None)

    def record_failure(self, key: str) -> bool:
        """Record a failure; return True if this tripped the circuit open."""
        now = time.monotonic()
        with self._lock:
            count = self._failures.get(key, 0) + 1
            self._failures[key] = count
            if count >= self._threshold:
                self._opened_until[key] = now + self._cooldown
                return True
            return False

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            open_keys = {k: round(v - now, 1) for k, v in self._opened_until.items() if v > now}
            return {"open_circuits": open_keys, "tracked_failures": dict(self._failures)}


class ResilienceTelemetry:
    """In-process bounded telemetry for model-call & tool-call recoveries."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Counter[str] = Counter()
        self._failed_tools: Counter[str] = Counter()
        self._recent: deque[dict[str, Any]] = deque(maxlen=40)

    def record(self, event: str, *, tool: str = "", detail: str = "") -> None:
        with self._lock:
            self._counters[event] += 1
            if tool and "fail" in event:
                self._failed_tools[tool] += 1
            self._recent.append({
                "ts": round(time.time(), 1),
                "event": event,
                "tool": tool,
                "detail": str(detail or "")[:180],
            })

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "counters": dict(self._counters),
                "top_failing_tools": dict(self._failed_tools.most_common(10)),
                "recent_events": list(self._recent)[-15:],
            }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._failed_tools.clear()
            self._recent.clear()


GLOBAL_TELEMETRY = ResilienceTelemetry()
GLOBAL_CIRCUIT_BREAKER = CircuitBreaker()


__all__ = [
    "CircuitBreaker",
    "FallbackOption",
    "GLOBAL_CIRCUIT_BREAKER",
    "GLOBAL_TELEMETRY",
    "ResilienceTelemetry",
    "classify_model_error",
    "classify_tool_failure",
    "compact_messages_for_retry",
    "format_fallback_hint",
    "plan_alternatives",
    "repair_tool_arguments",
    "resolve_tool_name",
]
