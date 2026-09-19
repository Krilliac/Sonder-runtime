"""Pure policy tests for bounded local reasoning continuation."""

import pytest

from sonder_runtime.domain import reasoning_continuation as policy


def test_checkpoint_compaction_is_bounded_and_keeps_both_ends():
    previous = "PLAN:" + ("a" * 300)
    current = "LATEST:" + ("z" * 300)

    checkpoint = policy.compact_checkpoint(previous, current, max_chars=256)

    assert len(checkpoint) <= 256
    assert checkpoint.startswith("PLAN:")
    assert checkpoint.endswith("z" * 20)
    assert "older private reasoning compacted" in checkpoint


def test_checkpoint_payload_replaces_prior_checkpoint_and_reserves_final_answer():
    old = {
        "role": "user",
        "content": policy.CHECKPOINT_MARKER + "\nold private state",
    }
    payload = {
        "model": "local",
        "messages": [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "original request"},
            old,
        ],
        "options": {"temperature": 0.1, "num_predict": 100},
    }

    updated = policy.checkpoint_payload(
        payload, "new private state", num_predict=75, final_segment=True,
    )

    checkpoint_messages = [
        message for message in updated["messages"]
        if message.get("content", "").startswith(policy.CHECKPOINT_MARKER)
    ]
    assert len(checkpoint_messages) == 1
    assert "new private state" in checkpoint_messages[0]["content"]
    assert "old private state" not in checkpoint_messages[0]["content"]
    assert updated["think"] is False
    assert updated["options"] == {"temperature": 0.1, "num_predict": 75}
    assert payload["messages"][-1] is old
    assert payload["options"]["num_predict"] == 100


def test_segment_planner_has_a_hard_total_and_final_segment():
    first = policy.plan_next_segment(
        spent_tokens=100,
        total_tokens=300,
        chunk_tokens=100,
        completed_segments=1,
    )
    assert first is not None
    assert first.num_predict == 66
    assert first.final_segment is False

    final = policy.plan_next_segment(
        spent_tokens=232,
        total_tokens=300,
        chunk_tokens=66,
        completed_segments=3,
    )
    assert final is not None
    assert final.num_predict == 68
    assert final.final_segment is True

    assert policy.plan_next_segment(
        spent_tokens=300,
        total_tokens=300,
        chunk_tokens=100,
        completed_segments=3,
    ) is None


@pytest.mark.parametrize("value", [True, 1.5, "100", 0, 65537])
def test_token_budgets_are_strict_and_bounded(value):
    with pytest.raises(ValueError):
        policy.strict_token_budget(value, field="budget")
