"""Pure deterministic difficulty-to-execution-mode router.

Decides *how* to execute a turn -- single model, parallel multi-model ensemble
(e.g. local GPU ``code`` + Node1 ``reasoning``), bounded local fan-out, or
autonomous ``/agent`` -- from prompt difficulty signals, conversation depth,
bound local capacity, tier health/latency hints, and explicit operator policy.

Pure domain module: no I/O, no environment reads, no network, no SQLite, and
no permission mutations.  The application service loads operator settings,
available tier bindings, and health/latency snapshots and passes them into
:func:`plan_execution_mode`.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

ExecutionMode = Literal["single", "ensemble", "fanout", "agent"]
DifficultyBand = Literal["quick", "standard", "hard", "multi_scope"]
ExecutionModeRequest = Literal["off", "auto", "single", "ensemble", "fanout", "agent"]
EnsembleMode = Literal["prose", "code"]
TierHealthStatus = Literal["healthy", "slow", "unavailable"]

VALID_EXECUTION_MODES: frozenset[str] = frozenset(
    {"off", "auto", "single", "ensemble", "fanout", "agent"}
)

DEFAULT_SLOW_TIER_LATENCY_MS: float = 15000.0
DEFAULT_FAST_TIER_LATENCY_MS: float = 4000.0
DEFAULT_MAX_TIER_ERROR_RATE: float = 0.5

_LOG_FENCE_LANGS: frozenset[str] = frozenset(
    {
        "log",
        "logs",
        "txt",
        "text",
        "output",
        "console",
        "stderr",
        "stdout",
        "trace",
        "traceback",
        "ndjson",
        "csv",
        "tsv",
        "syslog",
        "accesslog",
        "shell-session",
        "console-output",
    }
)

_CODE_FENCE_LANGS: frozenset[str] = frozenset(
    {
        "py",
        "python",
        "cpp",
        "c++",
        "cc",
        "cxx",
        "c",
        "h",
        "hpp",
        "rust",
        "rs",
        "ts",
        "typescript",
        "tsx",
        "js",
        "javascript",
        "jsx",
        "dart",
        "go",
        "golang",
        "java",
        "kotlin",
        "kt",
        "cs",
        "csharp",
        "sql",
        "cmake",
        "hlsl",
        "glsl",
        "wgsl",
    }
)

_FENCE_OPEN_PATTERN = re.compile(r"^\s*(?P<marker>`{3,}|~{3,})\s*(?P<lang>[^\s`~]*)")
_URL_PATTERN = re.compile(r"https?://\S+", re.I)
_INLINE_CODE_PATTERN = re.compile(r"`[^`\n]+`")

_LOG_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # ISO / syslog / bracketed timestamps
    re.compile(
        r"^\s*(?:\[)?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?(?:\])?(?:\s|\|)"
    ),
    re.compile(
        r"^\s*(?:\[)?\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:\])?\s+(?:\[?(?:TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|ERR|FATAL|CRITICAL|NOTICE)\b)"
    ),
    re.compile(
        r"^(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\S+"
    ),
    # Explicit log-level prefixes
    re.compile(
        r"^\s*(?:\[(?:TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|ERR|FATAL|CRITICAL|NOTICE)\]|"
        r"(?:TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|FATAL|CRITICAL)(?:\s*[:|]|\s+\S+:\d+))",
    ),
    # Tracebacks & stack frames (Python, JS/Java/C#, GDB/ASAN/Go)
    re.compile(r"^\s*Traceback \(most recent call last\):\s*$"),
    re.compile(r'^\s*File "[^"]+", line \d+(?:, in .+)?$'),
    re.compile(r"^\s+at\s+\S+\s*\([^)]*:\d+(?::\d+)?\)\s*$"),
    re.compile(r"^\s*#\d+\s+0x[0-9a-fA-F]+\b"),
    re.compile(r"^\s*==\d+==(?:ERROR|WARNING|SUMMARY|ABORTING):"),
    re.compile(r"^\s*goroutine\s+\d+\s+\["),
    re.compile(r"^\s*Caused by:\s+[A-Za-z_][\w.$]*(?:Error|Exception)\b"),
    # Build / compiler / test-runner diagnostics
    re.compile(r"^\s*\[\d+/\d+\]\s+(?:Building|Compiling|Linking|Running|FAILED)\b"),
    re.compile(
        r"^\s*[^\s:]+\(\d+(?:,\d+)?\)\s*:\s*(?:fatal\s+)?(?:error|warning)\s+[A-Z]+\d+:"
    ),
    re.compile(r"^\s*[^\s:]+:\d+:\d+:\s*(?:fatal\s+)?(?:error|warning|note):"),
    re.compile(r"^\s*(?:FAILED|PASSED|ERROR|SKIPPED|XFAIL)\s+\S+::\S+"),
    # HTTP access logs & structured JSON/NDJSON log records
    re.compile(
        r'^\s*(?:\d{1,3}\.){3}\d{1,3}\s+.*"(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+/\S*\s+HTTP/\d'
    ),
    re.compile(
        r'^\s*\{.*"(?:level|severity|timestamp|ts|logger|msg|message|event)"\s*:.*\}\s*$'
    ),
)

_TRACEBACK_HEADER_PATTERN = re.compile(r"^\s*Traceback \(most recent call last\):\s*$")
_TRACEBACK_FRAME_PATTERN = re.compile(r'^\s*File "[^"]+", line \d+')
_EXCEPTION_LINE_PATTERN = re.compile(r"^\s*[A-Za-z_][\w.]*(?:Error|Exception|Warning|Panic):\s*")
_INLINE_LOG_MARKER_PATTERN = re.compile(
    r"(?:\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}|\[(?:INFO|WARN|WARNING|ERROR|DEBUG)\])"
)

_HARD_REASONING_PATTERNS: tuple[tuple[str, re.Pattern[str], float], ...] = (
    (
        "explicit_hard_cue",
        re.compile(
            r"\b(think\s+hard|think\s+carefully|be\s+rigorous|reason\s+through|"
            r"step\s+by\s+step|deep\s+dive|second\s+opinion|double[- ]check|"
            r"carefully\s+verify|first\s+principles|exhaustively)\b",
            re.I,
        ),
        0.38,
    ),
    (
        "architecture_tradeoff",
        re.compile(
            r"\b(trade[- ]?offs?|architect(?:ure|ural)?|"
            r"design\s+a\s+(?:system|protocol|engine|pipeline|scheduler|router|schema)|"
            r"system\s+design|invariants?|failure\s+modes?|distributed|consensus|"
            r"scalability|backpressure|idempotenc\w*|consistency\s+model|"
            r"linearizab\w*|eventual\s+consistency|sharding|fault\s+toleran\w*|"
            r"state\s+machine|ownership\s+model|thread\s+affinity|"
            r"hot[- ]reload\s+boundar\w*|migration\s+plan|strangler)\b",
            re.I,
        ),
        0.34,
    ),
    (
        "concurrency_memory_systems",
        re.compile(
            r"\b(race\s+conditions?|deadlocks?|livelocks?|lock[- ]?free|wait[- ]?free|"
            r"memory\s+leaks?|use[- ]after[- ]free|double[- ]free|data\s+races?|"
            r"memory\s+order|acquire/release|aba\s+problem|ubsan|tsan|asan|"
            r"heap\s+corruption|buffer\s+overflows?|stack\s+overflows?|"
            r"false\s+sharing|cache\s+coheren\w*)\b",
            re.I,
        ),
        0.36,
    ),
    (
        "security_analysis",
        re.compile(
            r"\b(threat\s+models?|security\s+audits?|security\s+reviews?|attack\s+surface|"
            r"privilege\s+escalation|ssrf|csrf|sql\s+injection|command\s+injection|"
            r"path\s+traversal|injection|cryptograph\w*|timing\s+attacks?|"
            r"side[- ]channel|sandbox\s+escapes?|auth(?:entication|orization)\s+bypass|"
            r"vulnerabilit(?:y|ies))\b",
            re.I,
        ),
        0.34,
    ),
    (
        "formal_math_proof",
        re.compile(
            r"\b(prove\b|proofs?\b|theorems?\b|lemmas?\b|corollar(?:y|ies)\b|derive\b|asymptotic|"
            r"big[- ]o\b|time\s+complexity|space\s+complexity|amortized|"
            r"correctness\s+proofs?|induction|counterexamples?|recurrences?|"
            r"closed[- ]form|eigenvalue\w*|differential\s+equation\w*|"
            r"combinatorics|bayesian|markov|convex\s+optimization|"
            r"np[- ]hard|np[- ]complete|formal\s+verification|soundness|"
            r"completeness|decidability|invariant\s+proofs?)\b",
            re.I,
        ),
        0.34,
    ),
    (
        "code_audit",
        re.compile(
            r"\b(code\s+audits?|code\s+reviews?|defect\s+sweep|static\s+analysis|"
            r"edge[- ]case\s+(?:audit|review|analysis)|correctness\s+audit|"
            r"soundness\s+review|"
            r"audit\s+(?:this|the)\s+(?:code|diff|module|file|implementation|pr|"
            r"handler|parser|router|gateway|store|service|pipeline)|"
            r"review\s+(?:this|the)\s+(?:code|diff|pr|implementation|module)\s+for\s+"
            r"(?:bugs|correctness|safety|races|leaks|edge\s+cases|regressions)|"
            r"find\s+(?:subtle\s+)?(?:bugs|defects|edge\s+cases|regressions)|"
            r"harden\s+(?:this|the)\s+(?:code|parser|handler|router|gateway|module|store|pipeline))\b",
            re.I,
        ),
        0.32,
    ),
    (
        "deep_debug_crash",
        re.compile(
            r"\b(segfaults?|segmentation\s+faults?|access\s+violations?|0xc0000005|"
            r"crash\s+dumps?|heisenbugs?|non[- ]?deterministic|memory\s+corruption|"
            r"silent\s+corruption|starvation|bisect|minimal\s+repro(?:ducer)?s?|"
            r"silent(?:ly)?\s+(?:fails?|drops?|corrupts?|no-?ops?))\b",
            re.I,
        ),
        0.34,
    ),
    (
        "root_cause_debug",
        re.compile(
            r"\b(root\s+causes?|why\s+(?:does|is|would|did)\b|intermittent|flaky|"
            r"subtle\s+bugs?|diagnos\w*|bottlenecks?|profil(?:e|ing)|regressions?|"
            r"infinite\s+loops?|off[- ]by[- ]one|false\s+positives?|false\s+negatives?)\b",
            re.I,
        ),
        0.26,
    ),
    (
        "comparative_analysis",
        re.compile(
            r"\b(compare\s+and\s+contrast|versus\b|\bvs\.?\b|pros\s+and\s+cons|"
            r"evaluate\s+(?:options|approaches|alternatives))\b",
            re.I,
        ),
        0.24,
    ),
)

_MULTI_SCOPE_PATTERNS: tuple[tuple[str, re.Pattern[str], float], ...] = (
    (
        "multi_angle_parallel",
        re.compile(
            r"\b(in\s+parallel|from\s+multiple\s+angles|multiple\s+perspectives|"
            r"fan[- ]?out|across\s+(?:all|multiple)\s+"
            r"(?:modules|services|files|layers|repos|packages|subsystems))\b",
            re.I,
        ),
        0.46,
    ),
    (
        "multi_target_audit",
        re.compile(
            r"\b(audit|review|inspect|compare|benchmark)\b(?:(?!\bfor\b).){0,80}"
            r"\b(?:and|&)\b(?:(?!\bfor\b).){0,80}\b(?:and|&)\b",
            re.I | re.S,
        ),
        0.40,
    ),
    (
        "end_to_end_multi_layer",
        re.compile(
            r"\b(end[- ]to[- ]end|full[- ]stack|frontend\s+and\s+backend|"
            r"across\s+the\s+(?:codebase|repo|repository|stack)|entire\s+repo|"
            r"multi[- ]repo|cross[- ]repo)\b",
            re.I,
        ),
        0.44,
    ),
)

_MUTATING_AGENT_PATTERN = re.compile(
    r"\b(implement|refactor|migrate|rewrite|fix\s+all|update\s+all)\b.{0,120}"
    r"\b(tests?|pytest|ctest|build|ci|verify|commit|pull\s+request|pr\b|"
    r"frontend\s+and\s+backend|across\s+(?:multiple\s+)?files|multiple\s+modules)\b",
    re.I | re.S,
)

_MUTATING_VERB_PATTERN = re.compile(
    r"\b(implement|refactor|migrate|rewrite|fix\s+all|update\s+all|wire\s+up)\b",
    re.I,
)

_MULTI_FILE_PHRASE_PATTERN = re.compile(
    r"\b(multi[- ]?file|cross[- ]?file|cross[- ]?module|all\s+callers|"
    r"every\s+caller|callers\s+and\s+callees|across\s+files)\b",
    re.I,
)

_FILE_REF_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_/.:\\-])(?:(?:[A-Za-z0-9_.-]+[/\\])+[A-Za-z0-9_.-]+\."
    r"(?:py|pyi|cpp|cc|cxx|c|h|hpp|hh|rs|ts|tsx|js|jsx|dart|go|java|kt|cs|sql|yaml|yml|json|toml|md|cmake|ps1|sh)|"
    r"[A-Za-z0-9_][A-Za-z0-9_.-]*\."
    r"(?:py|cpp|cc|cxx|hpp|rs|ts|tsx|js|jsx|dart|go|java|kt|cs|cmake))\b",
)

_NUMBERED_LIST_PATTERN = re.compile(r"(?:^|\n)\s*(?:\d+[.)]|[-*])\s+\S", re.M)

_CODE_INTENT_PATTERN = re.compile(
    r"\b(write\s+(?:a|an|the)?\s*(?:python|c\+\+|cpp|rust|dart|ts|typescript|go|sql|function|class|script|module)|"
    r"implement\s+(?:a|an|the)?\b|code\s+for\b|source\s+file\b|compilable\b|refactor\s+this)\b",
    re.I,
)

_CODE_FENCE_ACTION_PATTERN = re.compile(
    r"\b(fix|implement|refactor|rewrite|optimize|patch|edit|convert)\b",
    re.I,
)

_QUICK_GREETING_PATTERN = re.compile(
    r"^\s*(?:hi|hello|hey|yo|thanks|thank\s+you|ok|okay|cool|nice|ping|test|status|"
    r"what\s+time|who\s+are\s+you)\b",
    re.I,
)

_STANDARD_WORK_PATTERN = re.compile(
    r"\b(write|implement|fix|refactor|convert|translate|summarize|explain|format|"
    r"generate|create|add|remove|edit|debug|regex|sql|json|yaml|script|function|class)\b",
    re.I,
)


@dataclass(frozen=True)
class TierHealthHint:
    """Pure point-in-time health and latency snapshot for one model tier."""

    tier: str
    available: bool = True
    healthy: bool = True
    latency_ms: float | None = None
    slow_threshold_ms: float | None = None
    error_rate: float = 0.0
    reason: str = ""


@dataclass(frozen=True)
class ExecutionModePolicy:
    """Operator policy governing automatic execution-mode routing."""

    mode: ExecutionModeRequest = "off"
    allow_auto_ensemble: bool = True
    allow_auto_fanout: bool = True
    allow_auto_agent: bool = False
    ensemble_tiers: tuple[str, ...] = ("code", "reasoning")
    fanout_scope: str = "local"
    slow_tier_latency_ms: float = DEFAULT_SLOW_TIER_LATENCY_MS
    fast_tier_latency_ms: float = DEFAULT_FAST_TIER_LATENCY_MS
    max_tier_error_rate: float = DEFAULT_MAX_TIER_ERROR_RATE
    degrade_on_slow_tier: bool = True


@dataclass(frozen=True)
class ExecutionContext:
    """Pure snapshot of the request, conversation depth, and local tier capacity."""

    prompt: str
    available_tiers: frozenset[str]
    distinct_ensemble_models: int = 0
    distinct_local_models: int = 0
    developer_authorized: bool = False
    has_image: bool = False
    structured_output: bool = False
    explicit_model_pin: bool = False
    requested_mode: str = ""
    conversation_depth: int = 0
    tier_health: tuple[TierHealthHint, ...] = ()
    tier_models: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ExecutionDecision:
    """Authoritative execution-mode decision returned to callers and receipts."""

    mode: ExecutionMode
    band: DifficultyBand
    difficulty_score: float
    recommended_tiers: tuple[str, ...]
    ensemble_mode: EnsembleMode
    fanout_scope: str
    reason: str
    signals: tuple[str, ...]
    auto_selected: bool
    fallback_tiers: tuple[str, ...] = ()
    degraded_tiers: tuple[str, ...] = ()
    health_degraded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "band": self.band,
            "difficulty_score": round(self.difficulty_score, 3),
            "recommended_tiers": list(self.recommended_tiers),
            "fallback_tiers": list(self.fallback_tiers),
            "degraded_tiers": list(self.degraded_tiers),
            "health_degraded": self.health_degraded,
            "ensemble_mode": self.ensemble_mode,
            "fanout_scope": self.fanout_scope,
            "reason": self.reason,
            "signals": list(self.signals),
            "auto_selected": self.auto_selected,
            "summary": route_summary_line(self),
        }


@dataclass(frozen=True)
class _PromptSegments:
    instruction_text: str
    pasted_log_blocks: int
    pasted_log_chars: int
    pasted_code_blocks: int
    pasted_code_chars: int
    has_code_fence: bool

    @property
    def had_pasted_log(self) -> bool:
        return self.pasted_log_blocks > 0


@dataclass(frozen=True)
class _HealthState:
    hints: Mapping[str, TierHealthHint]
    statuses: Mapping[str, TierHealthStatus]
    healthy_tiers: frozenset[str]
    slow_tiers: frozenset[str]
    unavailable_tiers: frozenset[str]
    usable_tiers: frozenset[str]
    active_multi_tiers: frozenset[str]
    tier_model_map: Mapping[str, str]


def check_execution_mode(raw: Any) -> ExecutionModeRequest | None:
    """Validate an optional execution_mode value or return ``None`` if invalid."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().lower()
    if normalized not in VALID_EXECUTION_MODES:
        return None
    if normalized == "off":
        return "off"
    if normalized == "auto":
        return "auto"
    if normalized == "single":
        return "single"
    if normalized == "ensemble":
        return "ensemble"
    if normalized == "fanout":
        return "fanout"
    return "agent"


