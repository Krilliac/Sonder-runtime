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
    rows = [record("rules", "rules", ordinal=4, stable=True), record("schema", "schema", ordinal=1, stable=True)]
    first = cache.resolve(rows, version="v2")
    second = cache.resolve(list(reversed(rows)), version="v2")
    assert first.cache_key == second.cache_key
    assert [item.item_id for item in first.sections] == ["rules", "schema"]
    assert (cache.writes, cache.hits) == (1, 1)


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
