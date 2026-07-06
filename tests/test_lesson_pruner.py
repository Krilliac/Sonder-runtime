"""Tests for lesson_pruner. No GPU/network: embeddings are planted vectors,
so embeddings.cosine/to_blob/from_blob (pure stdlib math) run for real and
embeddings.embed (the Ollama network call) is never invoked.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import embeddings
import memory_store

import lesson_pruner


def _seed(conn, id_, text, vector, ts=None):
    memory_store.add_lesson(conn, id_, text, embeddings.to_blob(vector), source_interaction=None)
    if ts is not None:
        conn.execute("UPDATE lessons SET ts=? WHERE id=?", (ts, id_))
        conn.commit()


def _store_with_duplicates():
    """Two duplicate clusters plus one genuine unique lesson.

    Cluster A ~ [1,0,0]: 'a1' (short) and 'a2' (longer, near-identical vector).
    Cluster B ~ [0,1,0]: 'b1' and 'b2', near-identical vector, same length text
        so the tie-break falls to earliest ts.
    'u1' ~ [0,0,1] is orthogonal to both -- a genuine unique.
    """
    conn = memory_store.connect(":memory:")
    _seed(conn, "a1", "short lesson", [1.0, 0.0, 0.0], ts="2026-01-01 00:00:01")
    _seed(conn, "a2", "a much longer and more detailed lesson text", [0.995, 0.005, 0.0],
          ts="2026-01-01 00:00:02")
    _seed(conn, "b1", "same length text", [0.0, 1.0, 0.0], ts="2026-01-01 00:00:03")
    _seed(conn, "b2", "same length text", [0.0, 0.985, 0.015], ts="2026-01-01 00:00:04")
    _seed(conn, "u1", "totally unrelated unique lesson", [0.0, 0.0, 1.0],
          ts="2026-01-01 00:00:05")
    return conn


def test_cluster_near_duplicates_groups_similar_vectors_and_drops_uniques():
    conn = _store_with_duplicates()
    lessons = lesson_pruner._load_lessons(conn)
    clusters = lesson_pruner.cluster_near_duplicates(lessons, threshold=0.93)

    cluster_id_sets = [set(l["id"] for l in c) for c in clusters]
    assert {"a1", "a2"} in cluster_id_sets
    assert {"b1", "b2"} in cluster_id_sets
    assert len(clusters) == 2
    # The unique lesson must not appear in any cluster.
    assert not any("u1" in ids for ids in cluster_id_sets)


def test_cluster_near_duplicates_empty_input():
    assert lesson_pruner.cluster_near_duplicates([]) == []


def test_cluster_near_duplicates_respects_threshold():
    conn = _store_with_duplicates()
    lessons = lesson_pruner._load_lessons(conn)
    # A threshold above the maximum possible cosine (1.0) means nothing ever
    # clusters, no matter how near-identical the vectors are.
    clusters = lesson_pruner.cluster_near_duplicates(lessons, threshold=1.5)
    assert clusters == []


def test_choose_keeper_prefers_longest_text():
    cluster = [
        {"id": "short", "text": "hi", "ts": "2026-01-01 00:00:01"},
        {"id": "long", "text": "a much longer piece of lesson text", "ts": "2026-01-01 00:00:02"},
    ]
    assert lesson_pruner.choose_keeper(cluster)["id"] == "long"


def test_choose_keeper_ties_break_on_earliest_ts():
    cluster = [
        {"id": "later", "text": "same length", "ts": "2026-01-01 00:00:05"},
        {"id": "earlier", "text": "same length", "ts": "2026-01-01 00:00:01"},
    ]
    assert lesson_pruner.choose_keeper(cluster)["id"] == "earlier"


def test_build_plan_keeps_longer_lesson_and_sorts_by_similarity():
    conn = _store_with_duplicates()
    plan = lesson_pruner.build_plan(conn, threshold=0.93)

    assert len(plan) == 2
    by_keeper = {e["keeper_id"]: e for e in plan}
    assert "a2" in by_keeper  # a2 is longer than a1 -> kept
    assert by_keeper["a2"]["prune_ids"] == ["a1"]
    assert "b1" in by_keeper  # b1/b2 tie on text length -> earlier ts kept
    assert by_keeper["b1"]["prune_ids"] == ["b2"]
    # Sorted most-similar-cluster first.
    assert plan[0]["max_sim"] >= plan[1]["max_sim"]


def test_build_plan_no_duplicates_is_empty():
    conn = memory_store.connect(":memory:")
    _seed(conn, "u1", "one", [1.0, 0.0, 0.0])
    _seed(conn, "u2", "two", [0.0, 1.0, 0.0])
    _seed(conn, "u3", "three", [0.0, 0.0, 1.0])
    plan = lesson_pruner.build_plan(conn, threshold=0.93)
    assert plan == []


def test_load_lessons_skips_rows_with_no_embedding():
    conn = memory_store.connect(":memory:")
    memory_store.add_lesson(conn, "noemb", "text with no embedding", None, source_interaction=None)
    _seed(conn, "hasemb", "text with an embedding", [1.0, 0.0, 0.0])
    lessons = lesson_pruner._load_lessons(conn)
    assert [l["id"] for l in lessons] == ["hasemb"]


def test_prune_dry_run_default_does_not_delete():
    conn = _store_with_duplicates()
    before = {l["id"] for l in memory_store.all_lessons(conn)}

    plan, deleted = lesson_pruner.prune(conn, threshold=0.93)  # dry_run defaults True

    after = {l["id"] for l in memory_store.all_lessons(conn)}
    assert after == before
    assert deleted == 0
    assert len(plan) == 2


def test_prune_apply_deletes_losers_keeps_keepers():
    conn = _store_with_duplicates()

    plan, deleted = lesson_pruner.prune(conn, threshold=0.93, dry_run=False)

    remaining = {l["id"] for l in memory_store.all_lessons(conn)}
    assert deleted == 2
    assert remaining == {"a2", "b1", "u1"}
    assert "a1" not in remaining and "b2" not in remaining


def test_apply_plan_uses_injected_delete_fn():
    conn = _store_with_duplicates()
    plan = lesson_pruner.build_plan(conn, threshold=0.93)

    calls = []

    def fake_delete(_conn, lesson_id):
        calls.append(lesson_id)
        return True

    deleted = lesson_pruner.apply_plan(conn, plan, delete_fn=fake_delete)

    assert deleted == 2
    assert set(calls) == {"a1", "b2"}
    # The stub never touched the real table.
    assert {l["id"] for l in memory_store.all_lessons(conn)} == {"a1", "a2", "b1", "b2", "u1"}


def test_format_report_empty_plan():
    assert lesson_pruner.format_report([]) == "No near-duplicate lessons found."


def test_format_report_mentions_keeper_and_prune_counts():
    conn = _store_with_duplicates()
    plan = lesson_pruner.build_plan(conn, threshold=0.93)
    report = lesson_pruner.format_report(plan)

    assert "2 duplicate cluster(s), 2 lesson(s) prunable" in report
    assert "a2" in report
    assert "prune 1 dup(s)" in report


def test_truncate_shortens_long_text():
    long_text = "x" * 200
    out = lesson_pruner._truncate(long_text, n=20)
    assert len(out) == 20
    assert out.endswith("...")


def test_truncate_handles_none():
    assert lesson_pruner._truncate(None) == ""
