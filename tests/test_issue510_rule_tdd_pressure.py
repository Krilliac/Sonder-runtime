"""Measured original #510 skill/rule TDD at the real batching guard boundary.

The decision producer is deterministic; filesystem tools and the agent loop
are real. This qualifies the host rule, not a language model's competence.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

import pytest

import server
from sonder_runtime.domain import batch_coalescing as bc


@dataclass(frozen=True)
class PressureResult:
    answer: str
    expected_answer: str
    observed_targets: tuple[int, ...]
    model_calls: int
    tool_calls: tuple[str, ...]
    guard_actions: tuple[str, ...]
    source_digest: str

    def metrics(self):
        return {
            "task_completed": self.answer == self.expected_answer,
            "observed_files": len(self.observed_targets),
            "model_calls": self.model_calls,
            "tool_round_trips": len(self.tool_calls),
            "file_read_calls": self.tool_calls.count("file_read"),
            "context_pack_calls": self.tool_calls.count("context_pack"),
            "guard_actions": list(self.guard_actions),
        }


def _source_digest(workspace):
    return hashlib.sha256(json.dumps([
        (path.name, path.read_text(encoding="utf-8"))
        for path in sorted(workspace.glob("item-*.txt"))
    ], separators=(",", ":")).encode()).hexdigest()


def run_pressure_trial(monkeypatch, workspace, *, with_rule, file_count=12):
    """Read all file values under ten tool steps, reacting to real steering.

    The same decision producer first chooses one unread file. Once it receives
    the actual host advisory, it obeys its four-file bound using context_pack.
    Its final sum is computed solely from observed file contents. The expected
    answer is the evaluator's independent fixture calculation.
    """
    seen, calls, events, prompts = {}, [], [], []
    dispatch = server._agent_dispatch_observed
    counterparts = server._agent_batch_counterparts()
    names = ["item-%02d.txt" % index for index in range(file_count)]
    expected = "sum=%d; files=%d" % (sum(range(1, file_count + 1)), file_count)
    source_digest = _source_digest(workspace)
    batching = False

    def generate(prompt, history=None):
        nonlocal batching
        prompts.append(prompt)
        for index, value in re.findall(r"VALUE_(\d+)=(\d+)", prompt):
            seen[int(index)] = int(value)
        batching = batching or "HOST BATCH ADVISORY" in prompt
        remaining = [name for index, name in enumerate(names) if index not in seen]
        if not remaining:
            return json.dumps({"final": "sum=%d; files=%d" % (sum(seen.values()), len(seen))})
        if "HOST FINALIZATION ONLY" in prompt:
            return json.dumps({"final": "partial sum=%d; files=%d" % (sum(seen.values()), len(seen))})
        if batching:
            return json.dumps({"tool": "context_pack", "args": {"paths_json": remaining[:4]}})
        return json.dumps({"tool": "file_read", "args": {"path": remaining[0]}})

    def record_dispatch(tool, args, **kwargs):
        calls.append(tool)
        return dispatch(tool, args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(server, "_make_generate", lambda *a, **k: generate)
        scoped.setattr(server, "_agent_dispatch_observed", record_dispatch)
        scoped.setattr(server, "_agent_batch_counterparts", lambda: counterparts if with_rule else ())
        scoped.setattr(server.activity_tracker, "record_event", lambda kind, **fields: events.append((kind, fields)))
        for key in (bc.ENV_ADVISORY_AFTER, bc.ENV_REFUSE_AFTER, bc.ENV_MAX_REFUSALS):
            scoped.delenv(key, raising=False)
        # Remove unrelated background speculation from this deterministic rule
        # comparison; the synchronous dispatch count is the measured boundary.
        scoped.setattr(server.sonder_speculation, "speculation_enabled", lambda: False)
        answer = server._agent_impl(
            "Read every one of these files and return their sum: " + ", ".join(names),
            max_steps=10, read_only=True, project=str(workspace), require_file_evidence=True,
        )
    assert _source_digest(workspace) == source_digest
    return PressureResult(
        str(answer), expected, tuple(sorted(seen)), len(prompts), tuple(calls),
        tuple(fields["action"] for kind, fields in events if kind == "agent_guard"), source_digest,
    )


@pytest.mark.parametrize("file_count", [12, 2])
def test_rule_tdd_same_pressure_scenario_before_after_and_below_threshold(monkeypatch, tmp_path, file_count):
    for index in range(file_count):
        (tmp_path / ("item-%02d.txt" % index)).write_text("VALUE_%d=%d\n" % (index, index + 1), encoding="utf-8")
    without = run_pressure_trial(monkeypatch, tmp_path, with_rule=False, file_count=file_count)
    with_rule = run_pressure_trial(monkeypatch, tmp_path, with_rule=True, file_count=file_count)
    assert without.source_digest == with_rule.source_digest
    assert with_rule.answer == with_rule.expected_answer
    assert with_rule.observed_targets == tuple(range(file_count))
    if file_count == 12:
        # Recorded baseline failure: ten independent reads exhaust the fixed
        # budget before two files are seen. This is not an assigned score.
        assert without.answer == "partial sum=55; files=10"
        assert without.answer != without.expected_answer
        assert without.metrics() == {
            "task_completed": False, "observed_files": 10, "model_calls": 11,
            "tool_round_trips": 10, "file_read_calls": 10, "context_pack_calls": 0,
            "guard_actions": [],
        }
        assert with_rule.metrics() == {
            "task_completed": True, "observed_files": 12, "model_calls": 7,
            "tool_round_trips": 6, "file_read_calls": 3, "context_pack_calls": 3,
            "guard_actions": ["advisory"],
        }
    else:
        # Ordinary traffic retains the same quality, tool choices and cost.
        assert without.answer == with_rule.answer
        assert without.metrics() == with_rule.metrics()
        assert with_rule.guard_actions == ()
    print(json.dumps({"file_count": file_count, "without_rule": without.metrics(), "with_rule": with_rule.metrics()}, sort_keys=True))
