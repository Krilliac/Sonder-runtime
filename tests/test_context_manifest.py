from sonder_runtime.application.context_manifest import ContextItem, deduplicate, prefix_manifest, replay_manifest, snapshot


def test_context_manifest_deduplicates_with_provenance_and_replays_stably():
    items = (ContextItem("a", "history", "same", "one"), ContextItem("b", "memory", "same", "two"), ContextItem("c", "policy", "keep", "three", True))
    snap = snapshot(items, token_count=12)
    assert [item.item_id for item in deduplicate(items)] == ["a", "c"]
    assert len(prefix_manifest(items)) == 2
    assert replay_manifest(snap, model="local", native_context=4096)["snapshot_digest"] == snap.snapshot_digest


def test_protected_duplicate_survives_in_either_order():
    plain = ContextItem("x", "history", "same", "one")
    guarded = ContextItem("y", "policy", "same", "two", True)
    for items in ((plain, guarded), (guarded, plain)):
        assert [item.item_id for item in deduplicate(items)] == ["y"]
        assert [item.item_id for item in snapshot(items, token_count=1).items] == ["y"]


def test_protected_items_are_never_merged_with_each_other():
    items = (ContextItem("p1", "policy", "same", "global", True), ContextItem("p2", "policy", "same", "project", True))
    assert [item.item_id for item in deduplicate(items)] == ["p1", "p2"]
    assert len(prefix_manifest(items)) == 2


def test_unprotected_duplicates_still_collapse_to_the_first():
    items = (ContextItem("a", "history", "same", "one"), ContextItem("b", "memory", "same", "two"), ContextItem("c", "memory", "other", "three"))
    assert [item.item_id for item in deduplicate(items)] == ["a", "c"]
