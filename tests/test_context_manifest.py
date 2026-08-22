from sonder_runtime.application.context_manifest import ContextItem, deduplicate, prefix_manifest, replay_manifest, snapshot


def test_context_manifest_deduplicates_with_provenance_and_replays_stably():
    items = (ContextItem("a", "history", "same", "one"), ContextItem("b", "memory", "same", "two"), ContextItem("c", "policy", "keep", "three", True))
    snap = snapshot(items, token_count=12)
    assert [item.item_id for item in deduplicate(items)] == ["a", "c"]
    assert len(prefix_manifest(items)) == 2
    assert replay_manifest(snap, model="local", native_context=4096)["snapshot_digest"] == snap.snapshot_digest
