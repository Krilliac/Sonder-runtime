"""OPS-006 real activity producer/snapshot privacy and cardinality qualification."""

from __future__ import annotations

import json

import pytest

import sonder_runtime.adapters.observability.activity_tracker as activity


@pytest.fixture(autouse=True)
def reset_activity():
    activity.reset_for_tests()
    yield
    activity.reset_for_tests()


def test_real_activity_snapshot_is_redacted_by_default_and_bounded(monkeypatch):
    monkeypatch.delenv("SONDER_EXECUTION_FEED_DETAIL", raising=False)
    activity.reset_for_tests()
    prompt = "OPS006_PROMPT_SENTINEL"
    secret = "OPS006_SECRET_SENTINEL"
    artifact = "OPS006_ARTIFACT_SENTINEL"
    with activity.response_span("ops006", prompt, feed_owner="owner-a"):
        activity.record_model_call(
            model="local-model", request_preview=prompt,
            response_preview=secret, ok=True,
        )
        activity.record_tool_result(
            "file_write", {"path": "artifact.bin", "content": artifact},
            command=["writer", "--token", secret], output=artifact,
        )
        activity.record_file_change(
            "create", r"C:\private\artifact.bin", preview=artifact,
            preview_kind="content",
        )

    public = activity.public_snapshot()
    encoded = json.dumps(public, ensure_ascii=False)
    assert all(value not in encoded for value in (prompt, secret, artifact))
    assert public["detail_enabled"] is False
    feed = activity.execution_feed(activity.snapshot())
    assert all(value not in json.dumps(feed) for value in (prompt, secret, artifact))
    assert len(feed["events"]) <= activity.MAX_FEED_EVENTS
    assert feed["bytes"] <= activity.MAX_FEED_BYTES
    assert feed["truncated"] is False or len(feed["events"]) < activity.MAX_FEED_EVENTS


def test_real_activity_api_bounds_event_and_owner_cardinality(monkeypatch):
    monkeypatch.delenv("SONDER_EXECUTION_FEED_DETAIL", raising=False)
    activity.reset_for_tests()
    for index in range(activity.MAX_FEED_OWNERS + 20):
        owner = f"ops006-owner-{index}"
        with activity.response_span(
            f"label-{index}", "OPS006_PROMPT_SENTINEL", feed_owner=owner,
        ):
            for event_index in range(activity.MAX_OWNER_FEED_ENTRIES + 5):
                activity.record_event(
                    f"ops006-label-{index}-{event_index}",
                    summary="OPS006_SECRET_SENTINEL",
                )

    latest_owner = f"ops006-owner-{activity.MAX_FEED_OWNERS + 19}"
    for span_index in range(activity.MAX_OWNER_FEED_ENTRIES + 5):
        with activity.response_span(
            f"repeat-{span_index}", "OPS006_PROMPT_SENTINEL", feed_owner=latest_owner,
        ):
            activity.record_event("repeated-owner", summary="OPS006_SECRET_SENTINEL")
    retained = [
        activity.live_feed_for_owner(f"ops006-owner-{index}")
        for index in range(activity.MAX_FEED_OWNERS + 20)
    ]
    assert sum(bool(feed["recent"]) for feed in retained) == activity.MAX_FEED_OWNERS
    assert retained[0]["recent"] == []
    owner_feed = activity.live_feed_for_owner(latest_owner)
    assert owner_feed["owner_scoped"] is True
    assert len(owner_feed["recent"]) == activity.MAX_OWNER_FEED_ENTRIES
    assert all(
        "OPS006_SECRET_SENTINEL" not in json.dumps(entry)
        for entry in owner_feed["recent"]
    )
    public_feed = activity.execution_feed(activity.snapshot(), max_events=1000)
    assert len(public_feed["events"]) <= activity.MAX_FEED_EVENTS
    assert public_feed["bytes"] <= activity.MAX_FEED_BYTES
    assert public_feed["truncated"] is True
