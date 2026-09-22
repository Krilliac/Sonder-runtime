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
    payload = {
        "model": "local",
        "messages": [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "original request"},
        ],
        "options": {"temperature": 0.1, "num_predict": 100},
    }
    first = policy.checkpoint_payload(
        payload, "old private state", num_predict=80, final_segment=False,
    )
    old = first["messages"][-1]

    updated = policy.checkpoint_payload(
        first,
        "new private state",
        num_predict=75,
        final_segment=True,
        previous_checkpoint="old private state",
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
    assert first["messages"][-1] is old
    assert payload["options"]["num_predict"] == 100


def test_checkpoint_payload_preserves_caller_message_with_checkpoint_prefix():
    caller_message = {
        "role": "user",
        "content": policy.CHECKPOINT_MARKER + "\nExplain this protocol marker",
    }
    payload = {
        "model": "local",
        "messages": [caller_message],
        "options": {"num_predict": 100},
    }

    updated = policy.checkpoint_payload(
        payload, "private state", num_predict=50, final_segment=False,
    )

    assert updated["messages"][0] is caller_message
    assert len(updated["messages"]) == 2


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


def test_default_total_scales_to_reserve_an_answer_segment():
    assert policy.total_token_budget(chunk_tokens=4096) == 8192
    assert policy.total_token_budget(chunk_tokens=1024) == 4096


@pytest.mark.parametrize(
    ("chunk_tokens", "total_tokens"),
    [
        (4096, 4096),
        (4096, 2048),
        (policy.MAX_TOTAL_TOKENS, None),
    ],
)
def test_total_budget_rejects_configurations_without_an_answer_reserve(
    chunk_tokens, total_tokens,
):
    with pytest.raises(ValueError, match="reserve a final answer segment"):
        policy.total_token_budget(
            chunk_tokens=chunk_tokens,
            total_tokens=total_tokens,
        )


@pytest.mark.parametrize("value", [True, 1.5, "100", 0, 65537])
def test_token_budgets_are_strict_and_bounded(value):
    with pytest.raises(ValueError):
        policy.strict_token_budget(value, field="budget")