def _is_log_line(line: str) -> bool:
    if not line.strip():
        return False
    for pattern in _LOG_LINE_PATTERNS:
        if pattern.search(line):
            return True
    if len(line) >= 500:
        stripped = line.strip()
        if (stripped.startswith("{") and stripped.endswith("}")) or (
            stripped.startswith("[") and stripped.endswith("]")
        ):
            return True
        if len(_INLINE_LOG_MARKER_PATTERN.findall(stripped)) >= 3:
            return True
    return False


def _looks_like_log_block(lines: Sequence[str]) -> bool:
    non_empty = [line for line in lines if line.strip()]
    if not non_empty:
        return False
    if any(_TRACEBACK_HEADER_PATTERN.search(line) for line in non_empty) and any(
        _TRACEBACK_FRAME_PATTERN.search(line) for line in non_empty
    ):
        return True
    log_hits = sum(1 for line in non_empty if _is_log_line(line))
    if log_hits >= 3 and log_hits * 2 >= len(non_empty):
        return True
    if len(non_empty) == 1 and len(non_empty[0]) >= 500 and log_hits == 1:
        return True
    return False


def _strip_unfenced_logs(lines: Sequence[str]) -> tuple[list[str], int, int]:
    """Identify and strip unfenced pasted log/traceback runs from *lines*."""
    n = len(lines)
    is_log = [_is_log_line(line) for line in lines]
    mask = [False] * n
    blocks = 0
    chars = 0

    i = 0
    while i < n:
        if not is_log[i]:
            i += 1
            continue
        start = i
        j = i
        matched_lines = 0
        has_traceback_header = False
        has_traceback_frame = False
        while j < n:
            line = lines[j]
            if is_log[j]:
                matched_lines += 1
                if _TRACEBACK_HEADER_PATTERN.search(line):
                    has_traceback_header = True
                if _TRACEBACK_FRAME_PATTERN.search(line):
                    has_traceback_frame = True
                j += 1
                continue
            # Allow indented stack/source continuation lines or exception tail inside a log run.
            if line.startswith(("  ", "\t")) and line.strip():
                j += 1
                continue
            if _EXCEPTION_LINE_PATTERN.search(line) and (has_traceback_frame or matched_lines >= 2):
                matched_lines += 1
                j += 1
                continue
            # Allow a single blank line bridging two log lines.
            if not line.strip() and j + 1 < n and is_log[j + 1]:
                j += 1
                continue
            break

        # Trim trailing indented non-log lines if this wasn't a traceback tail.
        end = j
        while end > start and not lines[end - 1].strip():
            end -= 1

        is_single_huge_blob = (
            matched_lines == 1
            and end - start == 1
            and len(lines[start]) >= 500
        )
        is_traceback_run = has_traceback_header and has_traceback_frame
        if matched_lines >= 3 or is_traceback_run or is_single_huge_blob:
            blocks += 1
            for k in range(start, end):
                mask[k] = True
                chars += len(lines[k]) + 1
        i = max(j, i + 1)

    kept = [lines[idx] for idx in range(n) if not mask[idx]]
    return kept, blocks, chars


