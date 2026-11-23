"""Tests for model-call resilience, tool-use failure recovery, and alternative-method planning."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import server
from sonder_runtime.application.chat.tool_loop import ChatToolTurn, build_tool_turn_manifest
from sonder_runtime.domain.chat_tools import CORE_CHAT_TOOL_DEFINITIONS, ChatToolBudget, parse_tool_arguments
from sonder_runtime.domain.resilience import (
    GLOBAL_TELEMETRY,
    CircuitBreaker,
    classify_model_error,
    classify_tool_failure,
    compact_messages_for_retry,
    plan_alternatives,
    repair_tool_arguments,
    resolve_tool_name,
)
from sonder_runtime.domain.speculation_policy import is_speculatable
from sonder_runtime.domain.tools.builtin_traits import builtin_traits


# --- 1. Pure domain resilience primitives -------------------------------------


def test_repair_tool_arguments_handles_fences_trailing_commas_prose_and_python_dicts():
    # Valid JSON fast path
    d, rep = repair_tool_arguments('{"path": "a.py"}')
    assert d == {"path": "a.py"} and rep is False

    # Markdown code fence + trailing comma
    d2, rep2 = repair_tool_arguments('```json\n{"path": "a.py", "limit": 5,}\n```')
    assert d2 == {"path": "a.py", "limit": 5} and rep2 is True

    # Surrounding prose around JSON
    d3, rep3 = repair_tool_arguments('Sure, here are args: {"expression": "2+2", "exact": true,} done.')
    assert d3 == {"expression": "2+2", "exact": True} and rep3 is True

    # Python single-quoted dict literal with True/None
    d4, rep4 = repair_tool_arguments("{'path': 'foo.py', 'exact': True, 'x': None}")
    assert d4 == {"path": "foo.py", "exact": True, "x": None} and rep4 is True

    # parse_tool_arguments uses repair automatically
    assert parse_tool_arguments("{'query': 'hello'}") == {"query": "hello"}


def test_resolve_tool_name_aliases_and_fuzzy_suggestions_respect_admitted_set():
    admitted = ("file_read", "text_search", "math_eval", "run_code")
    assert resolve_tool_name("read_file", admitted) == ("file_read", [], True)
    assert resolve_tool_name("grep", admitted) == ("text_search", [], True)
    assert resolve_tool_name("calc", admitted) == ("math_eval", [], True)

    # Alias whose target is NOT admitted must not escalate
    name, _sug, aliased = resolve_tool_name("create_file", admitted)
    assert aliased is False and name == "create_file"

    # Typo returns close suggestions within admitted
    typo, sug, aliased2 = resolve_tool_name("file_raed", admitted)
    assert aliased2 is False and "file_read" in sug


def test_classify_model_error_and_context_compaction():
    assert classify_model_error(TimeoutError("read timed out")) == "transient"
    assert classify_model_error(RuntimeError("HTTP 503 Service Unavailable")) == "transient"
    assert classify_model_error(RuntimeError("prompt exceeds maximum context length (n_ctx)")) == "context_overflow"
    assert classify_model_error(RuntimeError("model does not support tools")) == "tools_unsupported"
    assert classify_model_error(RuntimeError("HTTP 401 Unauthorized")) == "non_retryable"

    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "goal"},
        {"role": "tool", "name": "file_read", "content": "X" * 5000},
        {"role": "assistant", "content": "step 1"},
        {"role": "tool", "name": "text_search", "content": "recent"},
        {"role": "user", "content": "continue"},
        {"role": "assistant", "content": "step 2"},
    ]
    compacted, saved = compact_messages_for_retry(msgs, max_tool_chars=400, keep_recent=3)
    assert saved > 3000
    assert compacted[0]["content"] == "sys"
    assert compacted[1]["content"] == "goal"
    assert "[compacted" in compacted[2]["content"]
    assert compacted[-1]["content"] == "step 2"


def test_plan_alternatives_maps_failures_to_policy_safe_fallbacks():
    plan = plan_alternatives(
        failed_tool="run_code",
        error="ERROR: HOST POLICY: refused in manual mode",
        goal="calculate combinations and check Python imports",
        available_tools=("run_code", "math_eval", "symbol_verify", "reasoning_board"),
    )
    assert plan["ok"] is True
    assert plan["failure_class"] == "permission_refused"
    alt_tools = [a["tool"] for a in plan["alternatives"]]
    assert "math_eval" in alt_tools
    assert "symbol_verify" in alt_tools
    assert "run_code" not in alt_tools

    # Already tried tools are excluded
    plan2 = plan_alternatives(
        failed_tool="file_read",
        error="FileNotFoundError: no such file",
        available_tools=("file_read", "file_find", "directory_tree"),
        tried_tools=("file_find",),
    )
    assert plan2["failure_class"] == "not_found"
    assert [a["tool"] for a in plan2["alternatives"]] == ["directory_tree"]


def test_circuit_breaker_opens_and_recovers():
    cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=60.0)
    assert cb.is_open("ep") is False
    assert cb.record_failure("ep") is False
    assert cb.record_failure("ep") is True
    assert cb.is_open("ep") is True
    cb.record_success("ep")
    assert cb.is_open("ep") is False


# --- 2. ChatToolTurn end-to-end resilience ------------------------------------


class _AllowPolicy:
    def decide(self, tool_name, **_kwargs):
        return SimpleNamespace(allowed=True, call_id="c1", risk="safe", mode="auto", source="allow")


class _StubBackend:
    def __init__(self, handler):
        self._handler = handler

    def backend_for(self, _name):
        return "workspace"

    def execute(self, name, args):
        return self._handler(name, args)


def _ok_result(output="ok"):
    return SimpleNamespace(
        ok=True, output=output, backend="workspace", duration_ms=1,
        exit_code=0, timed_out=False, stdout_preview=output[:80], stderr_preview="", reason="",
    )


def _fail_result(output="ERROR: file not found"):
    return SimpleNamespace(
        ok=False, output=output, backend="workspace", duration_ms=1,
        exit_code=1, timed_out=False, stdout_preview="", stderr_preview=output[:80], reason=output[:80],
    )


def test_chat_tool_turn_recovers_from_transient_model_error_and_context_overflow():
    GLOBAL_TELEMETRY.reset()
    attempts = {"n": 0}
    seen_lengths = []

    def generate_step(messages, tools):
        attempts["n"] += 1
        seen_lengths.append(sum(len(str(m.get("content") or "")) for m in messages))
        if attempts["n"] == 1:
            return {
                "message": {
                    "content": "",
                    "tool_calls": [{"id": "1", "function": {"name": "read_file", "arguments": "{'path': 'big.txt',}"}}],
                }
            }
        if attempts["n"] == 2:
            raise TimeoutError("connection timed out")
        if attempts["n"] == 3:
            raise RuntimeError("prompt exceeds maximum context length")
        return {"message": {"content": "Recovered final answer."}}

    executed = []

    def exec_fn(name, args):
        executed.append((name, args))
        return _ok_result("DATA " * 600)

    manifest = build_tool_turn_manifest(admitted_tools=("file_read", "file_find"), user_run_command=False, mode="auto")
    turn = ChatToolTurn(
        manifest=manifest,
        tools_payload=[],
        backend=_StubBackend(exec_fn),
        permission_policy=_AllowPolicy(),
        budget=ChatToolBudget(max_model_calls=4, model_output_chars=4000),
    )
    outcome = turn.run(generate_step, system="sys", prompt="read big.txt")
    # Alias `read_file` -> `file_read` AND Python dict repair worked!
    assert executed == [("file_read", {"path": "big.txt"})]
    assert outcome.content == "Recovered final answer."
    snap = GLOBAL_TELEMETRY.snapshot()["counters"]
    assert snap.get("tool_name_aliased", 0) >= 1
    assert snap.get("tool_args_repaired", 0) >= 1
    assert snap.get("transient_model_retry", 0) >= 1


def test_chat_tool_turn_retries_readonly_transient_error_and_injects_fallback_hint():
    GLOBAL_TELEMETRY.reset()
    calls_for_read = {"n": 0}
    tool_observations = []

    def exec_fn(name, args):
        if name == "file_read":
            calls_for_read["n"] += 1
            if calls_for_read["n"] == 1:
                raise TimeoutError("transient disk read timeout")
            return _fail_result("FileNotFoundError: missing.py does not exist")
        if name == "file_find":
            return _ok_result("found: src/real.py")
        return _ok_result("done")

    step_num = {"n": 0}

    def generate_step(messages, tools):
        step_num["n"] += 1
        if step_num["n"] == 1:
            return {"message": {"tool_calls": [{"id": "1", "function": {"name": "file_read", "arguments": '{"path": "missing.py"}'}}]}}
        if step_num["n"] == 2:
            # Capture the recovery hint injected after file_read failed
            tool_observations.append(messages[-1]["content"])
            return {"message": {"tool_calls": [{"id": "2", "function": {"name": "file_find", "arguments": '{"query": "real.py"}'}}]}}
        return {"message": {"content": "Found src/real.py via fallback."}}

    manifest = build_tool_turn_manifest(admitted_tools=("file_read", "file_find", "directory_tree"), user_run_command=False, mode="auto")
    turn = ChatToolTurn(
        manifest=manifest,
        tools_payload=[],
        backend=_StubBackend(exec_fn),
        permission_policy=_AllowPolicy(),
    )
    outcome = turn.run(generate_step, system="sys", prompt="find missing.py")
    assert calls_for_read["n"] == 2  # Retried transient read-only exception once!
    assert "[RECOVERY HINT]" in tool_observations[0]
    assert "file_find" in tool_observations[0]
    assert outcome.content == "Found src/real.py via fallback."


def test_chat_tool_turn_graceful_degraded_summary_after_mid_turn_model_crash():
    step_num = {"n": 0}

    def generate_step(messages, tools):
        step_num["n"] += 1
        if step_num["n"] == 1:
            return {"message": {"tool_calls": [{"id": "1", "function": {"name": "math_eval", "arguments": '{"expression": "2+2"}'}}]}}
        raise ValueError("unexpected permanent provider failure")

    manifest = build_tool_turn_manifest(admitted_tools=("math_eval",), user_run_command=False, mode="auto")
    turn = ChatToolTurn(
        manifest=manifest,
        tools_payload=[],
        backend=_StubBackend(lambda _n, _a: _ok_result("4")),
        permission_policy=_AllowPolicy(),
    )
    outcome = turn.run(generate_step, system="sys", prompt="calc 2+2")
    assert "math_eval(ok)" in outcome.content
    assert "unexpected permanent provider failure" in outcome.content


# --- 3. Cross-surface tool registration ---------------------------------------


def test_resilience_tools_wired_across_surfaces():
    fb = json.loads(server.control_command("/fallback run_code | refused in manual mode | compute 2^10"))
    assert fb["ok"] is True
    assert fb["failure_class"] == "permission_refused"
    assert any(a["tool"] == "math_eval" for a in fb["alternatives"])

    rc = json.loads(server.control_command("/resilience"))
    assert rc["ok"] is True and "telemetry" in rc and "circuit_breaker" in rc

    core_names = {d.name for d in CORE_CHAT_TOOL_DEFINITIONS}
    for name in ("plan_fallback", "runtime_resilience_check"):
        assert name in core_names
        assert builtin_traits(name).is_read_only is True
        assert is_speculatable(name) is True
