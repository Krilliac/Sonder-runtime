r"""ReDoS regression tests for the unfenced-log detector in the execution-mode router.

``_LOG_LINE_PATTERNS`` includes a structured-JSON-record pattern
(``^\s*\{.*"(?:level|ts|...)"\s*:.*\}\s*$``) that is O(n^2) on a single line
holding many ``"ts":`` keys and no closing brace. ``_is_log_line`` ran it on
every prose line with no length cap, so one hostile line in a chat prompt
(body cap 1-16 MiB) pinned a worker for minutes. These tests pin the contract:
a hostile line is handled in time that grows linearly, and routing of normal
log pastes is unchanged.
"""
from __future__ import annotations

import pytest

from sonder_runtime.domain.routing import execution_mode_router as router
from sonder_runtime.domain.routing.execution_mode_router import (
    ExecutionContext,
    ExecutionModePolicy,
    plan_execution_mode,
    score_difficulty,
)
from tests._fuzz_timing import assert_scales_linearly, time_once

pytestmark = pytest.mark.unit

# Measured on the unfixed code (this machine): 200 KB -> ~2.5 s, 1 MB -> ~1 minute.
# 200 KB keeps the RED run to a few seconds per test while a linear scan stays in single-digit ms.
HOSTILE_CHARS = 200_000
SMALL_CHARS = 25_000
CEILING_S = 1.0


def _hostile_json_line(chars: int) -> str:
    return "{" + '"ts":' * (chars // 5) + "x"


def _score(prompt: str) -> object:
    return score_difficulty(prompt)


def _plan(prompt: str) -> object:
    return plan_execution_mode(
        ExecutionContext(
            prompt=prompt,
            available_tiers=frozenset({"fast", "code", "reasoning"}),
            distinct_ensemble_models=2,
            distinct_local_models=2,
            developer_authorized=True,
        ),
        ExecutionModePolicy(mode="auto"),
    )


def test_is_log_line_hostile_json_record_is_bounded():
    big = _hostile_json_line(HOSTILE_CHARS)
    elapsed = time_once(router._is_log_line, big)
    assert elapsed < CEILING_S, f"_is_log_line took {elapsed:.2f}s on {len(big)} chars"


def test_is_log_line_cost_scales_linearly_on_hostile_json_record():
    assert_scales_linearly(
        router._is_log_line,
        _hostile_json_line(SMALL_CHARS),
        _hostile_json_line(100_000),
        label="_is_log_line hostile json record",
        ceiling_s=CEILING_S,
        skip_ratio_below_s=0.05,
    )


def test_score_difficulty_hostile_json_line_is_bounded():
    big = _hostile_json_line(HOSTILE_CHARS)
    elapsed = time_once(_score, big)
    assert elapsed < CEILING_S, f"score_difficulty took {elapsed:.2f}s on {len(big)} chars"


def test_score_difficulty_hostile_line_inside_prose_is_bounded():
    prompt = "Summarize this payload please:\n" + _hostile_json_line(HOSTILE_CHARS) + "\nThanks."
    elapsed = time_once(_score, prompt)
    assert elapsed < CEILING_S, f"score_difficulty took {elapsed:.2f}s on {len(prompt)} chars"


def test_plan_execution_mode_hostile_json_line_is_bounded():
    big = _hostile_json_line(HOSTILE_CHARS)
    elapsed = time_once(_plan, big)
    assert elapsed < CEILING_S, f"plan_execution_mode took {elapsed:.2f}s on {len(big)} chars"


def test_plan_execution_mode_hostile_json_line_still_yields_a_decision():
    decision = _plan("Count the entries:\n" + _hostile_json_line(60_000))
    assert decision.mode == "single"
    assert decision.band in {"quick", "standard"}


def test_short_json_log_records_are_still_log_lines():
    assert router._is_log_line('{"timestamp":"2026-11-25T01:00:01Z","level":"INFO","msg":"boot"}')
    assert router._is_log_line('  {"ts": 1, "msg": "x"}  ')
    assert not router._is_log_line('{"name": "value", "count": 3}')
    assert not router._is_log_line("Please explain how the router picks a tier.")


def test_ndjson_paste_is_still_dampened_as_log():
    prompt = (
        "Count the error entries:\n"
        '{"timestamp":"2026-11-25T01:00:01Z","level":"INFO","msg":"boot"}\n'
        '{"timestamp":"2026-11-25T01:00:02Z","level":"ERROR","msg":"deadlock risk"}\n'
        '{"timestamp":"2026-11-25T01:00:03Z","level":"ERROR","msg":"race condition"}\n'
    )
    band, _, signals, _, _ = score_difficulty(prompt)
    assert band == "standard"
    assert "pasted_log_dampened" in signals