def _segment_prompt(text: str) -> _PromptSegments:
    """Separate user instruction prose from fenced payloads and pasted logs."""
    lines = text.splitlines()
    prose_lines: list[str] = []
    pasted_log_blocks = 0
    pasted_log_chars = 0
    pasted_code_blocks = 0
    pasted_code_chars = 0
    has_code_fence = False

    i = 0
    n = len(lines)
    while i < n:
        match = _FENCE_OPEN_PATTERN.match(lines[i])
        if not match:
            prose_lines.append(lines[i])
            i += 1
            continue
        marker = match.group("marker")
        lang = (match.group("lang") or "").strip().lower()
        fence_char = marker[0]
        fence_len = len(marker)
        close_re = re.compile(rf"^\s*{re.escape(fence_char)}{{{fence_len},}}\s*$")
        body_lines: list[str] = []
        i += 1
        while i < n and not close_re.match(lines[i]):
            body_lines.append(lines[i])
            i += 1
        if i < n:
            i += 1  # consume closing fence
        body_char_len = sum(len(line) + 1 for line in body_lines)
        if lang in _LOG_FENCE_LANGS or _looks_like_log_block(body_lines):
            pasted_log_blocks += 1
            pasted_log_chars += body_char_len
        else:
            pasted_code_blocks += 1
            pasted_code_chars += body_char_len
            if lang in _CODE_FENCE_LANGS or lang == "":
                has_code_fence = True

    cleaned_lines, unfenced_blocks, unfenced_chars = _strip_unfenced_logs(prose_lines)
    instruction_text = "\n".join(cleaned_lines).strip()
    return _PromptSegments(
        instruction_text=instruction_text,
        pasted_log_blocks=pasted_log_blocks + unfenced_blocks,
        pasted_log_chars=pasted_log_chars + unfenced_chars,
        pasted_code_blocks=pasted_code_blocks,
        pasted_code_chars=pasted_code_chars,
        has_code_fence=has_code_fence,
    )


