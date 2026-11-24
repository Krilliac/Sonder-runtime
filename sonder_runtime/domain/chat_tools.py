"""Pure domain models and OpenAI/Ollama tool schemas for interactive chat turns."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sonder_runtime.domain.agents.delegation import BOARD_MESSAGE_KINDS, DELEGATION_TOOLS
from sonder_runtime.domain.resilience import repair_tool_arguments
from sonder_runtime.domain.security import redaction

TOOL_BACKEND_CONTAINER = "container"
TOOL_BACKEND_HOST = "host"
TOOL_BACKEND_WORKSPACE = "workspace"
TOOL_BACKEND_DISCOVERY = "discovery"

_BIDI_CODEPOINTS: dict[int, str] = {
    0x061C: "U+061C", 0x200B: "U+200B", 0x200C: "U+200C", 0x200D: "U+200D",
    0x200E: "U+200E", 0x200F: "U+200F", 0x202A: "U+202A", 0x202B: "U+202B",
    0x202C: "U+202C", 0x202D: "U+202D", 0x202E: "U+202E", 0x2066: "U+2066",
    0x2067: "U+2067", 0x2068: "U+2068", 0x2069: "U+2069", 0xFEFF: "U+FEFF",
}


def detect_bidi_chars(text: object) -> tuple[bool, list[str]]:
    """Return ``(has_bidi_or_hidden, sorted_codepoint_labels)`` for ``text``."""
    raw = str(text or "")
    if not raw:
        return False, []
    found: set[str] = set()
    for ch in raw:
        code = ord(ch)
        if code in _BIDI_CODEPOINTS:
            found.add(_BIDI_CODEPOINTS[code])
        elif code < 0x20 and ch not in ("\n", "\r", "\t"):
            found.add(f"U+{code:04X}")
    labels = sorted(found)
    return bool(labels), labels


_SENSITIVE_PREVIEW_KEYS = frozenset({
    "token", "approval", "extra_roots", "bypass", "developer_authorized",
})


def preview_tool_arguments(
    tool_name: str,
    args: Mapping[str, Any],
    *,
    max_detail_chars: int = 4000,
) -> dict[str, Any]:
    """Return bounded, secret-free preview + bidi metadata for an approval card."""
    visible_args: dict[str, Any] = {
        str(key): value
        for key, value in args.items()
        if str(key) not in _SENSITIVE_PREVIEW_KEYS
    }
    bidi_texts: list[str] = []
    if tool_name == "run_code":
        bidi_texts.append(str(visible_args.get("code") or ""))
    else:
        bidi_texts.append(json.dumps(visible_args, ensure_ascii=False, sort_keys=True, default=str))
    has_bidi, bidi_chars = detect_bidi_chars("\n".join(bidi_texts))
    redacted_args = redaction.redact_structure(visible_args, sensitive_keys=True)
    detail = ""
    if tool_name == "run_code":
        detail = redaction.redact_text(str(redacted_args.get("code") or ""))
    elif tool_name in {"file_write", "file_edit", "file_delete", "text_patch", "workspace_run"}:
        detail = redaction.redact_text(
            json.dumps(redacted_args, ensure_ascii=False, sort_keys=True, default=str)
        )
    truncated = len(detail) > max_detail_chars
    shown_detail = (
        detail[:max_detail_chars] + f"\n... (truncated {len(detail) - max_detail_chars} of {len(detail)} chars)"
        if truncated
        else detail
    )
    safe_args: dict[str, Any] = {}
    for key, value in redacted_args.items():
        if isinstance(value, str) and len(value) > 512:
            safe_args[key] = value[:512] + "..."
        elif value is None or isinstance(value, (bool, int, float, str)):
            safe_args[key] = value
        else:
            try:
                json.dumps(value, ensure_ascii=False)
                safe_args[key] = value
            except (TypeError, ValueError):
                safe_args[key] = redaction.redact_text(str(value))
    return {
        "arguments": safe_args,
        "detail": shown_detail,
        "detail_truncated": truncated,
        "detail_chars": len(detail),
        "bidi_warning": has_bidi,
        "bidi_chars": list(bidi_chars),
    }


@dataclass(frozen=True)
class ChatToolBudget:
    """Hard bounds for one interactive chat-tool turn."""

    max_runs: int = 3
    max_tool_calls: int = 32
    max_model_calls: int = 10
    wall_seconds: float = 180.0
    model_output_chars: int = 4000
    user_output_chars: int = 12000
    approval_wait_seconds: float = 120.0


DEFAULT_CHAT_TOOL_BUDGET = ChatToolBudget()


@dataclass(frozen=True)
class ChatToolDefinition:
    """One native tool definition advertised to capable chat models."""

    name: str
    description: str
    parameters: Mapping[str, Any]
    backend: str = TOOL_BACKEND_HOST

    def to_ollama_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters),
            },
        }


CORE_CHAT_TOOL_DEFINITIONS: tuple[ChatToolDefinition, ...] = (
    ChatToolDefinition(
        name="run_code",
        description=(
            "Execute a short standalone code snippet (python, js, powershell, cpp, csharp) "
            "when verifying or computing an answer. Never invent execution output."
        ),
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Source code to execute."},
                "language": {
                    "type": "string",
                    "enum": ["python", "js", "powershell", "cpp", "csharp"],
                    "default": "python",
                },
                "stdin": {"type": "string", "default": ""},
                "timeout": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10},
            },
            "required": ["code"],
        },
        backend=TOOL_BACKEND_HOST,
    ),
    ChatToolDefinition(
        name="tool_search",
        description=(
            "Discover additional workspace, git, test, lint, data, or memory tools "
            "and their JSON argument schemas by query or exact tool name."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword such as read, edit, test, git."},
                "name": {"type": "string", "description": "Exact tool name to inspect."},
            },
        },
        backend=TOOL_BACKEND_DISCOVERY,
    ),
    ChatToolDefinition(
        name="file_read",
        description="Read a text file inside an authorized project directory.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "required": ["path"],
        },
        backend=TOOL_BACKEND_WORKSPACE,
    ),
    ChatToolDefinition(
        name="directory_tree",
        description="List files and directories under a project path.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
                "depth": {"type": "integer", "minimum": 1, "maximum": 5, "default": 2},
            },
        },
        backend=TOOL_BACKEND_WORKSPACE,
    ),
    ChatToolDefinition(
        name="text_search",
        description="Search for text or a symbol across project files.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "root": {"type": "string", "default": "."},
                "glob": {"type": "string", "default": ""},
            },
            "required": ["query"],
        },
        backend=TOOL_BACKEND_WORKSPACE,
    ),
    ChatToolDefinition(
        name="file_write",
        description="Create, overwrite, or append a file in an authorized directory (gated).",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "mode": {"type": "string", "enum": ["create", "overwrite", "append"], "default": "create"},
            },
            "required": ["path", "content"],
        },
        backend=TOOL_BACKEND_WORKSPACE,
    ),
    ChatToolDefinition(
        name="file_edit",
        description="Replace a unique string match inside an authorized file (gated).",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old": {"type": "string"},
                "new": {"type": "string"},
            },
            "required": ["path", "old", "new"],
        },
        backend=TOOL_BACKEND_WORKSPACE,
    ),
    ChatToolDefinition(
        name="test_run",
        description="Run repository tests with pytest/jest/vitest/cargo/go/dotnet (gated).",
        parameters={
            "type": "object",
            "properties": {
                "root": {"type": "string", "default": "."},
                "path": {"type": "string", "default": ""},
                "pattern": {"type": "string", "default": ""},
            },
        },
        backend=TOOL_BACKEND_HOST,
    ),
    ChatToolDefinition(
        name="report_progress",
        description="Post a concise status or thinking update to the shared live agent progress board.",
        parameters={
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Short status or milestone update."},
                "thinking": {"type": "string", "default": ""},
                "phase": {"type": "string", "default": "progress"},
            },
            "required": ["summary"],
        },
        backend=TOOL_BACKEND_HOST,
    ),
    ChatToolDefinition(
        name="post_to_board",
        description=(
            "Post a message to the shared peer coordination board (supports @handle mentions "
            "for handoffs, findings, praise, complaints/pushback, or brief jokes)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Message for teammates; use @handle to mention a peer."},
                "kind": {
                    "type": "string",
                    "enum": sorted(BOARD_MESSAGE_KINDS),
                    "default": "chat",
                },
            },
            "required": ["text"],
        },
        backend=TOOL_BACKEND_HOST,
    ),
    ChatToolDefinition(
        name="read_board",
        description="Read recent messages from the shared peer coordination board (optionally filtered by since_seq or mentions).",
        parameters={
            "type": "object",
            "properties": {
                "since_seq": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                "mentions_only": {"type": "boolean", "default": False},
            },
        },
        backend=TOOL_BACKEND_DISCOVERY,
    ),
    ChatToolDefinition(
        name="delegate_task",
        description=(
            "Delegate one bounded subtask to a depth-1 child agent (depth 0 only; "
            "children run independently across Main/Node1 and cannot delegate further)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Subtask instruction for the child agent."},
                "name": {"type": "string", "default": "", "description": "Optional handle such as coder, scout, security, skeptic."},
                "role": {"type": "string", "default": ""},
                "tier": {"type": "string", "default": ""},
                "model": {"type": "string", "default": ""},
                "project": {"type": "string", "default": ""},
                "cwd": {"type": "string", "default": ""},
                "max_steps": {"type": "integer", "minimum": 1, "maximum": 16, "default": 8},
                "read_only": {"type": "boolean"},
            },
            "required": ["task"],
        },
        backend=TOOL_BACKEND_HOST,
    ),
    ChatToolDefinition(
        name="spawn_agents",
        description=(
            "Spawn multiple bounded depth-1 child agents in parallel across Main and Node1 "
            "(depth 0 only; children coordinate via the peer board and cannot delegate further)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "List of task strings or {task, name, role, personality, tier, model, project, cwd, max_steps, read_only} objects.",
                    "items": {"type": "object"},
                },
                "tier": {"type": "string", "default": ""},
                "model": {"type": "string", "default": ""},
                "project": {"type": "string", "default": ""},
                "cwd": {"type": "string", "default": ""},
                "max_steps": {"type": "integer", "minimum": 1, "maximum": 16, "default": 8},
                "read_only": {"type": "boolean"},
            },
            "required": ["tasks"],
        },
        backend=TOOL_BACKEND_HOST,
    ),
    ChatToolDefinition(
        name="math_eval",
        description=(
            "Exact/high-precision calculator, equation & linear-system solver, matrix/vector algebra, "
            "number theory, statistics/regression, unit conversion, and date math (pure, no approval needed)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "expression": {"type": "string", "default": "", "description": "Math expression or equation (e.g. 'comb(52,5)', '2*x + 5 = 17')."},
                "action": {
                    "type": "string",
                    "enum": ["eval", "solve", "matrix", "convert", "date", "derivative", "integrate"],
                    "default": "eval",
                },
                "exact": {"type": "boolean", "default": False, "description": "Use exact Fraction rational arithmetic."},
                "precision": {"type": "integer", "minimum": 0, "maximum": 50, "default": 0},
                "variable": {"type": "string", "default": "x"},
                "equations": {"type": "array", "items": {"type": "string"}},
                "matrix_op": {"type": "string", "default": ""},
                "matrix_a": {"type": "array"},
                "matrix_b": {"type": "array"},
                "value": {"type": "number"},
                "from_unit": {"type": "string", "default": ""},
                "to_unit": {"type": "string", "default": ""},
                "date_a": {"type": "string", "default": ""},
                "date_b": {"type": "string", "default": ""},
                "add_days": {"type": "integer"},
                "at": {"type": "number", "default": 0.0},
                "low": {"type": "number", "default": 0.0},
                "high": {"type": "number", "default": 1.0},
            },
        },
        backend=TOOL_BACKEND_DISCOVERY,
    ),
    ChatToolDefinition(
        name="logic_verify",
        description=(
            "Formal Boolean SAT solver, truth table, equivalence/implication prover with counterexamples, "
            "CNF simplifier, and finite-domain constraint solver (CSP)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "formula": {"type": "string", "default": "", "description": "Propositional formula using and/or/not/xor/->/<->."},
                "action": {
                    "type": "string",
                    "enum": ["satisfiable", "truth_table", "equivalent", "implies", "cnf", "csp"],
                    "default": "satisfiable",
                },
                "formula_b": {"type": "string", "default": ""},
                "premises": {"type": "array", "items": {"type": "string"}},
                "conclusion": {"type": "string", "default": ""},
                "domains": {"type": "object"},
                "constraints": {"type": "array", "items": {"type": "object"}},
            },
        },
        backend=TOOL_BACKEND_DISCOVERY,
    ),
    ChatToolDefinition(
        name="regex_lab",
        description=(
            "Test a regular expression against text or positive/negative cases, inspect capture groups "
            "and substitutions, and statically detect catastrophic backtracking (ReDoS) hazards."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "text": {"type": "string", "default": ""},
                "samples": {"type": "array", "items": {"type": "string"}},
                "should_match": {"type": "array", "items": {"type": "string"}},
                "should_not_match": {"type": "array", "items": {"type": "string"}},
                "replacement": {"type": "string"},
                "flags": {"type": "string", "default": ""},
                "mode": {"type": "string", "enum": ["search", "match", "fullmatch", "findall"], "default": "search"},
            },
            "required": ["pattern"],
        },
        backend=TOOL_BACKEND_DISCOVERY,
    ),
    ChatToolDefinition(
        name="code_graph",
        description=(
            "Analyze repository module dependencies, import cycles (Tarjan SCC), change blast-radius "
            "impact, and Python call/inheritance graphs in one read-only pass."
        ),
        parameters={
            "type": "object",
            "properties": {
                "root": {"type": "string", "default": "."},
                "action": {"type": "string", "enum": ["summary", "impact", "cycles", "calls"], "default": "summary"},
                "target": {"type": "string", "default": ""},
                "max_depth": {"type": "integer", "minimum": 1, "maximum": 20, "default": 6},
            },
        },
        backend=TOOL_BACKEND_WORKSPACE,
    ),
    ChatToolDefinition(
        name="diff_semantic",
        description=(
            "AST-aware Python diff (ignores formatting/whitespace), deep JSON/TOML/INI structural diff, "
            "or symbol-signature diff between two guarded files or two inline strings."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path_a": {"type": "string", "default": ""},
                "path_b": {"type": "string", "default": ""},
                "text_a": {"type": "string", "default": ""},
                "text_b": {"type": "string", "default": ""},
                "kind": {"type": "string", "enum": ["auto", "python", "json", "toml", "ini", "generic"], "default": "auto"},
            },
        },
        backend=TOOL_BACKEND_WORKSPACE,
    ),
    ChatToolDefinition(
        name="reasoning_board",
        description=(
            "Structured session scratchpad for tracking grounded facts, hypotheses (open/supported/refuted/confirmed), "
            "decisions, and contradiction audits across multi-step investigations."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["init", "add_fact", "add_hypothesis", "update_hypothesis", "add_decision", "audit", "summary", "clear"],
                    "default": "summary",
                },
                "board_id": {"type": "string", "default": "default"},
                "goal": {"type": "string", "default": ""},
                "text": {"type": "string", "default": ""},
                "source": {"type": "string", "default": ""},
                "hypothesis_id": {"type": "string", "default": ""},
                "status": {"type": "string", "default": ""},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "evidence_for": {"type": "string", "default": ""},
                "evidence_against": {"type": "string", "default": ""},
                "fact_refs": {"type": "array", "items": {"type": "string"}},
                "alternatives": {"type": "array", "items": {"type": "string"}},
            },
        },
        backend=TOOL_BACKEND_DISCOVERY,
    ),
    ChatToolDefinition(
        name="memory_search",
        description="Search Sonder's local memory for previously learned facts, lessons, and prior solutions.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            },
            "required": ["query"],
        },
        backend=TOOL_BACKEND_DISCOVERY,
    ),
    ChatToolDefinition(
        name="repository_symbol_index",
        description="Build a bounded declaration index of classes, functions, and symbols across project files.",
        parameters={
            "type": "object",
            "properties": {
                "root": {"type": "string", "default": "."},
                "query": {"type": "string", "default": ""},
            },
        },
        backend=TOOL_BACKEND_WORKSPACE,
    ),
    ChatToolDefinition(
        name="code_metrics",
        description="Compute language LOC, TODO/FIXME markers, largest files, and complexity hotspots.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
                "top_n": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
        },
        backend=TOOL_BACKEND_WORKSPACE,
    ),
    ChatToolDefinition(
        name="claim_check",
        description="Verify a code citation or claim against the actual repository file, line, symbols, and guards.",
        parameters={
            "type": "object",
            "properties": {
                "root": {"type": "string", "default": "."},
                "file": {"type": "string", "default": ""},
                "line": {"type": "integer"},
                "symbols": {"type": "string", "default": ""},
                "claim_text": {"type": "string", "default": ""},
            },
            "required": ["root"],
        },
        backend=TOOL_BACKEND_WORKSPACE,
    ),
    ChatToolDefinition(
        name="schema_validate",
        description="Validate a guarded JSON, JSONL, or TOML file against a JSON Schema file or inline schema_json.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "schema_path": {"type": "string", "default": ""},
                "schema_json": {"type": "string", "default": ""},
            },
            "required": ["path"],
        },
        backend=TOOL_BACKEND_WORKSPACE,
    ),
    ChatToolDefinition(
        name="symbol_verify",
        description=(
            "Verify that a Python module, attribute, method, or keyword argument actually exists in "
            "stdlib/environment/repository, or audit a Python code snippet for hallucinated APIs."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["verify_symbol", "audit_code"], "default": "verify_symbol"},
                "module": {"type": "string", "default": ""},
                "symbol": {"type": "string", "default": ""},
                "kwargs": {"type": "array", "items": {"type": "string"}},
                "code": {"type": "string", "default": ""},
                "root": {"type": "string", "default": ""},
            },
        },
        backend=TOOL_BACKEND_DISCOVERY,
    ),
    ChatToolDefinition(
        name="citation_audit",
        description=(
            "Audit file:line citations, quoted code/doc snippets, and source-grounded claims against "
            "real guarded files or supplied source texts to catch fabricated quotes and dead line numbers."
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "default": ""},
                "root": {"type": "string", "default": ""},
                "claims": {"type": "array", "items": {"type": "object"}},
                "sources": {"type": "array", "items": {"type": "string"}},
            },
        },
        backend=TOOL_BACKEND_WORKSPACE,
    ),
    ChatToolDefinition(
        name="fact_cross_check",
        description=(
            "Cross-check draft prose for inline arithmetic errors, Markdown table total mismatches, "
            "invalid dates or wrong weekdays, list count mismatches, and direct self-contradictions."
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
            },
            "required": ["text"],
        },
        backend=TOOL_BACKEND_DISCOVERY,
    ),
    ChatToolDefinition(
        name="consistency_check",
        description=(
            "Compare 2-8 candidate answers for consensus vs contested claims, or audit a draft reply "
            "against grounded tool execution receipts to catch fabricated pass/write claims."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["compare_candidates", "audit_against_receipts"], "default": "compare_candidates"},
                "candidates": {"type": "array", "items": {"type": "string"}},
                "reply": {"type": "string", "default": ""},
                "receipts": {"type": "array", "items": {"type": "object"}},
            },
        },
        backend=TOOL_BACKEND_DISCOVERY,
    ),
    ChatToolDefinition(
        name="plan_fallback",
        description=(
            "When a tool, command, or method fails or is unavailable, classify the failure and return "
            "ranked, policy-safe alternative tools and concrete fallback strategies to finish the task."
        ),
        parameters={
            "type": "object",
            "properties": {
                "failed_tool": {"type": "string"},
                "error": {"type": "string", "default": ""},
                "goal": {"type": "string", "default": ""},
                "tried_tools": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["failed_tool"],
        },
        backend=TOOL_BACKEND_DISCOVERY,
    ),
    ChatToolDefinition(
        name="runtime_resilience_check",
        description="Inspect recent model-call retries, context compactions, arg repairs, loop breaks, and circuit-breaker state.",
        parameters={"type": "object", "properties": {}},
        backend=TOOL_BACKEND_DISCOVERY,
    ),
    ChatToolDefinition(
        name="prompt_shield",
        description=(
            "Audit untrusted text or a repository file for indirect prompt-injection payloads, "
            "Trojan-Source Unicode bidi/tag spoofing, and accidental secret/credential leaks, returning redacted output."
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "default": ""},
                "path": {"type": "string", "default": ""},
                "redact_secrets": {"type": "boolean", "default": True},
                "include_sanitized": {"type": "boolean", "default": True},
            },
        },
        backend=TOOL_BACKEND_DISCOVERY,
    ),
    ChatToolDefinition(
        name="error_diagnose",
        description=(
            "Parse multi-language tracebacks and compiler/linker errors (Python, MSVC, GCC/Clang, Rust, JS/TS, pytest), "
            "resolve repository source context around failing lines, classify root cause, and recommend fix steps."
        ),
        parameters={
            "type": "object",
            "properties": {
                "error_text": {"type": "string"},
                "root": {"type": "string", "default": ""},
                "max_frames": {"type": "integer", "default": 8},
                "context_lines": {"type": "integer", "default": 3},
            },
            "required": ["error_text"],
        },
        backend=TOOL_BACKEND_DISCOVERY,
    ),
    ChatToolDefinition(
        name="code_audit",
        description=(
            "Static security vulnerability and bug-footgun auditor (Python AST + generic multi-language rules): "
            "flags eval/exec, shell=True, SQL injection, unsafe deserialization, TLS bypass, hardcoded secrets, "
            "mutable defaults, bare except, unreachable code, and always-true tuple asserts."
        ),
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "default": ""},
                "path": {"type": "string", "default": ""},
                "language": {"type": "string", "default": ""},
                "min_severity": {"type": "string", "default": "low"},
            },
        },
        backend=TOOL_BACKEND_DISCOVERY,
    ),
    ChatToolDefinition(
        name="data_transform",
        description=(
            "Deterministic structured data utility: convert between JSON/JSONL/CSV/TSV/TOML/INI/Markdown/flat, "
            "compute column statistical profiles (min/max/mean/median/p95/nulls), run safe dot-path/where/sort/group_by "
            "queries, encode/decode Base64/URL/Hex/Unicode, convert timestamps, and compute 5-field cron next runs."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "default": "convert"},
                "data": {"type": "string", "default": ""},
                "path": {"type": "string", "default": ""},
                "input_format": {"type": "string", "default": "auto"},
                "output_format": {"type": "string", "default": "json"},
                "expression": {"type": "string", "default": ""},
                "encoding": {"type": "string", "default": "base64"},
                "limit": {"type": "integer", "default": 50},
            },
        },
        backend=TOOL_BACKEND_DISCOVERY,
    ),
    ChatToolDefinition(
        name="learn_tool",
        description=(
            "Autonomously discover and figure out an unfamiliar or external tool/program/package (e.g. blender, "
            "inkscape, ffmpeg, krita, godot, pandoc, or any unknown CLI): checks PATH/App Paths/install dirs, "
            "safely probes live --version/--help/<subcommand> --help, inspects Python package metadata, detects "
            "headless/scripting flags, ranks goal-relevant flags, and optionally searches docs online."
        ),
        parameters={
            "type": "object",
            "properties": {
                "tool": {"type": "string"},
                "goal": {"type": "string", "default": ""},
                "subcommand": {"type": "string", "default": ""},
                "research_web": {"type": "boolean", "default": False},
                "save_note": {"type": "boolean", "default": False},
            },
            "required": ["tool"],
        },
        backend=TOOL_BACKEND_DISCOVERY,
    ),
)

_BACKEND_BY_TOOL = {item.name: item.backend for item in CORE_CHAT_TOOL_DEFINITIONS}


@dataclass(frozen=True)
class ChatToolRecord:
    """Structured receipt record for one tool invocation inside a chat turn."""

    call_id: str
    tool: str
    status: str
    backend: str
    duration_ms: int = 0
    exit_code: int | None = None
    timed_out: bool = False
    truncated: bool = False
    bidi_warning: bool = False
    bidi_chars: tuple[str, ...] = ()
    stdout_preview: str = ""
    stderr_preview: str = ""
    reason: str = ""

    def to_receipt(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "call_id": self.call_id,
            "tool": self.tool,
            "status": self.status,
            "backend": self.backend,
            "duration_ms": max(0, int(self.duration_ms)),
            "timed_out": bool(self.timed_out),
            "truncated": bool(self.truncated),
            "bidi_warning": bool(self.bidi_warning),
        }
        if self.exit_code is not None:
            payload["exit_code"] = int(self.exit_code)
        if self.bidi_chars:
            payload["bidi_chars"] = list(self.bidi_chars)
        if self.stdout_preview:
            payload["stdout_preview"] = self.stdout_preview
        if self.stderr_preview:
            payload["stderr_preview"] = self.stderr_preview
        if self.reason:
            payload["reason"] = self.reason
        return payload


def tool_backend_label(tool_name: str, *, container_active: bool = False) -> str:
    name = str(tool_name or "").strip()
    if name == "run_code":
        return TOOL_BACKEND_CONTAINER if container_active else TOOL_BACKEND_HOST
    return _BACKEND_BY_TOOL.get(name, TOOL_BACKEND_WORKSPACE)


def parse_tool_arguments(raw: Any) -> dict[str, Any]:
    """Normalize provider tool-call arguments into a dict, repairing common LLM JSON slips."""
    parsed, _repaired = repair_tool_arguments(raw)
    return parsed


def chat_tools_payload(
    *,
    allowed_names: Sequence[str] | frozenset[str] | None = None,
    extra_definitions: Sequence[ChatToolDefinition] = (),
    depth: int = 0,
) -> list[dict[str, Any]]:
    """Render bounded Ollama/OpenAI tool definitions for ``allowed_names``."""
    items = [*CORE_CHAT_TOOL_DEFINITIONS, *extra_definitions]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    allow = None if allowed_names is None else set(allowed_names)
    for entry in items:
        if entry.name in seen:
            continue
        if int(depth) >= 1 and entry.name in DELEGATION_TOOLS:
            continue
        if allow is not None and entry.name not in allow:
            continue
        seen.add(entry.name)
        out.append(entry.to_ollama_tool())
        if len(out) >= 64:
            break
    return out


__all__ = [
    "CORE_CHAT_TOOL_DEFINITIONS",
    "ChatToolBudget",
    "ChatToolDefinition",
    "ChatToolRecord",
    "DEFAULT_CHAT_TOOL_BUDGET",
    "TOOL_BACKEND_CONTAINER",
    "TOOL_BACKEND_DISCOVERY",
    "TOOL_BACKEND_HOST",
    "TOOL_BACKEND_WORKSPACE",
    "chat_tools_payload",
    "detect_bidi_chars",
    "parse_tool_arguments",
    "preview_tool_arguments",
    "tool_backend_label",
]
