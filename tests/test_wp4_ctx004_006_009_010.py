"""WP4 CTX-004/006/009/010 context manifest contracts."""

from sonder_runtime.application.context_manifests import (
    ContextRecord,
    LastGoodSnapshot,
    PrefixManifestCache,
    build_replay_manifest,
    deduplicate_context,
)


def record(item_id, text, *, ordinal=0, source="fixture", stable=False, section="stable_instructions"):
    return ContextRecord(item_id, section, text, source, ordinal, stable)


def test_dedup_is_deterministic_and_retains_exact_and_semantic_provenance():
    result = deduplicate_context([
        record("b", "Policy: keep local data.", ordinal=2, source="policy"),
        record("a", "Policy: keep local data.", ordinal=1, source="rules"),
        record("c", "POLICY keep local data", ordinal=3, source="memory"),
    ], semantic_threshold=0.8)

    assert [item.item_id for item in result.retained] == ["a"]
    assert [(item.removed_item_id, item.reason, item.retained_item_id) for item in result.provenance] == [
        ("b", "exact", "a"), ("c", "semantic", "a")
    ]
    assert result.provenance[0].removed_source == "policy"
    assert result.provenance[1].retained_source == "rules"


def test_last_good_snapshot_rejects_incomplete_refresh_and_isolated_mutation():
    store = LastGoodSnapshot()
    first = store.publish({"items": ["one"]})
    rejected = store.publish({"items": ["partial"]}, complete=False)
    assert rejected.digest == first.digest
    current = store.get()
    current.value["items"].append("caller mutation")
    assert store.get().value == {"items": ["one"]}


def test_prefix_manifest_and_cache_key_are_stable_with_hit_write_metrics():
    cache = PrefixManifestCache()
    rows = [
        record("rules", "rules", ordinal=4, stable=True, section="project_rules"),
        record("schema", "schema", ordinal=1, stable=True, section="tool_schemas"),
        record("skill", "skill", ordinal=2, stable=True, section="skill_catalog"),
    ]
    identity = {
        "model": "model-a",
        "tokenizer": "tok-1",
        "template": "chat-v2",
        "system_prefix": "system",
        "visible_tool_schemas": {"read": {"type": "object"}},
        "project_policy": {"network": "deny"},
    }
    first = cache.resolve(rows, version="v2", **identity)
    second = cache.resolve(list(reversed(rows)), version="v2", **identity)
    assert first.cache_key == second.cache_key
    assert [item.section for item in first.sections] == [
        "project_rules", "skill_catalog", "tool_schemas"
    ]
    assert (cache.writes, cache.hits) == (1, 1)


def test_prefix_identity_hits_and_dynamic_memory_does_not_bust():
    cache = PrefixManifestCache()
    rows = [record("rules", "rules", stable=True)]
    first = cache.resolve(
        rows, model="model-a", tokenizer="tok-1", template="chat-v2",
        system_prefix="system", visible_tool_schemas={"read": {"type": "object"}},
        project_policy={"network": "deny"}, dynamic_memory=["turn-1"],
    )
    second = cache.resolve(
        rows, model="model-a", tokenizer="tok-1", template="chat-v2",
        system_prefix="system", visible_tool_schemas={"read": {"type": "object"}},
        project_policy={"network": "deny"}, dynamic_memory=["turn-2"], retrieval={"query": "new"},
    )
    assert first.cache_key == second.cache_key
    assert cache.telemetry.last_reason == "hit"
    assert cache.telemetry.hits == 1


def test_prefix_identity_misses_for_model_schema_and_policy_changes():
    cache = PrefixManifestCache()
    rows = [record("rules", "rules", stable=True)]
    common = {"tokenizer": "tok", "template": "chat", "system_prefix": "sys"}
    cache.resolve(rows, model="a", visible_tool_schemas={"read": 1}, project_policy={"x": 1}, **common)
    cache.resolve(rows, model="b", visible_tool_schemas={"read": 1}, project_policy={"x": 1}, **common)
    assert cache.telemetry.last_reason == "identity_changed"
    cache.resolve(rows, model="b", visible_tool_schemas={"write": 1}, project_policy={"x": 1}, **common)
    cache.resolve(rows, model="b", visible_tool_schemas={"write": 1}, project_policy={"x": 2}, **common)
    assert cache.telemetry.misses == 4
    assert cache.telemetry.reasons["identity_changed"] == 3


def test_prefix_cache_is_bounded_and_miss_reason_uses_last_resolution():
    cache = PrefixManifestCache(max_entries=2)
    rows = [record("rules", "rules", stable=True)]
    cache.resolve(rows, model="a")
    cache.resolve(rows, model="b")
    cache.resolve([record("other", "other", stable=True)], model="b")
    assert len(cache._values) == 2
    assert cache.telemetry.last_reason == "prefix_changed"
    cache.resolve(rows, model="a")
    assert cache.telemetry.last_reason == "identity_changed"


def test_replay_manifest_preserves_order_and_is_immutable():
    rows = [record("history", "old", ordinal=8, stable=False, section="recent_history"), record("policy", "safe", ordinal=2)]
    manifest = build_replay_manifest("req-1", "model-a", rows, prefix_key="v2:key", metadata={"temperature": 0})
    assert [section.item_id for section in manifest.sections] == ["history", "policy"]
    assert manifest.sections[0].content_digest == rows[0].content_digest
    try:
        manifest.metadata["temperature"] = 1
    except TypeError:
        pass
    else:
        raise AssertionError("replay metadata must be immutable")