def _count_prose_questions(text: str) -> int:
    stripped = _URL_PATTERN.sub(" ", text)
    stripped = _INLINE_CODE_PATTERN.sub(" ", stripped)
    return stripped.count("?")


def _distinct_file_references(text: str) -> tuple[str, ...]:
    stripped = _URL_PATTERN.sub(" ", text)
    found: list[str] = []
    seen: set[str] = set()
    for match in _FILE_REF_PATTERN.finditer(stripped):
        candidate = match.group(0).replace("\\", "/").lower()
        if candidate not in seen:
            seen.add(candidate)
            found.append(candidate)
    return tuple(found)


def score_difficulty(
    prompt: str,
    *,
    conversation_depth: int = 0,
) -> tuple[DifficultyBand, float, tuple[str, ...], EnsembleMode, bool]:
    """Score prompt difficulty deterministically.

    Returns ``(band, score_0_to_1, signals, ensemble_mode, wants_mutating_agent)``.
    """
    text = (prompt or "").strip()
    if not text:
        return "quick", 0.0, ("empty_prompt",), "prose", False

    depth = max(0, int(conversation_depth)) if not isinstance(conversation_depth, bool) else 0
    segments = _segment_prompt(text)
    instruction = segments.instruction_text

    # Raw pasted log or raw code block with zero surrounding instruction:
    # keep in standard single-model execution instead of falsely escalating.
    if not instruction:
        if segments.had_pasted_log:
            return "standard", 0.35, ("pasted_log_only",), "prose", False
        return (
            "standard",
            0.35,
            ("raw_code_snippet",),
            "code" if segments.has_code_fence else "prose",
            False,
        )

    score = 0.0
    signals: list[str] = []
    if segments.had_pasted_log:
        signals.append("pasted_log_dampened")

    hard_hits = 0
    for name, pattern, weight in _HARD_REASONING_PATTERNS:
        matches = {m.group(0).strip().lower() for m in pattern.finditer(instruction) if m.group(0).strip()}
        if not matches:
            continue
        hard_hits += 1
        score += weight
        signals.append(name)
        if len(matches) >= 3:
            score += 0.26
            signals.append(f"{name}:deep")
        elif len(matches) == 2:
            score += 0.16
            signals.append(f"{name}:multi")

    multi_hits = 0
    for name, pattern, weight in _MULTI_SCOPE_PATTERNS:
        if pattern.search(instruction):
            multi_hits += 1
            score += weight
            signals.append(name)

    file_refs = _distinct_file_references(instruction)
    file_ref_count = len(file_refs)
    if file_ref_count >= 3:
        score += 0.30
        signals.append(f"multi_file_refs:{file_ref_count}")
    elif file_ref_count == 2:
        score += 0.18
        signals.append("multi_file_refs:2")
    elif _MULTI_FILE_PHRASE_PATTERN.search(instruction):
        score += 0.20
        signals.append("multi_file_scope")

    wants_mutating_agent = bool(_MUTATING_AGENT_PATTERN.search(instruction)) or (
        file_ref_count >= 3 and bool(_MUTATING_VERB_PATTERN.search(instruction))
    )
    if wants_mutating_agent:
        score += 0.42
        signals.append("multi_file_mutating_work")

    bullet_count = len(_NUMBERED_LIST_PATTERN.findall(instruction))
    if bullet_count >= 3:
        score += 0.34
        signals.append(f"structured_subitems:{bullet_count}")
    elif bullet_count == 2:
        score += 0.16
        signals.append("structured_subitems:2")

    char_len = len(instruction)
    if char_len >= 1400:
        score += 0.30
        signals.append("very_long_prompt")
    elif char_len >= 600:
        score += 0.22
        signals.append("long_prompt")
    elif char_len >= 240:
        score += 0.12
        signals.append("medium_prompt")

    question_marks = _count_prose_questions(instruction)
    if question_marks >= 3:
        score += 0.18
        signals.append(f"multi_question:{question_marks}")

    has_domain_signal = (
        hard_hits > 0
        or multi_hits > 0
        or wants_mutating_agent
        or file_ref_count >= 2
        or "multi_file_scope" in signals
    )
    has_attached_payload = segments.had_pasted_log or segments.pasted_code_blocks > 0
    is_quick_greeting = (
        char_len <= 90
        and not has_domain_signal
        and not has_attached_payload
        and file_ref_count == 0
        and bullet_count == 0
        and question_marks < 3
        and bool(_QUICK_GREETING_PATTERN.search(instruction))
    )

    # Conversation-depth calibration: never escalate pure greetings/acks,
    # while reflecting accumulated context in deep technical threads.
    if depth >= 10 and not is_quick_greeting:
        score += 0.16
        signals.append("deep_conversation:high")
    elif depth >= 4 and not is_quick_greeting:
        score += 0.08
        signals.append("deep_conversation:moderate")

    if score == 0.0:
        if (
            char_len <= 90
            and not has_attached_payload
            and file_ref_count == 0
            and (
                _QUICK_GREETING_PATTERN.search(instruction)
                or not _STANDARD_WORK_PATTERN.search(instruction)
            )
        ):
            score = 0.12
            signals.append("short_simple_prompt")
        else:
            score = 0.35
            signals.append("standard_task")
    elif not has_domain_signal:
        if is_quick_greeting:
            score = 0.12
            signals.append("short_simple_prompt")
        else:
            # Pure length, conversation depth, or attached logs without domain
            # difficulty signals stay in standard unless explicitly multi-part.
            if score < 0.30:
                score = 0.35
            if bullet_count < 3 and question_marks < 3:
                score = min(score, 0.55)

    clamped = max(0.0, min(1.0, round(score, 3)))

    has_multi_scope_signal = (
        multi_hits >= 1
        or wants_mutating_agent
        or file_ref_count >= 3
        or bullet_count >= 3
        or question_marks >= 3
    )

    if has_multi_scope_signal and (
        (multi_hits >= 1 and (hard_hits >= 1 or bullet_count >= 2 or char_len >= 120 or file_ref_count >= 2))
        or (
            wants_mutating_agent
            and (
                multi_hits >= 1
                or hard_hits >= 1
                or bullet_count >= 2
                or char_len >= 80
                or file_ref_count >= 3
            )
        )
        or (file_ref_count >= 3 and hard_hits >= 1)
        or (bullet_count >= 3 and hard_hits >= 1)
        or (question_marks >= 3 and hard_hits >= 1)
        or clamped >= 0.85
    ):
        band: DifficultyBand = "multi_scope"
        clamped = max(clamped, 0.85)
    elif clamped >= 0.60 or hard_hits >= 2:
        band = "hard"
        clamped = max(clamped, 0.60)
    elif clamped >= 0.25:
        band = "standard"
    else:
        band = "quick"

    wants_code_ensemble = bool(_CODE_INTENT_PATTERN.search(instruction)) or (
        segments.has_code_fence and bool(_CODE_FENCE_ACTION_PATTERN.search(instruction))
    )
    ensemble_mode: EnsembleMode = "code" if wants_code_ensemble else "prose"
    return band, clamped, tuple(signals), ensemble_mode, wants_mutating_agent


