"""Focused contracts for interactive lane prompt and decode evidence metadata."""

from types import SimpleNamespace

import pytest

from sonder_runtime.application.agents.interactive_lanes import (
    _LANE_HISTORY_MESSAGES,
    _prefix_timing_stats,
    _retain_history_with_hysteresis,
)


def test_prefix_timing_stats_accepts_a3_mapping_and_object_shapes():
    assert _prefix_timing_stats(SimpleNamespace(
        timings={"cache_n": 120, "prompt_n": 8, "ignored": 1},
    )) == {"cache_n": 120, "prompt_n": 8}
    assert _prefix_timing_stats(SimpleNamespace(
        timings=SimpleNamespace(cache_n=120, prompt_n=8),
    )) == {"cache_n": 120, "prompt_n": 8}


def test_prefix_timing_stats_does_not_estimate_missing_backend_counts():
    assert _prefix_timing_stats(SimpleNamespace(timings=None)) == {}
    assert _prefix_timing_stats(SimpleNamespace(
        timings={"prompt_n": 8, "cache_n": None},
    )) == {"prompt_n": 8}
    assert _prefix_timing_stats(SimpleNamespace(
        timings={"cache_n": True, "prompt_n": 1.5},
    )) == {}
    assert _prefix_timing_stats(SimpleNamespace(timings={"cache_n": -1, "prompt_n": float("inf")})) == {}


@pytest.mark.parametrize("per_turn", [1, 2])
def test_actual_history_head_mutates_no_more_than_once_per_five_turns(tmp_path, per_turn):
    from tests.test_live_agent_context import _project, _tool_worker

    project = _project(tmp_path, name="history", rule="HISTORY RULE")
    service, store, _planner, context = _tool_worker(tmp_path / "worker", tmp_path)
    lane_id = service.spawn(
        command_id="history-cap", parent_session_id="parent", task="inspect",
        workspace_root=str(project), context=context,
    )["lane"]["id"]
    lane = store.read_lane(lane_id)
    previous_head = None
    mutations = []
    for turn in range(1, 70):
        for offset in range(per_turn):
            event_id = "tool-%d-%d" % (turn, offset)
            service.sessions.append(
                lane["session_id"], "tool.result", {"call_id": event_id, "content": "ok"},
                event_id=event_id,
            )
        history = service._history(lane)
        assert len(history) <= _LANE_HISTORY_MESSAGES
        head = history[0]
        if previous_head is not None and head != previous_head:
            mutations.append(turn)
        previous_head = head
    assert mutations
    assert all(right - left >= 5 for left, right in zip(mutations, mutations[1:], strict=False))


def test_activity_observer_failure_cannot_fail_a_completed_model_step():
    from sonder_runtime.application.agents.interactive_lanes import _record_prefix_activity

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("observer unavailable")

    _record_prefix_activity(unavailable, 1, {"cache_n": 12, "prompt_n": 2}, turn_id="turn", request_id="request")


def test_history_head_moves_in_four_entry_blocks_after_the_cap_binds():
    def entry(sequence, protected=False):
        return sequence, 0, {"content": str(sequence)}, protected

    timeline = [entry(sequence) for sequence in range(_LANE_HISTORY_MESSAGES)]
    kept, cursor = _retain_history_with_hysteresis(timeline, 0, None)
    assert len(kept) == _LANE_HISTORY_MESSAGES
    assert cursor is None

    kept, cursor = _retain_history_with_hysteresis(
        timeline + [entry(_LANE_HISTORY_MESSAGES)], 0, cursor,
    )
    assert len(kept) == _LANE_HISTORY_MESSAGES - 15
    first_head = kept[0][0]
    for sequence in range(_LANE_HISTORY_MESSAGES + 1, _LANE_HISTORY_MESSAGES + 16):
        kept, cursor = _retain_history_with_hysteresis(
            timeline + [entry(value) for value in range(_LANE_HISTORY_MESSAGES, sequence + 1)],
            0, cursor,
        )
        assert kept[0][0] == first_head
    kept, _ = _retain_history_with_hysteresis(
        timeline + [entry(value) for value in range(_LANE_HISTORY_MESSAGES, _LANE_HISTORY_MESSAGES + 17)],
        0, None,
    )
    assert kept[0][0] > first_head
    assert len(kept) <= _LANE_HISTORY_MESSAGES


def test_history_hysteresis_keeps_protected_entries():
    def entry(sequence, protected=False):
        return sequence, 0, {"content": str(sequence)}, protected

    protected = [entry(sequence, True) for sequence in range(3)]
    ordinary = [entry(sequence + 3) for sequence in range(40)]
    kept, _ = _retain_history_with_hysteresis(protected + ordinary, len(protected), None)
    assert len(kept) <= _LANE_HISTORY_MESSAGES - len(protected)


def test_history_preserves_newest_tool_when_protected_facts_leave_one_slot():
    protected = [(n, 0, {"content": "keep"}, True) for n in range(39)]
    ordinary = [(39, 0, {"content": "older"}, False), (40, 0, {"content": "newest"}, False)]
    kept, _ = _retain_history_with_hysteresis(protected + ordinary, 39)
    assert kept == ordinary[-1:]
    kept, _ = _retain_history_with_hysteresis(protected + ordinary, 40)
    assert kept == []


def test_run_pending_projects_prefix_stats_to_ledger_and_activity(tmp_path):
    from tests.test_live_agent_context import _project, _tool_worker

    project = _project(tmp_path, name="timed", rule="TIMED RULE")
    service, store, _planner, context = _tool_worker(tmp_path / "worker", tmp_path)
    observed = []

    class TimedResponse:
        text = "done"
        model = "fake-model"
        tier = "code"
        tokens_out = 1
        timings = {"cache_n": 120, "prompt_n": 8}

    class TimedGateway:
        def generate(self, request, context):
            return TimedResponse()

    service.gateway = TimedGateway()
    service._activity_observer = lambda kind, **fields: observed.append((kind, fields))
    lane_id = service.spawn(
        command_id="timed-prefix", parent_session_id="parent", task="inspect",
        workspace_root=str(project), context=context,
    )["lane"]["id"]
    service.run_pending(lane_id, context)
    events, _ = store.events(lane_id, 0, 100)
    decode = [event for event in events if event["event_type"] == "inference.decode"]
    assert len(decode) == 1
    assert decode[0]["payload"]["summary"] == "step 1 cache_n=120 prompt_n=8"
    assert observed == [("inference.decode", {
        "step": 1, "turn_id": decode[0]["payload"]["turn_id"],
        "request_id": decode[0]["payload"]["request_id"],
        "summary": "step 1 cache_n=120 prompt_n=8",
        "cache_n": 120, "prompt_n": 8,
    })]