def classify_tier_health(
    hint: TierHealthHint,
    policy: ExecutionModePolicy,
) -> TierHealthStatus:
    """Classify one tier's health snapshot into ``healthy``, ``slow``, or ``unavailable``."""
    tier = (hint.tier or "").strip().lower()
    if not hint.available or not hint.healthy:
        return "unavailable"
    max_err = policy.max_tier_error_rate
    if not math.isfinite(hint.error_rate) or hint.error_rate >= max_err:
        return "unavailable"
    if hint.latency_ms is not None:
        if not math.isfinite(hint.latency_ms):
            return "slow"
        if (
            hint.slow_threshold_ms is not None
            and math.isfinite(hint.slow_threshold_ms)
            and hint.slow_threshold_ms > 0.0
        ):
            threshold = hint.slow_threshold_ms
        elif tier in ("fast", "draft"):
            threshold = policy.fast_tier_latency_ms
        else:
            threshold = policy.slow_tier_latency_ms
        if threshold > 0.0 and hint.latency_ms > threshold:
            return "slow"
    return "healthy"


def _build_health_state(
    available: frozenset[str],
    ctx: ExecutionContext,
    policy: ExecutionModePolicy,
) -> _HealthState:
    hints: dict[str, TierHealthHint] = {}
    for raw_hint in ctx.tier_health:
        tier = (raw_hint.tier or "").strip().lower()
        if tier:
            hints[tier] = raw_hint

    statuses: dict[str, TierHealthStatus] = {}
    for tier in available | set(hints.keys()):
        hint = hints.get(tier)
        statuses[tier] = classify_tier_health(hint, policy) if hint is not None else "healthy"

    healthy = frozenset(t for t in available if statuses.get(t) == "healthy")
    slow = frozenset(t for t in available if statuses.get(t) == "slow")
    unavailable = frozenset(
        t for t, st in statuses.items() if st == "unavailable"
    )
    usable = healthy | slow
    active_multi = healthy if policy.degrade_on_slow_tier else usable

    tier_model_map: dict[str, str] = {}
    for raw_tier, raw_model in ctx.tier_models:
        t = (raw_tier or "").strip().lower()
        m = (raw_model or "").strip().casefold()
        if t and m:
            tier_model_map[t] = m

    return _HealthState(
        hints=hints,
        statuses=statuses,
        healthy_tiers=healthy,
        slow_tiers=slow,
        unavailable_tiers=unavailable,
        usable_tiers=usable,
        active_multi_tiers=active_multi,
        tier_model_map=tier_model_map,
    )


def _latency_sort_key(tier: str, order_idx: int, health: _HealthState) -> tuple[float, int]:
    hint = health.hints.get(tier)
    if hint is not None and hint.latency_ms is not None and math.isfinite(hint.latency_ms) and hint.latency_ms >= 0.0:
        return (hint.latency_ms, order_idx)
    return (math.inf, order_idx)


def _describe_tier_degradation(tiers: Sequence[str], health: _HealthState) -> str:
    parts: list[str] = []
    for tier in tiers:
        status = health.statuses.get(tier, "unavailable")
        hint = health.hints.get(tier)
        if status == "slow" and hint is not None and hint.latency_ms is not None and math.isfinite(hint.latency_ms):
            detail = f"{tier} slow ({int(round(hint.latency_ms))}ms)"
        elif hint is not None and hint.reason.strip():
            detail = f"{tier} {status} ({hint.reason.strip()})"
        else:
            detail = f"{tier} {status}"
        parts.append(detail)
    return ", ".join(parts)


def _pick_single_tier(
    available: frozenset[str],
    preferred: tuple[str, ...],
    health: _HealthState | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], bool]:
    """Select primary single tier plus ordered fallback and degradation telemetry."""
    if not available:
        degraded_unavail = tuple(sorted(health.unavailable_tiers)) if health is not None else ()
        return ("code",), (), degraded_unavail, bool(degraded_unavail)

    ordered: list[str] = [t for t in preferred if t in available]
    ordered.extend(t for t in sorted(available) if t not in ordered)

    if health is None:
        return (ordered[0],), tuple(ordered[1:]), (), False

    healthy_list = [t for t in ordered if t in health.healthy_tiers]
    slow_indexed = [(t, idx) for idx, t in enumerate(ordered) if t in health.slow_tiers]
    slow_indexed.sort(key=lambda item: _latency_sort_key(item[0], item[1], health))
    slow_list = [t for t, _ in slow_indexed]
    unavail_list = [t for t in ordered if t in health.unavailable_tiers]

    if healthy_list:
        primary = healthy_list[0]
        fallbacks = tuple(healthy_list[1:] + slow_list)
        primary_pref_idx = preferred.index(primary) if primary in preferred else len(preferred)
        skipped: list[str] = []
        for pref_tier in preferred[:primary_pref_idx]:
            if pref_tier in health.unavailable_tiers or pref_tier in health.slow_tiers:
                skipped.append(pref_tier)
        return (primary,), fallbacks, tuple(skipped), bool(skipped)

    if slow_list:
        primary = slow_list[0]
        fallbacks = tuple(slow_list[1:])
        degraded = list(dict.fromkeys([t for t in preferred if t in health.unavailable_tiers] + [primary]))
        return (primary,), fallbacks, tuple(degraded), True

    # Every available tier is marked unavailable: return best-effort preference
    # tier while flagging health_degraded=True and empty usable fallbacks.
    primary = unavail_list[0] if unavail_list else ordered[0]
    return (primary,), (), tuple(unavail_list or [primary]), True


def _bound_ensemble_tiers(
    available: frozenset[str],
    configured: tuple[str, ...],
    health: _HealthState | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], bool]:
    """Select active multi-model tiers plus fallbacks and degraded configured tiers."""
    pool = health.active_multi_tiers if health is not None else available
    model_map = health.tier_model_map if health is not None else {}

    selected: list[str] = []
    seen_models: set[str] = set()

    def _try_add(tier: str) -> None:
        if tier not in pool or tier in selected:
            return
        model = model_map.get(tier)
        if model:
            if model in seen_models:
                return
            seen_models.add(model)
        selected.append(tier)

    degraded: list[str] = []

    def _record_if_degraded(tier: str) -> None:
        if health is None:
            return
        if health.statuses.get(tier) in ("slow", "unavailable") and tier not in degraded:
            degraded.append(tier)

    for tier in configured:
        _record_if_degraded(tier)
        _try_add(tier)

    fallback_order = ("code", "reasoning", "general", "creative", "fast")
    if len(selected) < 2:
        for tier in fallback_order:
            if tier in available:
                _record_if_degraded(tier)
            _try_add(tier)
            if len(selected) >= 2:
                break
    if len(selected) < 2:
        for tier in sorted(pool):
            _try_add(tier)
            if len(selected) >= 2:
                break

    if not selected:
        single_tiers, single_fallbacks, single_degraded, single_hd = _pick_single_tier(
            available, ("code", "general"), health
        )
        merged_degraded = tuple(dict.fromkeys(degraded + list(single_degraded)))
        return single_tiers, single_fallbacks, merged_degraded, bool(merged_degraded) or single_hd

    # Build ordered fallback tiers from remaining usable tiers not in selected.
    remaining_healthy: list[str] = []
    remaining_slow: list[tuple[str, int]] = []
    ordered_rest = [t for t in fallback_order if t in available and t not in selected]
    ordered_rest.extend(t for t in sorted(available) if t not in selected and t not in ordered_rest)
    for idx, tier in enumerate(ordered_rest):
        if health is None or tier in health.healthy_tiers:
            remaining_healthy.append(tier)
        elif tier in health.slow_tiers:
            remaining_slow.append((tier, idx))
    if health is not None:
        remaining_slow.sort(key=lambda item: _latency_sort_key(item[0], item[1], health))
    fallbacks = tuple(remaining_healthy + [t for t, _ in remaining_slow])

    return tuple(selected), fallbacks, tuple(degraded), bool(degraded)


def _effective_multi_model_counts(
    ctx: ExecutionContext,
    policy: ExecutionModePolicy,
    health: _HealthState,
) -> tuple[int, int]:
    """Compute healthy distinct ensemble and local model counts."""
    if health.tier_model_map:
        active = health.active_multi_tiers
        ens_models = {
            health.tier_model_map[t]
            for t in policy.ensemble_tiers
            if t in active and t in health.tier_model_map
        }
        all_models = {
            health.tier_model_map[t]
            for t in active
            if t in health.tier_model_map
        }
        if len(ens_models) < 2 and len(all_models) >= 2:
            ens_models = set(all_models)
        eff_ens = (
            min(ctx.distinct_ensemble_models, len(ens_models))
            if ctx.distinct_ensemble_models > 0
            else len(ens_models)
        )
        eff_local = (
            min(ctx.distinct_local_models, len(all_models))
            if ctx.distinct_local_models > 0
            else len(all_models)
        )
        return eff_ens, eff_local

    if health.unavailable_tiers or (policy.degrade_on_slow_tier and health.slow_tiers):
        cap = len(health.active_multi_tiers)
        return min(ctx.distinct_ensemble_models, cap), min(ctx.distinct_local_models, cap)

    return ctx.distinct_ensemble_models, ctx.distinct_local_models


def route_summary_line(
    decision: ExecutionDecision | Mapping[str, Any],
    *,
    include_signals: bool = False,
    max_signals: int = 3,
    include_reason: bool = False,
) -> str:
    """Format a compact, single-line ASCII telemetry summary for UI or receipt display.

    Accepts either an :class:`ExecutionDecision` instance or its serialized
    ``to_dict()`` mapping (e.g. ``sonder_receipt["execution_route"]``).
    """
    mode: str
    band: str
    ensemble_mode: str
    if isinstance(decision, ExecutionDecision):
        mode = decision.mode
        band = decision.band
        raw_score: Any = decision.difficulty_score
        recommended = decision.recommended_tiers
        fallback = decision.fallback_tiers
        degraded = decision.degraded_tiers
        health_degraded = decision.health_degraded
        ensemble_mode = decision.ensemble_mode
        fanout_scope = decision.fanout_scope
        auto_selected = decision.auto_selected
        signals: Sequence[str] = decision.signals
        reason = decision.reason
    else:
        mode = str(decision.get("mode", "single") or "single").strip().lower()
        band = str(decision.get("band", "standard") or "standard").strip().lower()
        raw_score = decision.get("difficulty_score", 0.0)
        rec_raw = decision.get("recommended_tiers", ())
        recommended = (
            tuple(str(t).strip() for t in rec_raw if str(t).strip())
            if isinstance(rec_raw, (list, tuple))
            else ()
        )
        fb_raw = decision.get("fallback_tiers", ())
        fallback = (
            tuple(str(t).strip() for t in fb_raw if str(t).strip())
            if isinstance(fb_raw, (list, tuple))
            else ()
        )
        deg_raw = decision.get("degraded_tiers", ())
        degraded = (
            tuple(str(t).strip() for t in deg_raw if str(t).strip())
            if isinstance(deg_raw, (list, tuple))
            else ()
        )
        health_degraded = bool(decision.get("health_degraded", False))
        ensemble_mode = str(decision.get("ensemble_mode", "prose") or "prose").strip().lower()
        fanout_scope = str(decision.get("fanout_scope", "local") or "local").strip().lower()
        auto_selected = bool(decision.get("auto_selected", False))
        sig_raw = decision.get("signals", ())
        signals = (
            tuple(str(s).strip() for s in sig_raw if str(s).strip())
            if isinstance(sig_raw, (list, tuple))
            else ()
        )
        reason = str(decision.get("reason", "") or "")

    try:
        numeric_score = 0.0 if isinstance(raw_score, bool) else float(raw_score)
    except (TypeError, ValueError):
        numeric_score = 0.0
    if not math.isfinite(numeric_score):
        numeric_score = 0.0
    display_score = max(0.0, min(1.0, numeric_score))

    origin = "auto" if auto_selected else "manual"
    tier_text = "+".join(recommended)
    if mode == "ensemble" and ensemble_mode == "code":
        mode_part = f"ensemble:code[{tier_text}]" if tier_text else "ensemble:code"
    elif mode == "fanout" and fanout_scope:
        mode_part = f"fanout:{fanout_scope}[{tier_text}]" if tier_text else f"fanout:{fanout_scope}"
    else:
        mode_part = f"{mode}[{tier_text}]" if tier_text else mode

    parts: list[str] = [
        origin,
        f"{band} ({display_score:.2f})",
        mode_part,
    ]
    if health_degraded or degraded:
        deg_label = ",".join(degraded) if degraded else "yes"
        parts.append(f"degraded:{deg_label}")
        if fallback:
            parts.append(f"fallback:{','.join(fallback[:2])}")

    if include_signals and signals and max_signals > 0:
        shown = list(signals[:max_signals])
        extra = len(signals) - len(shown)
        sig_text = ",".join(shown) + (f"+{extra}" if extra > 0 else "")
        parts.append(f"signals:{sig_text}")

    if include_reason and reason.strip():
        clean_reason = " ".join(reason.split())
        parts.append(clean_reason)

    return " | ".join(parts)


def plan_execution_mode(
    ctx: ExecutionContext,
    policy: ExecutionModePolicy,
) -> ExecutionDecision:
    """Compute the execution mode for a single request under *policy*."""
    band, score, signals, ensemble_mode, wants_mutating_agent = score_difficulty(
        ctx.prompt,
        conversation_depth=ctx.conversation_depth,
    )
    available = frozenset(t.strip().lower() for t in ctx.available_tiers if t)
    health = _build_health_state(available, ctx, policy)
    eff_ensemble_models, eff_local_models = _effective_multi_model_counts(ctx, policy, health)
    ens_tiers, ens_fallbacks, ens_degraded, ens_health_degraded = _bound_ensemble_tiers(
        available, policy.ensemble_tiers, health
    )
    fanout_scope = (policy.fanout_scope or "local").strip().lower() or "local"

    req_mode = check_execution_mode(ctx.requested_mode) if ctx.requested_mode else None
    effective_mode: ExecutionModeRequest = req_mode if req_mode is not None else policy.mode

    def _single_decision(
        preferred: tuple[str, ...],
        base_reason: str,
        *,
        auto_selected: bool,
        extra_degraded: tuple[str, ...] = (),
        force_health_degraded: bool = False,
    ) -> ExecutionDecision:
        rec, fallbacks, skipped_deg, single_hd = _pick_single_tier(available, preferred, health)
        merged_deg = tuple(dict.fromkeys(list(extra_degraded) + list(skipped_deg)))
        is_hd = force_health_degraded or single_hd or bool(merged_deg)
        final_reason = base_reason
        if skipped_deg and not force_health_degraded:
            detail = _describe_tier_degradation(skipped_deg, health)
            final_reason = f"{base_reason} (fallback after {detail})"
        return ExecutionDecision(
            mode="single",
            band=band,
            difficulty_score=score,
            recommended_tiers=rec,
            fallback_tiers=fallbacks,
            degraded_tiers=merged_deg,
            health_degraded=is_hd,
            ensemble_mode=ensemble_mode,
            fanout_scope=fanout_scope,
            reason=final_reason,
            signals=signals,
            auto_selected=auto_selected,
        )

    def _annotate_multi_reason(base: str, sep: str = " ") -> str:
        if not (ens_health_degraded and ens_degraded):
            return base
        bypassed = tuple(t for t in ens_degraded if t not in ens_tiers)
        active_slow = tuple(t for t in ens_degraded if t in ens_tiers)
        notes: list[str] = []
        if bypassed:
            notes.append(f"bypassed {_describe_tier_degradation(bypassed, health)}")
        if active_slow:
            notes.append(f"degraded {_describe_tier_degradation(active_slow, health)}")
        if not notes:
            return base
        detail = "; ".join(notes)
        return f"{base} ({detail})" if sep == " " else f"{base}{sep}{detail}"

    # Hard bypasses: never override structured output, images, or pinned models.
    if ctx.structured_output:
        return _single_decision(
            ("code", "general", "reasoning", "fast"),
            "structured response_format requires single-model execution",
            auto_selected=False,
        )
    if ctx.has_image:
        return _single_decision(
            ("vision", "general"),
            "multimodal image input routes to single vision model",
            auto_selected=False,
        )
    if ctx.explicit_model_pin and req_mode not in ("ensemble", "fanout", "agent"):
        return _single_decision(
            ("code", "general", "reasoning", "fast"),
            "caller pinned an explicit model/tier",
            auto_selected=False,
        )

    if effective_mode in ("off", "single"):
        return _single_decision(
            ("code", "general", "reasoning", "fast"),
            (
                "explicit single execution mode requested"
                if req_mode == "single"
                else "auto execution-mode router disabled"
            ),
            auto_selected=False,
        )

    # Explicit non-single caller requests (auth is also enforced by HTTP facade).
    if effective_mode == "ensemble":
        if ctx.developer_authorized and eff_ensemble_models >= 2:
            return ExecutionDecision(
                mode="ensemble",
                band=band,
                difficulty_score=score,
                recommended_tiers=ens_tiers,
                fallback_tiers=ens_fallbacks,
                degraded_tiers=ens_degraded,
                health_degraded=ens_health_degraded,
                ensemble_mode=ensemble_mode,
                fanout_scope=fanout_scope,
                reason=_annotate_multi_reason("explicit ensemble execution mode requested"),
                signals=signals,
                auto_selected=False,
            )
        if not ctx.developer_authorized:
            reason = "ensemble unavailable: developer authorization required"
            extra_deg: tuple[str, ...] = ()
            force_hd = False
        elif ens_health_degraded and ens_degraded:
            reason = (
                "ensemble degraded to single: "
                f"{_describe_tier_degradation(ens_degraded, health)}"
            )
            extra_deg = ens_degraded
            force_hd = True
        else:
            reason = "ensemble degraded to single: fewer than 2 distinct local models"
            extra_deg = ()
            force_hd = False
        return _single_decision(
            ("reasoning", "code", "general", "fast"),
            reason,
            auto_selected=False,
            extra_degraded=extra_deg,
            force_health_degraded=force_hd,
        )

    if effective_mode == "fanout":
        if ctx.developer_authorized and eff_local_models >= 2:
            return ExecutionDecision(
                mode="fanout",
                band=band,
                difficulty_score=score,
                recommended_tiers=ens_tiers,
                fallback_tiers=ens_fallbacks,
                degraded_tiers=ens_degraded,
                health_degraded=ens_health_degraded,
                ensemble_mode=ensemble_mode,
                fanout_scope=fanout_scope,
                reason=_annotate_multi_reason("explicit fanout execution mode requested"),
                signals=signals,
                auto_selected=False,
            )
        if not ctx.developer_authorized:
            reason = "fanout unavailable: developer authorization required"
            extra_deg = ()
            force_hd = False
        elif ens_health_degraded and ens_degraded:
            reason = (
                "fanout degraded to single: "
                f"{_describe_tier_degradation(ens_degraded, health)}"
            )
            extra_deg = ens_degraded
            force_hd = True
        else:
            reason = "fanout degraded to single: fewer than 2 distinct local models"
            extra_deg = ()
            force_hd = False
        return _single_decision(
            ("reasoning", "code", "general", "fast"),
            reason,
            auto_selected=False,
            extra_degraded=extra_deg,
            force_health_degraded=force_hd,
        )

    if effective_mode == "agent":
        if ctx.developer_authorized:
            rec, fallbacks, skipped_deg, single_hd = _pick_single_tier(
                available, ("code", "reasoning", "general", "fast"), health
            )
            agent_reason = "explicit agent execution mode requested"
            if skipped_deg:
                agent_reason = (
                    f"{agent_reason} (fallback after {_describe_tier_degradation(skipped_deg, health)})"
                )
            return ExecutionDecision(
                mode="agent",
                band=band,
                difficulty_score=score,
                recommended_tiers=rec,
                fallback_tiers=fallbacks,
                degraded_tiers=skipped_deg,
                health_degraded=single_hd,
                ensemble_mode=ensemble_mode,
                fanout_scope=fanout_scope,
                reason=agent_reason,
                signals=signals,
                auto_selected=False,
            )
        return _single_decision(
            ("code", "general", "fast"),
            "agent mode unavailable: developer authorization required",
            auto_selected=False,
        )

    # Automatic difficulty-based routing (effective_mode == "auto").
    if band == "quick":
        return _single_decision(
            ("fast", "general", "code", "reasoning"),
            "quick low-complexity prompt routed to fast single tier",
            auto_selected=True,
        )

    if band == "standard":
        return _single_decision(
            ("code", "general", "reasoning", "fast"),
            "standard single-scope prompt routed to primary local tier",
            auto_selected=True,
        )

    if band == "hard":
        if (
            ctx.developer_authorized
            and policy.allow_auto_ensemble
            and eff_ensemble_models >= 2
        ):
            return ExecutionDecision(
                mode="ensemble",
                band=band,
                difficulty_score=score,
                recommended_tiers=ens_tiers,
                fallback_tiers=ens_fallbacks,
                degraded_tiers=ens_degraded,
                health_degraded=ens_health_degraded,
                ensemble_mode=ensemble_mode,
                fanout_scope=fanout_scope,
                reason=_annotate_multi_reason(
                    "hard reasoning prompt auto-routed to parallel multi-model ensemble"
                ),
                signals=signals,
                auto_selected=True,
            )
        if ctx.developer_authorized and policy.allow_auto_ensemble and ens_health_degraded and ens_degraded:
            hard_reason = (
                "hard prompt degraded to single strongest healthy tier: "
                f"{_describe_tier_degradation(ens_degraded, health)}"
            )
            return _single_decision(
                ("reasoning", "code", "general", "fast"),
                hard_reason,
                auto_selected=True,
                extra_degraded=ens_degraded,
                force_health_degraded=True,
            )
        return _single_decision(
            ("reasoning", "code", "general", "fast"),
            (
                "hard prompt degraded to single strongest tier: auto-ensemble unavailable"
                if eff_ensemble_models < 2
                else "hard prompt routed to single strongest tier"
            ),
            auto_selected=True,
        )

    # band == "multi_scope"
    if wants_mutating_agent:
        if ctx.developer_authorized and policy.allow_auto_agent:
            rec, fallbacks, skipped_deg, single_hd = _pick_single_tier(
                available, ("code", "reasoning", "general", "fast"), health
            )
            mut_agent_reason = "multi-scope implementation/verification task auto-routed to /agent"
            if skipped_deg:
                mut_agent_reason = (
                    f"{mut_agent_reason} (fallback after {_describe_tier_degradation(skipped_deg, health)})"
                )
            return ExecutionDecision(
                mode="agent",
                band=band,
                difficulty_score=score,
                recommended_tiers=rec,
                fallback_tiers=fallbacks,
                degraded_tiers=skipped_deg,
                health_degraded=single_hd,
                ensemble_mode=ensemble_mode,
                fanout_scope=fanout_scope,
                reason=mut_agent_reason,
                signals=signals,
                auto_selected=True,
            )
        if (
            ctx.developer_authorized
            and policy.allow_auto_ensemble
            and eff_ensemble_models >= 2
        ):
            return ExecutionDecision(
                mode="ensemble",
                band=band,
                difficulty_score=score,
                recommended_tiers=ens_tiers,
                fallback_tiers=ens_fallbacks,
                degraded_tiers=ens_degraded,
                health_degraded=ens_health_degraded,
                ensemble_mode=ensemble_mode,
                fanout_scope=fanout_scope,
                reason=_annotate_multi_reason(
                    "multi-scope task stepped down to ensemble (auto-agent disabled)",
                    sep="; ",
                ),
                signals=signals,
                auto_selected=True,
            )
        mut_single_reason = "multi-scope task kept on single tier (auto-agent disabled)"
        if ctx.developer_authorized and ens_health_degraded and ens_degraded:
            mut_single_reason = (
                f"{mut_single_reason}; {_describe_tier_degradation(ens_degraded, health)}"
            )
            return _single_decision(
                ("reasoning", "code", "general", "fast"),
                mut_single_reason,
                auto_selected=True,
                extra_degraded=ens_degraded,
                force_health_degraded=True,
            )
        return _single_decision(
            ("reasoning", "code", "general", "fast"),
            mut_single_reason,
            auto_selected=True,
        )

    # Read-only multi-scope prompt: prefer bounded local fanout, then ensemble.
    if (
        ctx.developer_authorized
        and policy.allow_auto_fanout
        and eff_local_models >= 2
    ):
        return ExecutionDecision(
            mode="fanout",
            band=band,
            difficulty_score=score,
            recommended_tiers=ens_tiers,
            fallback_tiers=ens_fallbacks,
            degraded_tiers=ens_degraded,
            health_degraded=ens_health_degraded,
            ensemble_mode=ensemble_mode,
            fanout_scope=fanout_scope,
            reason=_annotate_multi_reason(
                "multi-scope comparative/audit prompt auto-routed to bounded local fanout"
            ),
            signals=signals,
            auto_selected=True,
        )
    if (
        ctx.developer_authorized
        and policy.allow_auto_ensemble
        and eff_ensemble_models >= 2
    ):
        return ExecutionDecision(
            mode="ensemble",
            band=band,
            difficulty_score=score,
            recommended_tiers=ens_tiers,
            fallback_tiers=ens_fallbacks,
            degraded_tiers=ens_degraded,
            health_degraded=ens_health_degraded,
            ensemble_mode=ensemble_mode,
            fanout_scope=fanout_scope,
            reason=_annotate_multi_reason(
                "multi-scope prompt auto-routed to parallel multi-model ensemble"
            ),
            signals=signals,
            auto_selected=True,
        )
    if ctx.developer_authorized and ens_health_degraded and ens_degraded:
        return _single_decision(
            ("reasoning", "code", "general", "fast"),
            (
                "multi-scope prompt degraded to single strongest healthy tier: "
                f"{_describe_tier_degradation(ens_degraded, health)}"
            ),
            auto_selected=True,
            extra_degraded=ens_degraded,
            force_health_degraded=True,
        )
    return _single_decision(
        ("reasoning", "code", "general", "fast"),
        "multi-scope prompt degraded to single strongest tier",
        auto_selected=True,
    )


__all__ = [
    "DEFAULT_FAST_TIER_LATENCY_MS",
    "DEFAULT_MAX_TIER_ERROR_RATE",
    "DEFAULT_SLOW_TIER_LATENCY_MS",
    "DifficultyBand",
    "EnsembleMode",
    "ExecutionContext",
    "ExecutionDecision",
    "ExecutionMode",
    "ExecutionModePolicy",
    "ExecutionModeRequest",
    "TierHealthHint",
    "TierHealthStatus",
    "VALID_EXECUTION_MODES",
    "check_execution_mode",
    "classify_tier_health",
    "plan_execution_mode",
    "route_summary_line",
    "score_difficulty",
]
